"""Discover open job postings — web-wide, recent-first.

Two kinds of source, combined and de-duplicated:

1. **Aggregators (breadth).** `JobSpy` scrapes the big boards that already index
   nearly every posting — Indeed (no rate limit, the workhorse), Google Jobs, and
   ZipRecruiter — filtered to the last N hours. One query per field term (software
   engineer, data engineer, ML, ...). This is how we "cover everything recent"
   without knowing company names in advance. Plus keyless JSON feeds (Remotive).

2. **ATS board APIs (clean apply).** Greenhouse / Lever / Ashby per-company JSON —
   the same endpoints the careers pages call. Best apply URLs, used for known
   companies. Optional/secondary now that aggregators provide breadth.

Everything normalizes to `JobPosting` (with a real `posted_at`), then the caller
de-dupes, caps to the last few days, and sorts most-recent-first.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import html
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

_UA = {"User-Agent": "Mozilla/5.0 (resume-pipeline job discovery)"}
_TIMEOUT = 40.0


# --------------------------------------------------------------------- model
@dataclass
class JobPosting:
    uid: str                 # stable global id
    ats: str                 # source: greenhouse|lever|ashby|indeed|google|zip_recruiter|remotive
    company: str
    title: str
    location: str = ""
    remote: bool | None = None
    url: str = ""            # human-facing posting URL
    apply_url: str = ""      # canonical application URL
    jd_text: str = ""        # plain-text job description
    department: str = ""
    employment_type: str = ""
    posted_at: str = ""      # ISO8601 date/datetime the job was posted (for recency)
    updated_at: str = ""
    salary_min: float | None = None   # annualized USD, if the posting states it
    salary_max: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ----------------------------------------------------------------- helpers
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t  ]+")
_BLANKS_RE = re.compile(r"\n{3,}")
_DIRECT_ATS = {"greenhouse", "lever", "ashby"}


def html_to_text(s: str) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"(?i)<\s*br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</\s*(p|div|li|h[1-6]|tr)\s*>", "\n", s)
    s = re.sub(r"(?i)<\s*li[^>]*>", "• ", s)
    s = _TAG_RE.sub("", s)
    s = _WS_RE.sub(" ", s)
    s = _BLANKS_RE.sub("\n\n", s)
    return s.strip()


def _to_iso(v: Any) -> str:
    """Normalize a date/datetime/epoch/ISO-string to an ISO8601 string."""
    if v is None or v == "":
        return ""
    if isinstance(v, (int, float)):
        secs = v / 1000 if v > 1e12 else v
        try:
            return datetime.fromtimestamp(secs, tz=timezone.utc).isoformat()
        except Exception:
            return ""
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    s = str(v).strip()
    # pandas NaT / nan
    if s.lower() in ("nat", "nan", "none"):
        return ""
    return s


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def _hash_uid(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


_SALARY_MULT = {"yearly": 1, "annual": 1, "year": 1, "monthly": 12, "month": 12,
                "weekly": 52, "week": 52, "daily": 260, "day": 260, "hourly": 2080, "hour": 2080}


def _annualize(amount: Any, interval: Any) -> float | None:
    """Convert a pay amount at some interval to an approximate annual USD figure."""
    import math
    if amount is None:
        return None
    try:
        a = float(amount)
    except (TypeError, ValueError):
        return None
    if math.isnan(a) or a <= 0:
        return None
    mult = _SALARY_MULT.get(str(interval or "yearly").lower().strip(), 1)
    return round(a * mult, 2)


# ------------------------------------------------------------- ATS fetchers
def fetch_greenhouse(token: str, *, max_jobs: int = 1000) -> list[JobPosting]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    r = httpx.get(url, headers=_UA, timeout=_TIMEOUT)
    r.raise_for_status()
    out: list[JobPosting] = []
    for j in (r.json().get("jobs") or [])[:max_jobs]:
        jid = str(j.get("id"))
        loc = (j.get("location") or {}).get("name", "") or ""
        depts = j.get("departments") or []
        out.append(JobPosting(
            uid=f"greenhouse:{token}:{jid}", ats="greenhouse",
            company=j.get("company_name") or token, title=(j.get("title") or "").strip(),
            location=loc, remote=("remote" in loc.lower()) or None,
            url=j.get("absolute_url", ""), apply_url=j.get("absolute_url", ""),
            jd_text=html_to_text(j.get("content", "")),
            department=depts[0].get("name", "") if depts else "",
            posted_at=_to_iso(j.get("first_published") or j.get("updated_at")),
            updated_at=_to_iso(j.get("updated_at")),
        ))
    return out


def fetch_lever(token: str, *, max_jobs: int = 1000) -> list[JobPosting]:
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    r = httpx.get(url, headers=_UA, timeout=_TIMEOUT)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    out: list[JobPosting] = []
    for p in (r.json() or [])[:max_jobs]:
        cats = p.get("categories") or {}
        wt = (p.get("workplaceType") or "").lower()
        body = "\n\n".join(x for x in (p.get("descriptionPlain"), p.get("additionalPlain")) if x)
        out.append(JobPosting(
            uid=f"lever:{token}:{p.get('id')}", ats="lever", company=token,
            title=(p.get("text") or "").strip(), location=cats.get("location", "") or "",
            remote=True if wt == "remote" else (False if wt in ("onsite", "on-site", "hybrid") else None),
            url=p.get("hostedUrl", ""),
            apply_url=p.get("applyUrl", "") or (p.get("hostedUrl", "") + "/apply" if p.get("hostedUrl") else ""),
            jd_text=body or html_to_text(p.get("description", "")),
            department=cats.get("department", "") or cats.get("team", "") or "",
            employment_type=cats.get("commitment", "") or "",
            posted_at=_to_iso(p.get("createdAt")), updated_at=_to_iso(p.get("createdAt")),
        ))
    return out


def fetch_ashby(token: str, *, max_jobs: int = 1000) -> list[JobPosting]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
    r = httpx.get(url, headers=_UA, timeout=_TIMEOUT)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    out: list[JobPosting] = []
    for j in (r.json().get("jobs") or [])[:max_jobs]:
        if j.get("isListed") is False:
            continue
        out.append(JobPosting(
            uid=f"ashby:{token}:{j.get('id')}", ats="ashby", company=token,
            title=(j.get("title") or "").strip(), location=j.get("location", "") or "",
            remote=bool(j.get("isRemote")) if j.get("isRemote") is not None else None,
            url=j.get("jobUrl", ""), apply_url=j.get("applyUrl", "") or j.get("jobUrl", ""),
            jd_text=j.get("descriptionPlain", "") or html_to_text(j.get("descriptionHtml", "")),
            department=j.get("department", "") or "", employment_type=j.get("employmentType", "") or "",
            posted_at=_to_iso(j.get("publishedAt") or j.get("updatedAt")),
        ))
    return out


_ATS_FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


# ------------------------------------------------------------- aggregators
def fetch_jobspy(
    *, search_terms: list[str], location: str = "United States",
    sites: list[str] | None = None, results_per_term: int = 50,
    hours_old: int = 72, country_indeed: str = "USA",
) -> list[JobPosting]:
    """Breadth engine: scrape Indeed/Google/ZipRecruiter via JobSpy, one query per
    term. Heavy deps (pandas) are imported lazily so the rest of the app never
    needs them. Failures on one term/site are skipped, never fatal."""
    import math

    from jobspy import scrape_jobs  # lazy: pulls pandas/numpy

    sites = sites or ["indeed", "google", "zip_recruiter"]
    errors: list[str] = []
    out: list[JobPosting] = []
    for term in search_terms:
        df = None
        for attempt in (1, 2):  # one retry: scrapers occasionally drop a connection
            try:
                df = scrape_jobs(
                    site_name=sites, search_term=term,
                    google_search_term=f"{term} jobs near {location} since last 3 days",
                    location=location, results_wanted=results_per_term,
                    hours_old=hours_old, country_indeed=country_indeed,
                    description_format="markdown", verbose=0,
                )
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    errors.append(f"jobspy '{term}': {type(exc).__name__}: {exc}")
        if df is None:
            continue
        for _, r in df.iterrows():
            def g(k):
                v = r.get(k)
                if v is None:
                    return ""
                if isinstance(v, float) and math.isnan(v):
                    return ""
                return v
            site = str(g("site") or "jobspy")
            company = str(g("company") or "").strip()
            title = str(g("title") or "").strip()
            if not title:
                continue
            job_url = str(g("job_url") or "")
            uid = f"{site}:{_slug(company)[:24]}:{_hash_uid(job_url or title, company)}"
            rem = g("is_remote")
            interval = g("interval")
            out.append(JobPosting(
                uid=uid, ats=site, company=company or "(unknown)", title=title,
                location=str(g("location") or ""),
                remote=bool(rem) if isinstance(rem, bool) else None,
                url=job_url, apply_url=str(g("job_url_direct") or job_url),
                jd_text=str(g("description") or ""),
                employment_type=str(g("job_type") or ""),
                posted_at=_to_iso(g("date_posted")),
                salary_min=_annualize(g("min_amount"), interval),
                salary_max=_annualize(g("max_amount"), interval),
            ))
    fetch_jobspy.last_errors = errors  # type: ignore[attr-defined]
    return out


fetch_jobspy.last_errors = []  # type: ignore[attr-defined]


def fetch_remotive(categories: list[str] | None = None, *, max_jobs: int = 200) -> list[JobPosting]:
    """Keyless remote-tech feed. Direct apply URLs, fresh publication dates."""
    cats = categories or ["software-dev", "data"]
    out: list[JobPosting] = []
    for cat in cats:
        try:
            r = httpx.get(f"https://remotive.com/api/remote-jobs?category={cat}&limit={max_jobs}",
                          headers=_UA, timeout=_TIMEOUT)
            r.raise_for_status()
        except Exception:
            continue
        for j in (r.json().get("jobs") or []):
            out.append(JobPosting(
                uid=f"remotive:{j.get('id')}", ats="remotive",
                company=(j.get("company_name") or "").strip(), title=(j.get("title") or "").strip(),
                location=j.get("candidate_required_location", "") or "Remote", remote=True,
                url=j.get("url", ""), apply_url=j.get("url", ""),
                jd_text=html_to_text(j.get("description", "")),
                department=j.get("category", "") or "", employment_type=j.get("job_type", "") or "",
                posted_at=_to_iso(j.get("publication_date")),
            ))
    return out


# ----------------------------------------------------------------- dedup + recency
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def dedup(jobs: Iterable[JobPosting]) -> list[JobPosting]:
    """Collapse the same role seen across sources. Key = (company, title). Prefer a
    direct-ATS posting (best apply URL); otherwise keep the first seen."""
    best: dict[tuple[str, str], JobPosting] = {}
    for j in jobs:
        key = (_norm(j.company), _norm(j.title))
        cur = best.get(key)
        if cur is None:
            best[key] = j
        elif j.ats in _DIRECT_ATS and cur.ats not in _DIRECT_ATS:
            best[key] = j  # upgrade to the direct-apply version
    return list(best.values())


def _age_days(posted_at: str) -> float | None:
    if not posted_at:
        return None
    s = posted_at.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0


def filter_recent(jobs: Iterable[JobPosting], max_age_days: int = 3) -> list[JobPosting]:
    """Keep jobs posted within max_age_days. Jobs with an unknown date are kept
    (we can't prove they're stale) but will sort last."""
    out = []
    for j in jobs:
        age = _age_days(j.posted_at)
        if age is None or age <= max_age_days:
            out.append(j)
    return out


def sort_recent(jobs: list[JobPosting]) -> list[JobPosting]:
    """Most-recent-first; undated jobs last."""
    return sorted(jobs, key=lambda j: (_age_days(j.posted_at) if j.posted_at else 9e9), reverse=False)


# ----------------------------------------------------------------- orchestrator
def discover(sources: Iterable[dict], *, max_per_source: int = 1000) -> list[JobPosting]:
    """Fetch + normalize ATS sources (back-compat helper used by the ATS path)."""
    jobs: list[JobPosting] = []
    errors: list[str] = []
    for src in sources:
        ats = str(src.get("ats", "")).strip().lower()
        token = str(src.get("token", "")).strip()
        fn = _ATS_FETCHERS.get(ats)
        if not fn or not token:
            errors.append(f"skip invalid source: {src!r}")
            continue
        try:
            jobs.extend(fn(token, max_jobs=max_per_source))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{ats}:{token} -> {type(exc).__name__}: {exc}")
    discover.last_errors = errors  # type: ignore[attr-defined]
    return jobs


discover.last_errors = []  # type: ignore[attr-defined]


def discover_all(cfg: dict) -> tuple[list[JobPosting], list[str]]:
    """Run every enabled provider per the discovery config, then dedup + cap to the
    last `max_age_days` + sort most-recent-first. Returns (jobs, errors)."""
    errors: list[str] = []
    jobs: list[JobPosting] = []
    agg = cfg.get("aggregator", {}) or {}

    if agg.get("enabled", True):
        terms = agg.get("search_terms") or ["software engineer", "data engineer"]
        try:
            jobs += fetch_jobspy(
                search_terms=terms, location=agg.get("location", "United States"),
                sites=agg.get("sites", ["indeed", "google", "zip_recruiter"]),
                results_per_term=int(agg.get("results_per_term", 50)),
                hours_old=int(agg.get("hours_old", 72)),
            )
            errors += list(getattr(fetch_jobspy, "last_errors", []))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"jobspy unavailable: {type(exc).__name__}: {exc}")
        if agg.get("remotive", True):
            try:
                jobs += fetch_remotive(agg.get("remotive_categories"))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"remotive: {exc}")

    ats_sources = cfg.get("sources") or cfg.get("ats_sources") or []
    if ats_sources:
        jobs += discover(ats_sources, max_per_source=int(cfg.get("max_per_source", 1000)))
        errors += list(getattr(discover, "last_errors", []))

    before = len(jobs)
    jobs = dedup(jobs)
    jobs = filter_recent(jobs, int(cfg.get("max_age_days", 3)))
    jobs = sort_recent(jobs)
    errors.append(f"discovered {before} raw -> {len(jobs)} unique within {cfg.get('max_age_days', 3)}d")
    return jobs, errors
