r"""Tailor a resume.

The model returns STRUCTURED CONTENT (a JSON "plan": tailored summary, rephrased
bullets aligned to the real experience/projects, and ordered skill groups). Python
then renders the .tex deterministically (app/render.py) from profile/experience.json
metadata + that content. The model never emits LaTeX, so it can't corrupt braces
or drift on names/dates, and every résumé compiles.

Honesty is still enforced after rendering by grounding_violations().
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, ValidationError

from . import llm, render
from .config import PATHS, load_config, tailor_model
from .parse import ParsedJD


# ---------------------------------------------------------------- output schema
class SkillGroup(BaseModel):
    label: str
    items: list[str] = Field(default_factory=list)


class TailorPlan(BaseModel):
    summary: str = ""
    experience_bullets: list[list[str]] = Field(default_factory=list)
    project_bullets: list[list[str]] = Field(default_factory=list)
    skills: list[SkillGroup] = Field(default_factory=list)
    changelog: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)


@dataclass
class TailorResult:
    tex: str
    changelog: list[str] = field(default_factory=list)
    missing_requirements: list[str] = field(default_factory=list)
    plan: TailorPlan | None = None


SYSTEM = (
    "You tailor resumes truthfully. You are given the candidate's REAL profile as "
    "ground truth and a target job. You output ONLY structured JSON content "
    "(a summary, rephrased bullet points, and skill groups). You NEVER invent "
    "employers, titles, dates, metrics, degrees, or skills, and you never claim "
    "anything the candidate listed as off-limits. You rephrase and reorder the "
    "candidate's real material to foreground what's relevant to the job."
)


def _read(path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _experience_view(experience: list[dict]) -> str:
    lines = []
    for i, e in enumerate(experience):
        lines.append(
            f"[{i}] {e.get('title','')} at {e.get('company','')} "
            f"({e.get('start','')}-{e.get('end','')}), {e.get('location','')}"
        )
        for b in e.get("bullets", []):
            if str(b).strip():
                lines.append(f"      - {b}")
    return "\n".join(lines) or "(none)"


def _projects_view(projects: list[dict]) -> str:
    lines = []
    for i, p in enumerate(projects):
        if not (p.get("name") or "").strip():
            continue
        lines.append(f"[{i}] {p.get('name','')} ({', '.join(p.get('tech', []))})")
        for b in p.get("bullets", []):
            if str(b).strip():
                lines.append(f"      - {b}")
    return "\n".join(lines) or "(none)"


def build_messages(
    jd: ParsedJD,
    experience_data: dict,
    *,
    rag_snippets: list[str] | None = None,
    extra_instructions: str = "",
) -> list[dict[str, str]]:
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

    schema = json.dumps(TailorPlan.model_json_schema())

    user = f"""Tailor my resume to the job below. Output ONLY JSON matching the schema.

# Rules (corrections.md — follow ALL of these)
{corrections}
{rag_block}

# My background (about-me.md — ground truth)
{about}

# My experience entries (rephrase bullets; experience_bullets must align 1:1 by index)
{_experience_view(experience)}

# My projects (project_bullets align 1:1 by index)
{_projects_view(projects)}

# My real skills (only use these; you may reorder/relabel but never add new ones)
{json.dumps(real_skills, indent=2)}

# Target job
{jd_block}

# JSON schema
{schema}

# What to produce (JSON only)
- "summary": 2-3 sentence professional summary, tailored to this role, using ONLY
  facts true of me. No first person pronoun needed.
- "experience_bullets": a list aligned 1:1 with my experience entries above (same
  order, same count). For each, rephrase MY bullets to foreground relevance to the
  job and weave in priority keywords I actually have. KEEP my real numbers/metrics.
  Do NOT invent new achievements. You may drop a weak bullet but never fabricate.
- "project_bullets": aligned 1:1 with my projects (same order). Same rules.
- "skills": skill groups (e.g. label "Languages", "Frameworks", "Tools") containing
  ONLY skills I listed, reordered so job-relevant ones come first.
- "changelog": short bullets describing what you tailored.
- "missing_requirements": each job requirement I do NOT have evidence for in my
  profile. Be honest; this is expected and good.
{extra_instructions}
Return only the JSON object."""
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def _parse_plan(raw: str) -> TailorPlan:
    try:
        return TailorPlan.model_validate_json(raw)
    except ValidationError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return TailorPlan.model_validate_json(m.group(0))
        raise


def tailor(
    jd: ParsedJD,
    experience_data: dict,
    *,
    rag_snippets: list[str] | None = None,
    extra_instructions: str = "",
    num_predict: int = 4096,
) -> TailorResult:
    cfg = load_config()
    model = tailor_model(cfg)
    messages = build_messages(
        jd, experience_data, rag_snippets=rag_snippets, extra_instructions=extra_instructions
    )
    raw = llm.chat(
        messages, model=model, temperature=0.0, num_predict=num_predict,
        fmt=TailorPlan.model_json_schema(),
    )
    plan = _parse_plan(raw)

    skills = {g.label: g.items for g in plan.skills} or experience_data.get("skills", {})
    # Normalize a flat skills dict from experience.json (lists under lowercase keys).
    if not plan.skills and isinstance(experience_data.get("skills"), dict):
        skills = {k.capitalize(): v for k, v in experience_data["skills"].items() if v}

    tex = render.render_resume(
        contact=experience_data.get("contact", {}),
        summary=plan.summary,
        experience=experience_data.get("experience", []),
        education=experience_data.get("education", []),
        projects=[p for p in experience_data.get("projects", []) if (p.get("name") or "").strip()],
        skills=skills,
        experience_bullets=plan.experience_bullets,
        project_bullets=plan.project_bullets,
    )
    return TailorResult(
        tex=tex, changelog=plan.changelog,
        missing_requirements=plan.missing_requirements, plan=plan,
    )


# ------------------------------------------------------------ honesty checking
_FORBIDDEN_STOP = {
    "I", "A", "AN", "THE", "NOT", "NO", "OR", "AND", "IN", "OF", "ON", "TO",
    "WITH", "HAVE", "HAS", "NEVER", "DON'T", "DONT", "USED", "USE", "WORKED",
    "WORK", "EXPERIENCE", "PRODUCTION", "PROFESSIONALLY", "THINGS", "WILL",
    "CLAIM", "HARD", "BOUNDARY", "LIST", "ANYTHING", "MUST", "BE", "AS", "HAVING",
}


def forbidden_terms(about_me: str) -> list[str]:
    """Pull branded/proper-noun terms from the 'Things I will NOT claim' section."""
    low = about_me.lower()
    idx = low.find("not claim")
    if idx == -1:
        return []
    section = about_me[idx:]
    nl = section.find("\n## ")
    if nl != -1:
        section = section[:nl]
    terms, seen = [], set()
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9+#-]*", section):
        tok = raw.strip("-")
        if len(tok) < 2 or tok.upper() in _FORBIDDEN_STOP:
            continue
        if any(c.isupper() for c in tok) and tok.lower() not in seen:
            seen.add(tok.lower())
            terms.append(tok)
    return terms


def grounding_violations(resume_text: str, *, about_me: str, experience: dict | None = None) -> list[str]:
    """Honesty check on the rendered resume: flag any 'will NOT claim' term that
    appears. (Metadata is now rendered from the profile, so name/employer/
    placeholder drift is structurally impossible — only fabricated *content* in
    bullets/summary/skills can violate, and that's what this catches.)
    """
    violations: list[str] = []
    low = resume_text.lower()
    for term in forbidden_terms(about_me):
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(term.lower()) + r"(?![A-Za-z0-9])", low):
            violations.append(f"claims forbidden item from profile: '{term}'")
    return violations
