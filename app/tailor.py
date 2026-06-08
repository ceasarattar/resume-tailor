r"""Tailor a resume to a job — truthfully.

The model returns a STRUCTURED PLAN (tailored summary, selected/rephrased bullets
keyed to each real role/project, and ordered skill groups). It never emits LaTeX.
Python then maps that plan back onto the real profile metadata (employers, dates,
titles), the humanizer (app/humanize.py) makes the prose read like a person wrote
it, and the fitter (app/fit.py) renders + guarantees one page.

Honesty is enforced two ways: hard rules in the prompt, and grounding_violations()
on the rendered text (forbidden items + invented/inflated metrics).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from . import llm
from .config import PATHS
from .parse import ParsedJD


# ---------------------------------------------------------------- output schema
class SkillGroup(BaseModel):
    label: str = Field(description="Group name, e.g. Languages / Frameworks / Tools / Focus")
    items: list[str] = Field(default_factory=list)


class EntryTailoring(BaseModel):
    """Tailored bullets for ONE experience/project, keyed by its exact name so the
    renderer places them with the correct employer/project (never by position)."""
    ref: str = Field(description="Exact company name (experience) or project name")
    bullets: list[str] = Field(default_factory=list)


class TailorPlan(BaseModel):
    summary: str = ""
    experience: list[EntryTailoring] = Field(default_factory=list)
    projects: list[EntryTailoring] = Field(default_factory=list)
    skills: list[SkillGroup] = Field(default_factory=list)
    changelog: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)


@dataclass
class Tailoring:
    """Assembled, render-ready content (full — the fitter trims to one page)."""
    contact: dict
    summary: str
    experience: list[dict]
    experience_bullets: list[list[str]]
    education: list[dict]
    projects: list[dict]
    project_bullets: list[list[str]]
    skills: dict
    changelog: list[str] = field(default_factory=list)
    missing_requirements: list[str] = field(default_factory=list)
    plan: TailorPlan | None = None


SYSTEM = (
    "You are an expert technical resume writer. You tailor a candidate's REAL resume "
    "to a specific job, truthfully. You are given the candidate's profile as ground "
    "truth and a target job; you return a STRUCTURED PLAN (summary, per-role bullets, "
    "skill groups) — never LaTeX, never prose outside the schema.\n\n"
    "How you tailor (this is the whole point):\n"
    "1. Use the job's priority keywords, must-haves, and exact terminology as the lens "
    "for every choice. A resume tailored for this job must read differently from one "
    "tailored for a different job.\n"
    "2. Ethical keyword injection: reframe the candidate's REAL experience using the "
    "job's vocabulary where it is genuinely true. E.g. if the JD says 'RAG pipelines' "
    "and the candidate built 'retrieval workflows', you may say 'retrieval (RAG) "
    "workflows'. You NEVER add a skill, tool, or technology the candidate does not have.\n"
    "3. For each role: SELECT the most relevant bullets and DROP the rest, REORDER so "
    "the most job-relevant bullet is first, and REWRITE wording to foreground the "
    "competencies this job asks for. Each role's bullets must be rephrasings of ONLY "
    "that same role's source bullets — NEVER move an accomplishment between employers.\n\n"
    "Writing style (native, senior-engineer English):\n"
    "- Start every bullet with a strong, concrete past-tense verb; vary the verbs.\n"
    "- Short, direct sentences. Active voice. Lead with impact.\n"
    "- BANNED: 'utilize', 'leverage', 'in order to', 'responsible for', 'seamless', "
    "'robust', 'ensuring', 'ensure', 'showcasing', 'spearheaded', 'orchestrated', "
    "'cutting-edge', 'state-of-the-art', 'passionate', 'synergy', and corporate fluff. "
    "Write like a human engineer describing what they did.\n"
    "- No rule-of-three padding ('fast, scalable, and reliable'), no empty intensifiers.\n\n"
    "HONESTY — hard limits (never violate):\n"
    "- Never change a number, metric, percentage, date, tool, or technology.\n"
    "- Never invent achievements, employers, titles, or degrees.\n"
    "- Never add a skill the candidate did not list.\n"
    "- Never claim anything in the candidate's 'will NOT claim' list.\n"
    "- If a JD requirement isn't supported by the profile, put it in "
    "missing_requirements — do NOT fake it into the resume."
)


def _read(path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _experience_view(experience: list[dict]) -> str:
    lines = []
    for e in experience:
        lines.append(
            f'ref="{e.get("company","")}" — {e.get("title","")} '
            f"({e.get('start','')}-{e.get('end','')}), {e.get('location','')}"
        )
        for b in e.get("bullets", []):
            if str(b).strip():
                lines.append(f"      - {b}")
    return "\n".join(lines) or "(none)"


def _projects_view(projects: list[dict]) -> str:
    lines = []
    for p in projects:
        if not (p.get("name") or "").strip():
            continue
        lines.append(f'ref="{p.get("name","")}" ({", ".join(p.get("tech", []))})')
        for b in p.get("bullets", []):
            if str(b).strip():
                lines.append(f"      - {b}")
    return "\n".join(lines) or "(none)"


def _aligned_bullets(entries: list[dict], tailored: list[EntryTailoring], key: str) -> list[list[str]]:
    """Map the model's keyed bullet groups back onto entries by name (never by
    position), so each entry keeps its OWN bullets. Empty when no match → caller
    falls back to the entry's original bullets (safe, correctly attributed)."""
    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

    by_ref = {norm(t.ref): t.bullets for t in tailored}
    out: list[list[str]] = []
    for e in entries:
        k = norm(e.get(key, ""))
        bullets = by_ref.get(k, [])
        if not bullets:  # try a looser contains-match
            for rk, rv in by_ref.items():
                if rk and (rk in k or k in rk):
                    bullets = rv
                    break
        out.append([b for b in bullets if str(b).strip()])
    return out


def build_prompt(
    jd: ParsedJD,
    experience_data: dict,
    *,
    rag_snippets: list[str] | None = None,
    extra_instructions: str = "",
) -> str:
    corrections = _read(PATHS.corrections)
    about = _read(PATHS.about_me)
    experience = experience_data.get("experience", [])
    projects = experience_data.get("projects", [])
    real_skills = experience_data.get("skills", {})

    rag_block = ""
    if rag_snippets:
        rag_block = "\n\n## Retrieved past corrections/examples\n" + "\n".join(
            f"- {s}" for s in rag_snippets
        )

    jd_block = (
        f"Title: {jd.title}\nCompany: {jd.company}\nSeniority: {jd.seniority}\n"
        f"Priority keywords: {', '.join(jd.keywords)}\n"
        f"Must-haves:\n" + "\n".join(f"- {m}" for m in jd.must_haves) + "\n"
        f"Nice-to-haves:\n" + "\n".join(f"- {n}" for n in jd.nice_to_haves)
    )

    return f"""Tailor my resume to the job below.

# Rules (corrections.md — follow ALL of these)
{corrections}
{rag_block}

# My background (about-me.md — ground truth, includes my "will NOT claim" list)
{about}

# My experience entries (use the exact ref string per entry; rewrite/select within each)
{_experience_view(experience)}

# My projects (use the exact ref string per project)
{_projects_view(projects)}

# My real skills (only use these; reorder/relabel but never add new ones)
{json.dumps(real_skills, indent=2)}

# Target job
{jd_block}

# What to produce
- summary: 2-3 sentences written specifically for THIS role, in my voice, foregrounding
  the 2-3 real strengths that matter most here. A different job must yield a clearly
  different summary.
- experience: one object per role {{"ref": <exact company name above>, "bullets": [...]}}.
  Select the 2-4 most relevant bullets for the strongest role (fewer for weaker ones),
  most-relevant first, rewritten in the job's terminology. Bullets describe ONLY that role.
- projects: objects {{"ref": <exact project name>, "bullets": [...]}} for the job-relevant
  projects only (omit the rest).
- skills: groups (Languages, Frameworks, Tools, and optionally Focus) using ONLY my
  skills, reordered so the job-relevant ones come first.
- changelog: what you tailored and why it fits THIS job.
- missing_requirements: each JD requirement I have no evidence for. Be honest.
{extra_instructions}"""


def tailor(
    jd: ParsedJD,
    experience_data: dict,
    *,
    rag_snippets: list[str] | None = None,
    extra_instructions: str = "",
    max_tokens: int = 4096,
) -> Tailoring:
    """Get a tailoring plan from the LLM and assemble render-ready content.

    Length is NOT capped here — the fitter (app/fit.py) trims to exactly one page,
    removing the least-relevant material first, so nothing relevant is lost early.
    """
    user = build_prompt(
        jd, experience_data, rag_snippets=rag_snippets, extra_instructions=extra_instructions
    )
    plan = llm.complete_json(
        system=SYSTEM, user=user, schema_model=TailorPlan, max_tokens=max_tokens
    )
    return assemble(plan, experience_data)


def assemble(plan: TailorPlan, experience_data: dict) -> Tailoring:
    """Map a TailorPlan back onto the real profile metadata. No length capping."""
    def _orig(entry: dict) -> list[str]:
        return [b for b in entry.get("bullets", []) if str(b).strip()]

    # Skills: model groups if present, else the profile's own groups (capitalized).
    if plan.skills:
        skills = {g.label: list(g.items) for g in plan.skills if g.items}
    else:
        raw = experience_data.get("skills", {}) or {}
        skills = {k.capitalize(): list(v) for k, v in raw.items() if v} if isinstance(raw, dict) else {}

    # Experience: keep every role (in profile order); bullets from the plan by ref,
    # falling back to the role's own originals.
    exp_entries = experience_data.get("experience", [])
    exp_aligned = _aligned_bullets(exp_entries, plan.experience, "company")
    exp_bullets = [exp_aligned[i] or _orig(e) for i, e in enumerate(exp_entries)]

    # Projects: keep only the ones the model selected (fallback: all), profile order.
    all_projects = [p for p in experience_data.get("projects", []) if (p.get("name") or "").strip()]
    proj_aligned = _aligned_bullets(all_projects, plan.projects, "name")
    if any(proj_aligned):
        pairs = [(p, b) for p, b in zip(all_projects, proj_aligned) if b]
    else:
        pairs = [(p, _orig(p)) for p in all_projects]
    proj_entries = [p for p, _ in pairs]
    proj_bullets = [b for _, b in pairs]

    return Tailoring(
        contact=experience_data.get("contact", {}) or {},
        summary=plan.summary or "",
        experience=exp_entries,
        experience_bullets=exp_bullets,
        education=experience_data.get("education", []) or [],
        projects=proj_entries,
        project_bullets=proj_bullets,
        skills=skills,
        changelog=plan.changelog,
        missing_requirements=plan.missing_requirements,
        plan=plan,
    )


# ------------------------------------------------------------ honesty checking
_FORBIDDEN_STOP = {
    "I", "A", "AN", "THE", "NOT", "NO", "OR", "AND", "IN", "OF", "ON", "TO",
    "WITH", "HAVE", "HAS", "NEVER", "DON'T", "DONT", "USED", "USE", "WORKED",
    "WORK", "EXPERIENCE", "PRODUCTION", "PROFESSIONALLY", "THINGS", "WILL",
    "CLAIM", "HARD", "BOUNDARY", "LIST", "ANYTHING", "MUST", "BE", "AS", "HAVING",
}


def _not_claim_section(about_me: str) -> tuple[str, str]:
    """Split about_me into (everything-before-NOT-claim, the-NOT-claim-section)."""
    low = about_me.lower()
    idx = low.find("not claim")
    if idx == -1:
        return about_me, ""
    head_start = about_me.rfind("\n", 0, idx)
    before = about_me[: head_start if head_start != -1 else idx]
    section = about_me[head_start if head_start != -1 else idx:]
    nl = section.find("\n## ", 3)
    if nl != -1:
        before = before + section[nl:]
        section = section[:nl]
    return before, section


def forbidden_terms(about_me: str, profile_context: str = "") -> list[str]:
    """Branded/proper-noun terms from the 'Things I will NOT claim' section that
    do NOT appear anywhere else in the profile (real off-limits tripwires)."""
    _before, section = _not_claim_section(about_me)
    if not section:
        return []
    ctx = profile_context.lower()
    terms, seen = [], set()
    for raw in re.findall(r"[A-Za-z]{4,}", section):
        tok = raw
        if tok.upper() in _FORBIDDEN_STOP or not any(c.isupper() for c in tok):
            continue
        if tok.lower() in seen:
            continue
        seen.add(tok.lower())
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(tok.lower()) + r"(?![A-Za-z0-9])", ctx):
            continue
        terms.append(tok)
    return terms


_METRIC_RE = re.compile(r"\d[\d,\.]*\s?(?:%|[MKBmkb]\+?(?![A-Za-z])|x\b)")


def _metrics(text: str) -> set[str]:
    """Normalized metric-like tokens (percentages, 2M+, 50K, 3x)."""
    return {m.group(0).replace(" ", "").lower().rstrip(".") for m in _METRIC_RE.finditer(text)}


def grounding_violations(resume_text: str, *, about_me: str, experience: dict | None = None) -> list[str]:
    """Honesty check on the rendered resume: forbidden items + invented/inflated metrics."""
    before, _section = _not_claim_section(about_me)
    forbidden_context = before + "\n" + json.dumps(experience or {})
    full_context = about_me + "\n" + json.dumps(experience or {})
    violations: list[str] = []
    low = resume_text.lower()

    for term in forbidden_terms(about_me, forbidden_context):
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(term.lower()) + r"(?![A-Za-z0-9])", low):
            violations.append(f"claims forbidden item from profile: '{term}'")

    profile_metrics = _metrics(full_context)
    for metric in _metrics(resume_text):
        if metric not in profile_metrics:
            violations.append(f"metric not found in profile (possibly invented/inflated): '{metric}'")

    return violations
