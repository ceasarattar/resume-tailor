"""The autonomous pipeline: discover -> score -> tailor -> queue/apply.

One command, run as often as you like. Each run:

  1. DISCOVER  pull fresh postings from the configured ATS board APIs and upsert
               them into data/jobs.sqlite (dedup'd, so re-runs are cheap).
  2. SCORE     prefilter by title/location (free), then grounded LLM fit-judge on
               survivors. Recommended -> 'scored'; rejected -> 'skipped' (+reason).
  3. TAILOR    for the top recommended jobs (up to tailor_cap), run the existing
               résumé pipeline (parse->tailor->humanize->fit->ATS->honesty gate)
               and attach the resulting one-page PDF.
  4. APPLY     default mode 'review': enqueue with a pre-resolved answer preview
               for human review (the extension or apply_driver fills on demand).
               mode 'auto' hands queued jobs to apply_driver under guardrails.

Honesty + the no-headless-mass-submit line from APPLY_SPEC.md are preserved:
matching never inflates the candidate, and submission is gated by mode.

Config (config.yaml):

    discovery:
      sources:
        - {ats: greenhouse, token: databricks}
        - {ats: ashby,      token: openai}
      max_per_source: 300
      tailor_cap: 8
      match: {min_score: 0.6, remote_ok: true, ...}
      apply: {mode: review, daily_cap: 15}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import jobsdb, match
from .config import load_config
from .llm import LLMError


@dataclass
class RunReport:
    discovered_new: int = 0
    discovered_seen: int = 0
    scored: int = 0
    recommended: int = 0
    skipped: int = 0
    tailored: int = 0
    tailor_errors: list[str] = field(default_factory=list)
    queued: int = 0
    discover_errors: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


def _discovery_cfg() -> dict:
    return load_config().get("discovery", {}) or {}


# ----------------------------------------------------------------- stages
def stage_discover(report: RunReport) -> None:
    """Web-wide discovery: aggregators (JobSpy/Remotive) + any ATS sources, deduped,
    capped to the last `max_age_days`, sorted most-recent-first."""
    from . import discover as disc
    cfg = _discovery_cfg()
    jobs, errors = disc.discover_all(cfg)
    report.discover_errors = errors
    res = jobsdb.upsert_discovered(jobs)
    report.discovered_new = res["new"]
    report.discovered_seen = res["seen"]


def stage_score(report: RunReport, *, use_llm: bool = True) -> None:
    """Score 'discovered' jobs efficiently:

    1. Prefilter everything (free).
    2. Group survivors by company; free keyword pre-rank picks the most promising
       `judge_per_company` per company — the rest are skipped (we only want 1-2
       jobs per company, so there's no reason to spend an LLM call on the rest).
    3. LLM-judge the shortlist, best-first, bounded by `score_cap` (resumable:
       leftovers stay 'discovered' for the next run).
    """
    from collections import defaultdict

    cfg = _discovery_cfg()
    budget = int(cfg.get("score_cap", 40))
    judge_per_co = int(cfg.get("judge_per_company", 2))
    candidate_ctx = match._candidate_context()

    def skip(job, reason):
        jobsdb.set_score(job["uid"], score=0.0, reasons=[reason], missing=[],
                         blockers=[], recommend=False)
        report.scored += 1
        report.skipped += 1

    # 1 + 2: prefilter, group survivors by company.
    survivors: dict[str, list[dict]] = defaultdict(list)
    for job in jobsdb.by_state("discovered"):
        keep, reason = match.prefilter(job)
        if keep:
            survivors[(job.get("company") or "").lower()].append(job)
        else:
            skip(job, f"prefilter: {reason}")

    # Free keyword pre-rank; keep top judge_per_company per company.
    shortlist: list[dict] = []
    for jobs in survivors.values():
        jobs.sort(key=match._keyword_overlap_score, reverse=True)
        shortlist.extend(jobs[:judge_per_co])
        for j in jobs[judge_per_co:]:
            skip(j, "per-company judge cap (only 1-2 roles judged per company)")

    # 3: judge most-recent-first (the user prioritizes fresh postings), so the
    #    LLM budget always covers the newest jobs before older ones.
    from .discover import _age_days
    shortlist.sort(key=lambda j: (_age_days(j.get("posted_at", "")) if j.get("posted_at") else 9e9))
    for job in shortlist:
        if use_llm and budget <= 0:
            break  # out of budget; remaining survivors wait for the next run
        res = match.score_job(job, candidate_ctx=candidate_ctx, use_llm=use_llm)
        if use_llm:
            budget -= 1
        jobsdb.set_score(
            job["uid"], score=res.score, reasons=res.reasons,
            missing=res.missing, blockers=res.blockers, recommend=res.recommend,
        )
        report.scored += 1
        if res.recommend:
            report.recommended += 1
        else:
            report.skipped += 1


def stage_tailor(report: RunReport) -> None:
    """Generate a tailored résumé for the top recommended jobs (up to tailor_cap)."""
    from collections import Counter

    from .cli import generate_resume  # local import: heavy deps
    cfg = _discovery_cfg()
    cap = int(cfg.get("tailor_cap", 8))
    max_co = int(cfg.get("max_per_company", 2))
    # 'scored' is ordered by score desc; take the best, but at most max_co per company.
    seen: Counter[str] = Counter()
    todo: list[dict] = []
    for job in jobsdb.by_state("scored"):
        if len(todo) >= cap:
            break
        co = (job.get("company") or "").lower()
        if seen[co] >= max_co:
            continue  # leave extra recommended roles as 'scored', just don't tailor them
        seen[co] += 1
        todo.append(job)
    for job in todo:
        try:
            res = generate_resume(
                job["jd_text"], company=job.get("company"), role=job.get("title"),
            )
            jobsdb.set_tailored(
                job["uid"], res.out_dir.as_posix(),
                note=("ok" if (res.ats_ok and res.grounding_ok and res.one_page)
                      else "generated_with_warnings"),
            )
            report.tailored += 1
        except LLMError as exc:
            report.tailor_errors.append(f"{job['uid']}: LLM/billing: {exc}")
            jobsdb.set_state(job["uid"], "error", note=f"llm: {exc}")
            break  # billing/credit error will hit every job — stop early
        except Exception as exc:  # noqa: BLE001
            report.tailor_errors.append(f"{job['uid']}: {type(exc).__name__}: {exc}")
            jobsdb.set_state(job["uid"], "error", note=str(exc)[:300])


def stage_apply(report: RunReport) -> None:
    """Queue tailored jobs for application.

    mode 'review' (default): mark 'queued' for human-reviewed filling (extension /
    apply_driver fill-to-review). mode 'auto': hand to apply_driver under guardrails
    (daily cap, never headless) — only if explicitly configured.
    """
    cfg = _discovery_cfg()
    apply_cfg = cfg.get("apply", {}) or {}
    mode = str(apply_cfg.get("mode", "review")).lower()
    daily_cap = int(apply_cfg.get("daily_cap", 15))

    tailored = jobsdb.by_state("tailored")
    if mode == "auto":
        from . import apply_driver
        budget = max(0, daily_cap - jobsdb.applied_today())
        for job in tailored[:budget]:
            try:
                ok = apply_driver.apply(job, submit=True)
                jobsdb.set_state(job["uid"], "applied" if ok else "queued",
                                 note="auto-submit" if ok else "auto fill failed; queued")
                if ok:
                    report.queued += 1
            except Exception as exc:  # noqa: BLE001
                jobsdb.set_state(job["uid"], "queued", note=f"auto error: {exc}")
    else:
        from . import apply_driver
        for job in tailored:
            jobsdb.set_state(job["uid"], "queued", note="ready for review")
            report.queued += 1
        # Materialize the review queue (resolved answer drafts) for the extension/UI.
        apply_driver.export_queue()


# ----------------------------------------------------------------- driver
def run(*, use_llm: bool = True, do_tailor: bool = True) -> RunReport:
    from . import llm
    llm.reset_usage()
    report = RunReport()
    stage_discover(report)
    stage_score(report, use_llm=use_llm)
    if do_tailor:
        stage_tailor(report)
        stage_apply(report)
    report.usage = llm.usage_report()
    return report


def _print_report(r: RunReport) -> None:
    print("\n==================== PIPELINE RUN ====================")
    print(f"  Discovered:   {r.discovered_new} new, {r.discovered_seen} already seen")
    if r.discover_errors:
        for e in r.discover_errors:
            print(f"    ! source error: {e}")
    print(f"  Scored:       {r.scored}  ->  {r.recommended} recommended, {r.skipped} skipped")
    print(f"  Tailored:     {r.tailored} résumés generated")
    for e in r.tailor_errors:
        print(f"    ! tailor: {e}")
    print(f"  Queued:       {r.queued} ready to apply")
    print("  ----------------------------------------------------")
    c = jobsdb.counts()
    print("  DB state:    ", ", ".join(f"{k}={v}" for k, v in sorted(c.items())) or "(empty)")
    u = r.usage or {}
    if u.get("by_model"):
        print("  --- Claude usage (approx) -------------------------")
        for model, m in u["by_model"].items():
            print(f"    {model:20} {m['calls']:>4} calls  "
                  f"{m['input']:>8} in / {m['output']:>7} out  ~${m['cost_usd']:.4f}")
        print(f"    TOTAL: {u['total_calls']} calls  ~${u['total_cost_usd']:.4f}")
    print("=====================================================\n")


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Autonomous job-application pipeline.")
    sub = ap.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="discover -> score -> tailor -> queue")
    p_run.add_argument("--no-llm", action="store_true", help="score with keyword fallback only")
    p_run.add_argument("--no-tailor", action="store_true", help="discover + score only")

    sub.add_parser("status", help="show pipeline DB state")

    p_q = sub.add_parser("queue", help="list jobs ready to apply")
    p_q.add_argument("-n", type=int, default=20)

    p_a = sub.add_parser("apply", help="open queued jobs in a real browser and autofill to review")
    p_a.add_argument("-n", type=int, default=1, help="how many queued jobs to open")
    p_a.add_argument("--submit", action="store_true", help="also click Submit (use deliberately)")

    args = ap.parse_args(argv)

    if args.cmd == "status" or args.cmd is None:
        c = jobsdb.counts()
        print("Pipeline DB:", ", ".join(f"{k}={v}" for k, v in sorted(c.items())) or "(empty)")
        print("Applied today:", jobsdb.applied_today())
        return 0

    if args.cmd == "queue":
        for j in jobsdb.by_state("queued", limit=args.n):
            print(f"  [{j.get('score')}] {j['company']} — {j['title']}  ({j['ats']})")
            print(f"      apply: {j['apply_url']}")
            print(f"      resume: {j.get('resume_path')}")
        return 0

    if args.cmd == "apply":
        from . import apply_runner
        apply_runner.apply_jobs(limit=args.n, submit=args.submit, headed=True)
        return 0

    if args.cmd == "run":
        report = run(use_llm=not args.no_llm, do_tailor=not args.no_tailor)
        _print_report(report)
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
