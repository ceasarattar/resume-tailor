"""Apply-execution: drive a real browser through each queued application.

This is the "next step" after the pipeline queues a job — it opens the application
in a **real, non-headless, logged-in** Chrome (a persistent profile, so your logins
persist across runs), autofills every field the answer engine resolved, attaches
the tailored résumé, screenshots the result, and **stops at the review/submit
gate**. You glance at it and click Submit (or pass --submit to let it click for
confirmed-safe ATSes). Never headless, never a blind mass-submit — the APPLY_SPEC
line.

Run:  python -m app.pipeline apply            # fill the next queued job, leave open for review
      python -m app.pipeline apply -n 3        # do the next 3
      python -m app.pipeline apply --submit    # also click Submit (use deliberately)
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from . import apply_driver, jobsdb
from .config import PATHS

PROFILE_DIR = PATHS.data / "browser_profile"

# label -> the input-locating keywords used to find that field on an arbitrary form.
_FIELD_KEYWORDS: dict[str, list[str]] = {
    "First Name": ["first name", "firstname", "given name", "first_name"],
    "Last Name": ["last name", "lastname", "family name", "surname", "last_name"],
    "Preferred First Name": ["preferred name", "preferred first", "nickname"],
    "Email": ["email", "e-mail"],
    "Phone": ["phone", "mobile", "telephone"],
    "Location (City)": ["location", "city"],
    "LinkedIn Profile": ["linkedin"],
    "Website": ["website", "portfolio", "personal site"],
    "GitHub": ["github"],
    "How did you hear about this job?": ["how did you hear", "source", "referral"],
}


def _draft_for(job: dict) -> dict:
    return apply_driver.build_draft(job).to_dict()


def _val(draft: dict, label: str):
    for f in draft["fields"]:
        if f["label"] == label and not f["needs_review"] and f["value"]:
            return f["value"]
    return None


def _try(fn):
    try:
        return fn()
    except Exception:
        return None


def _frames(page):
    """All fillable contexts: the page's frames (ATS forms are often in an iframe,
    e.g. a Greenhouse embed)."""
    fr = _try(lambda: list(page.frames)) or []
    return fr or [page]


def _fill_text(page, keywords: list[str], value: str) -> bool:
    """Find a text input by label/name/placeholder/aria-label across all frames."""
    for fr in _frames(page):
        for kw in keywords:
            loc = _try(lambda: fr.get_by_label(re.compile(re.escape(kw), re.I)))
            if loc and _try(lambda: loc.count()) and _try(lambda: loc.first.is_visible()):
                if _try(lambda: loc.first.fill(value)) is not None:
                    return True
        for kw in keywords:
            sel = (f'input[name*="{kw}" i], input[id*="{kw}" i], '
                   f'input[placeholder*="{kw}" i], input[aria-label*="{kw}" i], '
                   f'textarea[name*="{kw}" i], textarea[aria-label*="{kw}" i]')
            loc = _try(lambda: fr.locator(sel))
            if loc and _try(lambda: loc.count()) and _try(lambda: loc.first.is_visible()):
                if _try(lambda: loc.first.fill(value)) is not None:
                    return True
    return False


def _upload_resume(page, pdf: str) -> bool:
    if not pdf or not Path(pdf).exists():
        return False
    for fr in _frames(page):
        loc = _try(lambda: fr.locator('input[type="file"]'))
        if loc and _try(lambda: loc.count()):
            if _try(lambda: loc.first.set_input_files(pdf)) is not None:
                return True
    return False


def _click_apply(page) -> bool:
    """Many listings show the form only after an Apply button. Click it if present."""
    for fr in _frames(page):
        btn = _try(lambda: fr.get_by_role(
            "button", name=re.compile(r"^\s*(apply now|apply for this|apply|i'?m interested)", re.I)))
        if btn and _try(lambda: btn.count()) and _try(lambda: btn.first.is_visible()):
            _try(lambda: btn.first.click())
            _try(lambda: page.wait_for_timeout(2500))
            return True
        link = _try(lambda: fr.get_by_role(
            "link", name=re.compile(r"^\s*(apply now|apply for this|apply)", re.I)))
        if link and _try(lambda: link.count()) and _try(lambda: link.first.is_visible()):
            _try(lambda: link.first.click())
            _try(lambda: page.wait_for_timeout(2500))
            return True
    return False


def apply_jobs(*, limit: int = 1, submit: bool = False, headed: bool = True) -> list[dict]:
    """Open the next `limit` queued jobs in a real browser and autofill to review."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Playwright is not installed. Run: pip install playwright && "
            "python -m playwright install chromium"
        ) from exc

    queued = jobsdb.by_state("queued")[:limit]
    if not queued:
        print("Nothing queued. Run `python -m app.pipeline run` first.")
        return []

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=not headed, accept_downloads=True,
            viewport={"width": 1280, "height": 1600},
        )
        for job in queued:
            draft = _draft_for(job)
            page = ctx.new_page()
            filled, total = 0, 0
            try:
                page.goto(draft["apply_url"], wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
                # If the form isn't visible yet, click an Apply button to reveal it.
                if not _try(lambda: page.locator('input[type="file"], input[name*="name" i]').count()):
                    _click_apply(page)
                # text fields
                for label, kws in _FIELD_KEYWORDS.items():
                    v = _val(draft, label)
                    if v:
                        total += 1
                        if _fill_text(page, kws, str(v)):
                            filled += 1
                # résumé upload
                uploaded = _upload_resume(page, draft["resume_path"])
                # screenshot for the record
                shot = (PATHS.root / (job.get("resume_path") or "outputs") / "apply.png")
                _try(lambda: page.screenshot(path=str(shot), full_page=True))
                note = f"autofilled {filled}/{total} text fields; resume={'yes' if uploaded else 'no'}"
                if submit:
                    btn = _try(lambda: page.get_by_role("button",
                               name=re.compile(r"submit|apply", re.I)))
                    if btn and _try(lambda: btn.first.is_visible()):
                        _try(lambda: btn.first.click())
                        note += "; submit clicked"
                        jobsdb.set_state(job["uid"], "applied", note=note)
                    else:
                        jobsdb.set_state(job["uid"], "queued", note=note + "; no submit button found")
                else:
                    note += "; left open for review"
                print(f"  [{job['company']}] {job['title'][:40]} -> {note}")
                results.append({"uid": job["uid"], "note": note, "filled": filled, "total": total})
            except Exception as exc:  # noqa: BLE001
                print(f"  [{job.get('company')}] {job.get('title','')[:40]} -> ERROR {exc}")
                jobsdb.set_state(job["uid"], "queued", note=f"apply error: {exc}")
                results.append({"uid": job["uid"], "error": str(exc)})
        if headed and not submit:
            print("\nBrowser left open for your review. Press Enter here to close it...")
            _try(lambda: input())
        ctx.close()
    return results
