"""Prepare a tailored job for application — the bridge to the "hands".

Per APPLY_SPEC.md, actual form-filling happens in your **real logged-in browser
session** (the Chrome extension, or an on-demand Playwright pass) behind a review
gate — never a headless mass-submitter. This module does the part that belongs in
the pipeline: it resolves the standard application field set against your profile
(reusing the honesty-gated answer engine in answers.py), attaches the tailored
résumé, and produces an `ApplicationDraft` the extension/UI consumes.

So a queued job arrives at the review step already filled-in-principle: every
deterministic field resolved, every ungrounded field flagged. The human (or the
guardrailed auto path) just confirms and submits.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from . import answers
from .config import PATHS

# The field set common to Greenhouse / Lever / Ashby / Workday / iCIMS forms.
# (label, field_type) — resolved via the same tiered engine the extension uses.
STANDARD_FIELDS: list[tuple[str, str]] = [
    ("First Name", "text"),
    ("Last Name", "text"),
    ("Preferred First Name", "text"),
    ("Email", "text"),
    ("Phone", "text"),
    ("Location (City)", "text"),
    ("LinkedIn Profile", "text"),
    ("Website", "text"),
    ("GitHub", "text"),
    ("Are you legally authorized to work in the country in which you are applying?", "select"),
    ("Do you now or will you in the future need sponsorship for employment visa status?", "select"),
    ("How did you hear about this job?", "text"),
    ("Have you previously worked here?", "select"),
    ("Gender", "select"),
    ("Are you Hispanic/Latino?", "select"),
    ("Race & Ethnicity", "select"),
    ("Veteran Status", "select"),
    ("Disability Status", "select"),
]


@dataclass
class ApplicationDraft:
    uid: str
    company: str
    title: str
    ats: str
    apply_url: str
    resume_path: str
    fields: list[dict[str, Any]] = field(default_factory=list)
    review_items: list[str] = field(default_factory=list)
    ready: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _resume_pdf(job: dict) -> str:
    """Resolve the tailored résumé PDF path for a job, if it was generated."""
    rp = job.get("resume_path") or ""
    if not rp:
        return ""
    p = PATHS.root / rp
    pdf = p / "resume.pdf" if p.is_dir() else p
    return pdf.as_posix() if pdf.exists() else ""


def build_draft(job: dict) -> ApplicationDraft:
    """Resolve the standard field set for one job into a review-ready draft."""
    apply_url = job.get("apply_url", "") or job.get("url", "")
    draft = ApplicationDraft(
        uid=job.get("uid", ""),
        company=job.get("company", ""),
        title=job.get("title", ""),
        ats=job.get("ats", ""),
        apply_url=apply_url,
        resume_path=_resume_pdf(job),
    )
    jd_ctx = (job.get("jd_text") or "")[:2000]
    for label, ftype in STANDARD_FIELDS:
        res = answers.resolve(label=label, field_type=ftype, url=apply_url, jd_context=jd_ctx)
        entry = {
            "label": label,
            "value": res.value,
            "source": res.source,
            "confidence": res.confidence,
            "needs_review": res.needs_review,
        }
        draft.fields.append(entry)
        # EEO/demographic fields default to a safe "decline" and are never blockers.
        is_eeo = label in ("Gender", "Are you Hispanic/Latino?", "Race & Ethnicity",
                            "Veteran Status", "Disability Status")
        if res.needs_review and not is_eeo and label not in ("Preferred First Name", "GitHub", "Website"):
            draft.review_items.append(label)

    if not draft.resume_path:
        draft.review_items.append("Résumé not generated yet")
    draft.ready = not draft.review_items
    return draft


# ---------------------------------------------------------------- queue export
def export_queue() -> dict[str, Any]:
    """Materialize drafts for every queued/tailored job to data/queue.json.

    This is what the Chrome extension / UI reads to drive review-gated filling in
    your real browser session.
    """
    from . import jobsdb
    jobs = jobsdb.by_state("queued") + jobsdb.by_state("tailored")
    drafts = [build_draft(j).to_dict() for j in jobs]
    out = PATHS.data / "queue.json"
    out.write_text(json.dumps(drafts, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"count": len(drafts), "path": out.as_posix()}


def apply(job: dict, *, submit: bool = False) -> bool:
    """Integration point for the pipeline's auto mode.

    Builds the draft and records it. We never headless-submit (APPLY_SPEC line):
    actual submission must happen in the user's real session via the extension or
    an on-demand Playwright pass. Returns True only if a real submission occurred.
    """
    draft = build_draft(job)
    # Persist the draft into the queue regardless of mode.
    from . import jobsdb
    jobsdb.set_state(job["uid"], "queued",
                     note=("ready" if draft.ready else f"review: {', '.join(draft.review_items)}"))
    export_queue()
    # No headless submission path by design; auto-submit requires a real session.
    return False
