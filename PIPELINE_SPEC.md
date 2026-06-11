# PIPELINE_SPEC.md — the autonomous discover → apply pipeline

`PROJECT_SPEC.md` turns one JD into an honest, ATS-safe résumé. `APPLY_SPEC.md`
fills one application form. This file covers the layer that drives them
end-to-end, unattended: **find compatible jobs, score them against the real
profile, tailor a résumé for each match, and queue it for application.**

Same cardinal rule, inherited from both halves: **honesty — never assert or
imply anything not in `profile/`.** The matcher judges fit against ground truth;
it never inflates the candidate to manufacture a match.

```
discover ─────▶ score ──▶ tailor ──▶ apply
 (JobSpy: Indeed (prefilter (existing  (review queue;
  /Google/Zip +  + LLM judge) résumé    real-session fill,
  Remotive + ATS)             pipeline) never headless)
  last 3d, recent-first
```

## How discovery works (web-wide, recent-first)

To "cover every recent posting related to me" without knowing company names in
advance, discovery (`app/discover.py`, `discover_all()`) combines two source kinds,
de-dupes them, caps to the last `max_age_days`, and sorts most-recent-first:

**1. Aggregators (breadth).** `JobSpy` scrapes the boards that already index nearly
every posting — **Indeed** (no rate limit, the workhorse), **Google Jobs**, and
**ZipRecruiter** — one query per field term ("software engineer", "data engineer",
"machine learning engineer", …) with `hours_old=72`. Returns title, company,
location, **date_posted**, job URL, and full description. Plus the keyless
**Remotive** feed for remote-tech. Verified working and fast (~1s per Indeed query).
Heavy `pandas`/`numpy` deps are imported lazily inside the JobSpy provider, so the
rest of the app never loads them.

**2. ATS board APIs (clean apply).** Optional per-company Greenhouse/Lever/Ashby
JSON (`boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`, etc.) — the
same endpoints careers pages call, with the best apply URLs. Add companies via
`discovery.sources`.

`dedup()` keeps one row per (company, title), preferring a direct-ATS posting over
an aggregator one. `filter_recent()` drops anything older than `max_age_days`
(default 3); `sort_recent()` orders newest-first so the LLM budget and tailoring
always hit the freshest jobs first.

## The stages (`app/pipeline.py`)

1. **discover** (`discover.py`) — fetch every source, normalize to `JobPosting`,
   upsert into `data/jobs.sqlite`. Dedup'd, so re-runs are cheap and idempotent.
2. **score** (`match.py`) — three gates, cheapest first, so Claude only ever sees
   a tiny, pre-ranked shortlist:
   - **Prefilter (free):** title include/exclude + a US-aware location filter
     (`Remote - California` passes; `Remote - Spain` does not). Cuts ~98%.
   - **Per-company pre-rank (free):** group survivors by company, rank by keyword
     overlap, keep only the top `judge_per_company` (default 2) — you want 1-2 roles
     per company, so there's no reason to judge the rest. (Across 24 companies:
     3730 raw postings → 3490 prefiltered → 204 capped → **36 reach the judge**.)
   - **Grounded LLM fit-judge** on that shortlist, on a **cheap model**
     (`judge_model`, default Haiku — it's classification, not writing), bounded by
     `score_cap` and resumable. Returns a 0..1 score, seniority/location fit, **hard
     blockers** (clearance, citizenship, degree, 5+ years, wrong field) and missing
     requirements. The candidate is treated as early-career: roles asking up to
     **~3 years** pass the seniority gate ("2 years" lines up); only explicit
     Senior/Staff/Principal/Lead or 5+ years is a seniority blocker. Recommend =
     no blockers ∧ seniority fit ∧ score ≥ `min_score`; else `skipped` with a reason.
3. **tailor** — for the top recommended jobs (≤ `tailor_cap`), run the existing
   résumé pipeline (`cli.generate_resume`): parse → tailor → humanize → fit to one
   page → ATS check → honesty gate. Attach the resulting PDF.
4. **apply** (`apply_driver.py`) — resolve the standard application field set
   against the profile (the same honesty-gated engine the extension uses) into an
   `ApplicationDraft`, and queue it (`data/queue.json`). **Submission happens in
   your real logged-in session** (Chrome extension, or an on-demand Playwright
   pass) behind a review gate — never a headless mass-submitter (the `APPLY_SPEC`
   line). `apply.mode: auto` is reserved for the guardrailed path (daily cap,
   real session, kill switch).

## State machine (`app/jobsdb.py`, `data/jobs.sqlite`)

```
discovered ─▶ scored ─▶ tailored ─▶ queued ─▶ applied
        └────▶ skipped (filtered / blocked, with reason)
        └────▶ error
```

Each job is one row, keyed by a stable `uid` = `{ats}:{token}:{id}`. Scoring is
**resumable**: a run LLM-judges up to `score_cap` survivors and leaves the rest
`discovered` for next time, so cost per run is bounded regardless of board size.

## Run it

```sh
.venv/Scripts/python.exe -m app.pipeline run            # discover → score → tailor → queue
.venv/Scripts/python.exe -m app.pipeline run --no-tailor # discover + score only
.venv/Scripts/python.exe -m app.pipeline run --no-llm    # keyword-only scoring (free/offline)
.venv/Scripts/python.exe -m app.pipeline status          # DB state + applied-today
.venv/Scripts/python.exe -m app.pipeline queue           # jobs ready to apply
```

Or via the server (`app/main.py`): `POST /api/pipeline/run`,
`GET /api/pipeline/status`, `GET /api/pipeline/queue`,
`POST /api/pipeline/applied` (the extension reports a reviewed submit here).

## Config (`config.yaml` → `discovery:`)

```yaml
discovery:
  max_age_days: 3          # only consider jobs posted within the last N days
  aggregator:
    enabled: true
    sites: [indeed, google, zip_recruiter]   # indeed = no rate limit, the workhorse
    location: "United States"
    hours_old: 72
    results_per_term: 60
    remotive: true
    search_terms: ["software engineer", "data engineer", "machine learning engineer", ...]
  sources: []              # optional: [{ats: greenhouse, token: stripe}] for clean apply
  judge_model: claude-haiku-4-5   # cheap model for the high-volume fit-judge
  judge_per_company: 2     # LLM-judge only the N most promising roles per company
  max_per_company: 2       # tailor/apply at most N roles per company
  score_cap: 120           # max LLM fit-judgments per run (Haiku ~cents; resumable)
  tailor_cap: 5            # max résumés generated per run (the real $ — Sonnet)
  match:
    min_score: 0.6
    titles_include: [engineer, developer, software, backend, data, ...]
    titles_exclude: [senior, "sr.", staff, principal, manager, architect, ...]
    locations_include: [chicago, illinois, united states, remote]
  apply:
    mode: review           # review | auto (guardrailed)
    daily_cap: 15
```

## Cost (Claude)

Discovery is **free** (scraping). Claude spend scales with **distinct recent
companies, not raw postings**. A measured full run: 596 raw → **211 unique jobs in
the last 3 days across 154 companies** → prefilter + per-company cap drop most for
free → **~82 reach the judge** on **Haiku** (~5-8¢) → at most `tailor_cap` résumés
on **Sonnet** (~3-5¢ each). A typical full run is **≈ $0.20-0.35**. Dry-run the
breadth for free with `--no-llm` before spending anything.

## What's deliberately NOT automated

- **No headless submission.** Forms are filled in the user's real browser session
  with a human review gate. This keeps the tool ToS-safe and accounts un-banned —
  a choice, not a limitation (see `APPLY_SPEC.md` "Roadmap to full automation").
- **No fabrication.** A job the candidate can't honestly fill (missing a required
  clearance/degree) is `skipped`, not papered over. Ungrounded answer fields are
  flagged for review, never invented.
