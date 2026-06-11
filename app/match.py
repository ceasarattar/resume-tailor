"""Score a job posting against the candidate's real profile.

Two stages, cheapest first:

1. **Prefilter (deterministic, free).** Drop the obvious mismatches by title and
   location before spending a single token — e.g. a "Senior Staff Engineer" in
   "Bangalore" never reaches the LLM. Configurable via config.yaml.

2. **Grounded fit-judge (one LLM call per survivor).** Compare the JD to the real
   profile and return a 0..1 fit score, hard blockers (clearance/citizenship/
   wildly-senior/wrong-field/location), and the requirements the candidate is
   missing. Same honesty stance as the résumé: it *judges* fit against ground
   truth — it never inflates the candidate to manufacture a match.

A job is recommended only when: not prefiltered, no hard blockers, and
score >= min_score. Everything else is recorded as skipped with a reason, so a
run is auditable.
"""
from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from . import config as cfgmod
from . import llm
from .answers import load_profile
from .config import PATHS, load_config

# ----------------------------------------------------------------- config
_DEFAULT_EXCLUDE = [
    "senior", "sr.", " sr ", "staff", "principal", "lead", "manager", "director",
    "vp", "vice president", "head of", "architect", "distinguished", "fellow",
    "chief", "sales", "account executive", " ii", " iii", " iv", "iii)", "ii)",
    " phd",
]
_DEFAULT_INCLUDE = [
    "engineer", "developer", "software", "backend", "back end", "back-end",
    "frontend", "front end", "full stack", "fullstack", "full-stack", "data",
    "platform", "infrastructure", "security", "machine learning", "sde",
    "programmer", "solutions engineer", "new grad", "early career", "associate",
    "intern", "graduate",
]
_DEFAULT_LOCATIONS = ["chicago", "illinois", "united states", "usa", "u.s."]

# US states + abbreviations, so "Remote - California" reads as US-eligible but
# "Remote - Denmark" does not. The LLM judge can't be trusted to gate location.
_US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho", "illinois",
    "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland",
    "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana",
    "nebraska", "nevada", "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
    "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah",
    "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia", "washington d.c.", "d.c.",
}
_US_TOKENS = {"united states", "usa", "u.s.", "u.s ", " us ", " us,", "(us", "us)",
              "remote us", "remote - us", "remote, us", "anywhere in the us"}
# Two-letter state codes, matched only at a comma boundary ("Austin, TX") so the
# word "in"/"or"/"me" etc. can't false-match.
_US_ABBR = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il",
    "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt",
    "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri",
    "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc",
}


def _match_cfg() -> dict:
    return (load_config().get("discovery", {}) or {}).get("match", {}) or {}


def _is_us(low: str) -> bool:
    return (any(t in low for t in _US_TOKENS)
            or any(f" {st} " in low or f"{st}," in low or f"- {st}" in low for st in _US_STATES)
            or any(f", {ab} " in low or low.rstrip().endswith(f", {ab}") for ab in _US_ABBR))


def _location_ok(loc: str, remote: bool | None = None) -> tuple[bool, str]:
    """Candidate's rule: accept a role only if it's (a) remote within the US, or
    (b) onsite/hybrid in Illinois. Everything else (onsite elsewhere, or remote
    abroad) is rejected. Returns (ok, reason)."""
    cfg = _match_cfg()
    raw = (loc or "").strip()
    if not raw:
        return True, "no location"
    low = f" {raw.lower()} "
    onsite = [t.lower() for t in cfg.get("locations_include", ["chicago", "illinois"])]

    # 1. Illinois — the only acceptable onsite/hybrid geography.
    if any(t in low for t in onsite) or re.search(r",\s*il\b|-\s*il\b", low):
        return True, "illinois"

    # 2. Remote — must be within the US.
    is_remote = bool(remote) or any(k in low for k in ("remote", "anywhere", "work from home", "wfh"))
    if is_remote and cfg.get("remote_ok", True):
        if _is_us(low):
            return True, "us remote"
        rest = re.sub(r"[-–—,;()/]", " ", low)
        for k in ("remote", "anywhere", "work from home", "wfh"):
            rest = rest.replace(k, " ")
        rest = re.sub(r"\s+", " ", rest).strip()
        if not rest:
            return True, "bare remote (us-assumed)"
        return False, f"remote non-US: {raw}"

    # 3. A bare US-national descriptor (no city) reads as national/remote.
    if low.strip() in ("united states", "usa", "u.s.", "u.s", "us"):
        return True, "us national"

    return False, f"onsite outside IL: {raw}"


# ----------------------------------------------------------------- prefilter
def _has_term(text: str, terms: list[str]) -> str | None:
    t = f" {text.lower()} "
    for term in terms:
        if term.lower() in t:
            return term
    return None


def prefilter(job: dict) -> tuple[bool, str]:
    """Cheap deterministic gate. Returns (keep, reason)."""
    cfg = _match_cfg()
    title = job.get("title", "") or ""
    loc = job.get("location", "") or ""
    remote = job.get("remote")

    exclude = cfg.get("titles_exclude", _DEFAULT_EXCLUDE)
    include = cfg.get("titles_include", _DEFAULT_INCLUDE)

    hit = _has_term(title, exclude)
    if hit:
        return False, f"title excluded by '{hit.strip()}'"
    if include and not _has_term(title, include):
        return False, "title not in target roles"

    loc_ok, loc_reason = _location_ok(loc, job.get("remote"))
    if not loc_ok:
        return False, loc_reason

    # Salary floor: drop only when the posting states a max below the floor.
    # Jobs with no stated salary pass (most don't post one).
    min_salary = cfg.get("min_salary")
    if min_salary:
        smax = job.get("salary_max")
        if smax is not None and float(smax) > 0 and float(smax) < float(min_salary):
            return False, f"pay ${int(float(smax)/1000)}k < ${int(float(min_salary)/1000)}k floor"
    return True, "ok"


# ----------------------------------------------------------------- fit judge
class FitJudgment(BaseModel):
    fit_score: float = Field(description="0.0-1.0 overlap of the candidate's real skills with the role's core technical requirements")
    seniority_fit: bool = Field(description="True if the role asks for 0-3 years / intern / new-grad / mid level (NOT Senior/Staff/Principal/Lead/Manager, NOT 5+ years)")
    location_fit: bool = Field(description="True if the candidate can plausibly work this location")
    hard_blockers: list[str] = Field(
        default_factory=list,
        description="ONLY truly disqualifying items: security clearance, a required "
        "citizenship/visa the candidate lacks, a required degree they don't hold, "
        "5+ years required, or a fundamentally different field. Empty if none.",
    )
    missing_requirements: list[str] = Field(
        default_factory=list, description="Listed requirements the candidate does NOT meet"
    )
    rationale: str = Field(description="One sentence explaining the score")


_JUDGE_SYSTEM = (
    "You are a precise technical recruiter screening ONE job for ONE candidate. "
    "You are given the candidate's real profile (ground truth) and a job description. "
    "Judge how well THIS candidate fits THIS role.\n\n"
    "CANDIDATE LEVEL (the bar): early-career — about 1-2 years of professional "
    "experience plus internships, currently finishing an M.S. in Computer Science. "
    "Apply this generously:\n"
    "- A role asking for 0-3 years of experience is a SENIORITY FIT. '2 years', "
    "'1-3 years', 'new grad', 'entry', 'associate', 'intern' all line up — set "
    "seniority_fit=true for these.\n"
    "- Only set seniority_fit=false for an explicit Senior/Staff/Principal/Lead/"
    "Manager/Director title, or a hard requirement of 5+ years.\n"
    "- '2-4 years preferred' is NOT a blocker; preferred/nice-to-have is never a blocker.\n\n"
    "RULES:\n"
    "- Be honest and grounded. Do NOT inflate the candidate or invent qualifications.\n"
    "- hard_blockers are ONLY: required security clearance, a required citizenship/visa "
    "the candidate lacks, a required degree they don't hold, 5+ years REQUIRED, or a "
    "fundamentally different field (sales quota, mechanical/civil/biomed eng, nursing).\n"
    "- fit_score = overlap of the candidate's real skills with the role's core technical "
    "requirements. 0.8+ = strong, 0.6-0.8 = solid fit, <0.5 = poor."
)


def _candidate_context() -> str:
    """Compact ground-truth summary of the candidate, built once per run ideally."""
    about = PATHS.about_me.read_text(encoding="utf-8") if PATHS.about_me.exists() else ""
    try:
        exp = json.loads(PATHS.experience.read_text(encoding="utf-8"))
    except Exception:
        exp = {}
    roles = [
        f"{e.get('title','')} @ {e.get('company','')} ({e.get('start','')}-{e.get('end','')})"
        for e in exp.get("experience", [])
    ]
    edu = [f"{e.get('degree','')} {e.get('school','')}" for e in exp.get("education", [])]
    skills = exp.get("skills", {})
    skills_txt = json.dumps(skills) if isinstance(skills, dict) else str(skills)
    return (
        f"{about}\n\n"
        f"ROLES: {'; '.join(r for r in roles if r.strip(' @()-'))}\n"
        f"EDUCATION: {'; '.join(e for e in edu if e.strip())}\n"
        f"SKILLS: {skills_txt}"
    )


def judge_fit(job: dict, *, candidate_ctx: str | None = None) -> FitJudgment:
    ctx = candidate_ctx if candidate_ctx is not None else _candidate_context()
    jd = (job.get("jd_text") or "")[:3500]
    user = (
        f"# CANDIDATE (ground truth)\n{ctx}\n\n"
        f"# JOB\nTitle: {job.get('title','')}\nCompany: {job.get('company','')}\n"
        f"Location: {job.get('location','')} (remote={job.get('remote')})\n\n"
        f"Description:\n{jd}\n\n"
        "Judge the candidate's fit for this job."
    )
    # Route to the cheap judge model (Haiku on Claude): screening is high-volume.
    return llm.complete_json(
        system=_JUDGE_SYSTEM, user=user, schema_model=FitJudgment,
        max_tokens=700, model=cfgmod.judge_model(),
    )


# ----------------------------------------------------------------- combined
@dataclass
class MatchResult:
    recommend: bool
    score: float
    reasons: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)


@functools.lru_cache(maxsize=1)
def _skill_tokens() -> frozenset[str]:
    """Candidate skill tokens, read once and cached (the free pre-rank runs this
    per job)."""
    try:
        exp = json.loads(PATHS.experience.read_text(encoding="utf-8"))
        skills = exp.get("skills", {})
        skill_txt = json.dumps(skills) if isinstance(skills, dict) else str(skills)
    except Exception:
        skill_txt = ""
    return frozenset(re.findall(r"[a-z+#.]+", skill_txt.lower()))


def _keyword_overlap_score(job: dict) -> float:
    """Free deterministic relevance: overlap of JD tokens with the candidate's
    skills. Used both as the no-LLM fallback score and to pre-rank which jobs
    per company are worth spending an LLM judge call on."""
    sk = _skill_tokens()
    jd = set(re.findall(r"[a-z+#.]+", (job.get("jd_text", "") + " " + job.get("title", "")).lower()))
    if not sk or not jd:
        return 0.0
    return round(len(sk & jd) / max(8, len(sk)), 3)


def score_job(job: dict, *, candidate_ctx: str | None = None, use_llm: bool = True) -> MatchResult:
    """Full scoring: prefilter -> (LLM judge | keyword fallback) -> recommend."""
    cfg = _match_cfg()
    min_score = float(cfg.get("min_score", 0.6))

    keep, reason = prefilter(job)
    if not keep:
        return MatchResult(recommend=False, score=0.0, reasons=[f"prefilter: {reason}"])

    if not use_llm:
        s = _keyword_overlap_score(job)
        return MatchResult(
            recommend=s >= min_score, score=s,
            reasons=[f"keyword-overlap fallback score={s}"],
        )

    try:
        j = judge_fit(job, candidate_ctx=candidate_ctx)
    except Exception as exc:  # noqa: BLE001
        s = _keyword_overlap_score(job)
        return MatchResult(
            recommend=s >= min_score, score=s,
            reasons=[f"llm judge failed ({type(exc).__name__}); keyword score={s}"],
        )

    blockers = [b for b in (j.hard_blockers or []) if b.strip()]
    recommend = (not blockers) and j.seniority_fit and j.fit_score >= min_score
    reasons = [j.rationale] if j.rationale else []
    if not j.seniority_fit:
        reasons.append("seniority mismatch")
    if not j.location_fit:
        reasons.append("location concern")
    return MatchResult(
        recommend=recommend,
        score=round(float(j.fit_score), 3),
        reasons=reasons,
        missing=[m for m in (j.missing_requirements or []) if m.strip()],
        blockers=blockers,
    )
