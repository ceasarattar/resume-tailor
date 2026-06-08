"""CLI: a job description file -> a tailored, humanized, one-page, ATS-checked PDF.

Usage:
    python -m app.cli path/to/jd.txt [--final] [--keep-going]

The orchestration lives in `generate_resume()` so the FastAPI server (app.main)
and this CLI share exactly one pipeline path:

    parse -> tailor -> humanize -> fit to one page -> ATS check -> honesty gate -> store
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import compile as comp
from . import fit as fitmod
from . import humanize as humanizer
from . import parse as parsemod
from . import rag, store, tailor
from .config import PATHS, humanize_enabled, load_config, one_page_enabled
from .llm import LLMError


@dataclass
class GenerateResult:
    date: str
    company: str
    role: str
    out_dir: Path
    pdf_path: Path
    changelog: list[str] = field(default_factory=list)
    missing_requirements: list[str] = field(default_factory=list)
    keywords_used: list[str] = field(default_factory=list)
    ats_ok: bool = True
    ats_issues: list[str] = field(default_factory=list)
    grounding_ok: bool = True
    grounding_violations: list[str] = field(default_factory=list)
    pages: int = 1
    one_page: bool = True
    trims: list[str] = field(default_factory=list)
    tells: list[str] = field(default_factory=list)


def _profile_is_empty(experience_data: dict, about_me: str = "") -> bool:
    """True if experience.json has no real content to build a resume from."""
    contact = experience_data.get("contact", {}) or {}
    has_name = bool((contact.get("name") or "").strip())
    has_job = any((e.get("company") or "").strip() for e in experience_data.get("experience", []))
    has_edu = any((e.get("school") or "").strip() for e in experience_data.get("education", []))
    has_proj = any((p.get("name") or "").strip() for p in experience_data.get("projects", []))
    skills = experience_data.get("skills", {}) or {}
    has_skills = any(v for v in skills.values()) if isinstance(skills, dict) else False
    return not (has_name and (has_job or has_edu or has_proj or has_skills))


def generate_resume(
    jd_text: str,
    *,
    company: str | None = None,
    role: str | None = None,
    final: bool = False,
) -> GenerateResult:
    """Run the full pipeline on raw JD text and return a structured result."""
    if len(jd_text.strip()) < 30:
        raise ValueError("Job description looks empty.")

    about_me = PATHS.about_me.read_text(encoding="utf-8") if PATHS.about_me.exists() else ""
    try:
        experience_data = json.loads(PATHS.experience.read_text(encoding="utf-8"))
    except Exception:
        experience_data = {}
    if _profile_is_empty(experience_data, about_me):
        raise ValueError(
            "Your profile is empty. Fill profile/experience.json (at least your "
            "contact name and one experience entry) and profile/about-me.md before "
            "generating — the system will not invent a background for you."
        )

    jd = parsemod.parse_jd(jd_text)
    if company:
        jd.company = company
    if role:
        jd.title = role
    date, company_slug, role_slug = parsemod.slugs(jd)

    brief_md = parsemod.to_brief_md(jd)
    parsemod.save_brief(jd)

    rag_query = " ".join(
        [jd.title, jd.company, " ".join(jd.keywords), " ".join(jd.must_haves)]
    ).strip()
    rag_snippets = rag.retrieve(rag_query, int(load_config().get("rag_top_k", 4)))

    out_dir = store.output_dir_for(date, company_slug, role_slug)
    do_humanize = humanize_enabled()
    fit_iters = 80 if one_page_enabled() else 0

    def _build(extra: str = ""):
        # 1. Tailor: structured plan -> render-ready content (full, uncapped).
        tailoring = tailor.tailor(jd, experience_data, rag_snippets=rag_snippets, extra_instructions=extra)
        # 2. Humanize: rewrite to natural voice, preserving every fact (best-effort).
        if do_humanize:
            tailoring = humanizer.humanize(tailoring)
        # 3. Fit: render + compile + trim until exactly one page.
        fit_res = fitmod.fit_to_one_page(tailoring, out_dir, final=final, max_iters=fit_iters)
        # 4. ATS text-layer check.
        report = comp.ats_check(fit_res.pdf_path)
        text = report.extractors.get("pypdf", "")
        # 5. Honesty grounding check on the rendered text.
        viol = tailor.grounding_violations(text, about_me=about_me, experience=experience_data)
        return tailoring, fit_res, report, viol

    tailoring, fit_res, report, violations = _build()

    # Honesty gate: regenerate ONCE with explicit feedback if the draft fabricated.
    if violations:
        feedback = (
            "\n\n# CRITICAL HONESTY FEEDBACK (your previous draft FAILED)\n"
            "Your previous attempt violated the ground-truth profile:\n"
            + "\n".join(f"- {v}" for v in violations)
            + "\nRegenerate using ONLY facts from the profile. Do NOT mention any "
            "forbidden item, and do NOT introduce any number or metric that isn't "
            "already in my profile.\n"
        )
        t2, f2, r2, v2 = _build(feedback)
        if len(v2) <= len(violations):
            tailoring, fit_res, report, violations = t2, f2, r2, v2

    grounding_ok = not violations
    tells = humanizer.remaining_tells(tailoring)
    status = "generated" if (report.ok and grounding_ok and fit_res.one_page) else "generated_with_warnings"

    out_dir = store.write_outputs(
        date=date, company=company_slug, role=role_slug,
        tex=fit_res.tex, pdf_path=fit_res.pdf_path, brief_md=brief_md,
    )
    store.record(
        date=date, company=company_slug, role=role_slug, out_dir=out_dir,
        missing_requirements=tailoring.missing_requirements,
        keywords_used=jd.keywords,
        status=status,
    )

    return GenerateResult(
        date=date, company=company_slug, role=role_slug,
        out_dir=out_dir, pdf_path=out_dir / "resume.pdf",
        changelog=tailoring.changelog,
        missing_requirements=tailoring.missing_requirements,
        keywords_used=jd.keywords,
        ats_ok=report.ok, ats_issues=report.issues,
        grounding_ok=grounding_ok, grounding_violations=violations,
        pages=fit_res.pages, one_page=fit_res.one_page, trims=fit_res.trims,
        tells=tells,
    )


def run(jd_path: Path, *, final: bool = False, keep_going: bool = False) -> int:
    jd_text = Path(jd_path).read_text(encoding="utf-8")
    print("==> Generating (parse -> tailor -> humanize -> fit -> compile -> store)...", flush=True)
    res = generate_resume(jd_text, final=final)

    print(f"==> Pages: {res.pages} ({'one page [OK]' if res.one_page else 'STILL OVER ONE PAGE'})")
    if res.trims:
        print("    Trimmed to fit:")
        for tcut in res.trims:
            print(f"      - {tcut}")

    if res.ats_ok:
        print(f"==> ATS check passed ({res.out_dir.name}).")
    else:
        print("WARN: ATS check found issues:", file=sys.stderr)
        for issue in res.ats_issues:
            print(f"  - {issue}", file=sys.stderr)

    if not res.grounding_ok:
        print("\n!!! HONESTY CHECK FAILED — the model may have fabricated content:", file=sys.stderr)
        for v in res.grounding_violations:
            print(f"  - {v}", file=sys.stderr)
        print("  Review the resume carefully before using it.", file=sys.stderr)

    if res.tells:
        print("\nResidual AI-writing tells (review/edit if you like):")
        for t in res.tells[:10]:
            print(f"  - {t}")

    print(f"\n==> Done. Output: {res.out_dir}")
    if res.changelog:
        print("\nChangelog:")
        for c in res.changelog:
            print(f"  - {c}")
    if res.missing_requirements:
        print("\nPossibly missing requirements (flagged, not faked):")
        for m in res.missing_requirements:
            print(f"  - {m}")
    ok = (res.ats_ok and res.grounding_ok and res.one_page)
    return 0 if (ok or keep_going) else 1


def main(argv: list[str] | None = None) -> int:
    # Make console output safe for any profile/content characters on Windows.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Tailor a resume to a job description.")
    ap.add_argument("jd", type=Path, help="Path to a job-description text file")
    ap.add_argument("--final", action="store_true", help="Use the pdflatex fallback renderer")
    ap.add_argument("--keep-going", action="store_true", help="Exit 0 even if a check fails")
    args = ap.parse_args(argv)
    try:
        return run(args.jd, final=args.final, keep_going=args.keep_going)
    except LLMError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except (ValueError, comp.CompileError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
