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


class EntryTailoring(BaseModel):
    """Tailored bullets for ONE experience/project, keyed by its exact name so the
    renderer can place them with the correct employer/project (never by position).
    """
    ref: str  # exact company name (experience) or project name
    bullets: list[str] = Field(default_factory=list)


class TailorPlan(BaseModel):
    summary: str = ""
    experience: list[EntryTailoring] = Field(default_factory=list)
    projects: list[EntryTailoring] = Field(default_factory=list)
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
    position), so each entry keeps its OWN bullets. Empty when no match → the
    renderer falls back to the entry's original bullets (safe, correctly attributed).
    """
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

# My experience entries (one inner list per entry, in THIS order; within each you
# select / reorder / rewrite bullets for the job)
{_experience_view(experience)}

# My projects (one inner list per project, in THIS order)
{_projects_view(projects)}

# My real skills (only use these; you may reorder/relabel but never add new ones)
{json.dumps(real_skills, indent=2)}

# Target job
{jd_block}

# JSON schema
{schema}

# Your goal: a resume that is DISTINCTLY tailored to THIS job — visibly different
# from how you'd tailor it for a different role. Use the job's priority keywords,
# must-haves, and terminology as your lens for every choice below.

# What to produce (JSON only)
- "summary": 2-3 sentences written specifically for THIS role. Mirror the job's
  own language and foreground the 2-3 of my real strengths that matter most here.
  A different job must yield a clearly different summary.
- "experience": a list of objects, one per job, each {{"ref": <exact company name
  from above>, "bullets": [...]}}. Within each job you MUST actively tailor:
    * SELECT the bullets most relevant to this job and DROP the rest (2-4 bullets
      for a relevant role; fewer for less-relevant ones),
    * REORDER so the most job-relevant bullet is first,
    * REWRITE wording to foreground the competencies/keywords THIS job asks for,
      using the job's terminology where it's truthful.
  CRITICAL: each job's bullets must be rephrasings of ONLY that same job's source
  bullets. NEVER move an accomplishment from one job to another — that misrepresents
  where I did the work. The "ref" must exactly match the company it describes.
  HONESTY (hard limits): never change a number, metric, percentage, date, tool, or
  technology; never add a skill/tool I don't have; never invent achievements.
- "projects": a list of objects {{"ref": <exact project name>, "bullets": [...]}}.
  Include only job-relevant projects (omit the rest). Same select/reorder/rewrite
  rules, and the same hard rule: a project's bullets describe ONLY that project.
- "skills": skill groups ("Languages", "Frameworks", "Tools", optionally "Focus")
  with ONLY skills I listed, reordered so the job-relevant ones come first.
- Keep it to ONE page: ~3-4 bullets for the most relevant role, fewer for others;
  ~8 skills per group max. Be concise and high-signal.
- "changelog": what you tailored and why it fits THIS job.
- "missing_requirements": each job requirement I have no evidence for. Be honest.
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
    temperature: float | None = None,
) -> TailorResult:
    cfg = load_config()
    model = tailor_model(cfg)
    # Non-zero temperature so the model genuinely re-tailors per job instead of
    # emitting the same near-deterministic rephrasing every time. Honesty is held
    # by the prompt's hard limits + the grounding/metric checks downstream.
    if temperature is None:
        temperature = float(cfg.get("tailor_temperature", 0.4))
    messages = build_messages(
        jd, experience_data, rag_snippets=rag_snippets, extra_instructions=extra_instructions
    )
    raw = llm.chat(
        messages, model=model, temperature=temperature, num_predict=num_predict,
        fmt=TailorPlan.model_json_schema(),
    )
    plan = _parse_plan(raw)

    skills = {g.label: g.items for g in plan.skills} or experience_data.get("skills", {})
    # Normalize a flat skills dict from experience.json (lists under lowercase keys).
    if not plan.skills and isinstance(experience_data.get("skills"), dict):
        skills = {k.capitalize(): v for k, v in experience_data["skills"].items() if v}

    def _orig(entry: dict) -> list[str]:
        return [b for b in entry.get("bullets", []) if str(b).strip()]

    # Experience: map tailored bullets by name (fallback to originals), then cap
    # per position so the resume stays to one page. Most-recent role gets the most.
    exp_entries = experience_data.get("experience", [])
    exp_aligned = _aligned_bullets(exp_entries, plan.experience, "company")
    exp_caps = [4, 3, 2]
    exp_bullets = []
    for i, e in enumerate(exp_entries):
        bullets = exp_aligned[i] or _orig(e)
        exp_bullets.append(bullets[: exp_caps[min(i, len(exp_caps) - 1)]])

    # Projects: keep the ones the model selected (fallback: all), cap to 3 with <=2
    # bullets each. An omitted project is a deliberate tailoring choice → dropped.
    all_projects = [p for p in experience_data.get("projects", []) if (p.get("name") or "").strip()]
    proj_aligned = _aligned_bullets(all_projects, plan.projects, "name")
    if any(proj_aligned):
        pairs = [(p, (b or _orig(p))) for p, b in zip(all_projects, proj_aligned) if b]
    else:
        pairs = [(p, _orig(p)) for p in all_projects]
    pairs = pairs[:3]
    proj_entries = [p for p, _ in pairs]
    proj_bullets = [b[:2] for _, b in pairs]

    # Cap skills per group too.
    skills = {label: items[:10] for label, items in skills.items() if items}

    tex = render.render_resume(
        contact=experience_data.get("contact", {}),
        summary=plan.summary,
        experience=exp_entries,
        education=experience_data.get("education", []),
        projects=proj_entries,
        skills=skills,
        experience_bullets=exp_bullets,
        project_bullets=proj_bullets,
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


def _not_claim_section(about_me: str) -> tuple[str, str]:
    """Split about_me into (everything-before-NOT-claim, the-NOT-claim-section)."""
    low = about_me.lower()
    idx = low.find("not claim")
    if idx == -1:
        return about_me, ""
    # back up to the start of that heading line
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
    do NOT appear anywhere else in the profile.

    The off-limits section is free prose and often *names real things for context*
    (e.g. "no experience beyond Raila & Associates / Georgia Tech"). A token that
    also appears elsewhere in the profile is therefore something the candidate
    legitimately HAS — not forbidden. Only terms unique to the off-limits section
    (kdb+, PhD, FPGA, ...) are real tripwires.
    """
    _before, section = _not_claim_section(about_me)
    if not section:
        return []
    ctx = profile_context.lower()
    terms, seen = [], set()
    # Alphabetic tokens only, length >= 4: this avoids prose noise (sentence words,
    # acronyms like UIC/ACM, fragments like "M+") while still catching real tech
    # tripwires (Kafka, FPGA, CUDA, Hadoop, ...). Conservative on purpose — a false
    # positive here destroys real content on regeneration, so we accept a few misses.
    for raw in re.findall(r"[A-Za-z]{4,}", section):
        tok = raw
        if tok.upper() in _FORBIDDEN_STOP or not any(c.isupper() for c in tok):
            continue
        if tok.lower() in seen:
            continue
        seen.add(tok.lower())
        # Skip terms the candidate legitimately has (appear elsewhere in profile).
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(tok.lower()) + r"(?![A-Za-z0-9])", ctx):
            continue
        terms.append(tok)
    return terms


_METRIC_RE = re.compile(r"\d[\d,\.]*\s?(?:%|[MKBmkb]\+?(?![A-Za-z])|x\b)")


def _metrics(text: str) -> set[str]:
    """Normalized metric-like tokens (percentages, 2M+, 50K, 3x)."""
    return {m.group(0).replace(" ", "").lower().rstrip(".") for m in _METRIC_RE.finditer(text)}


def grounding_violations(resume_text: str, *, about_me: str, experience: dict | None = None) -> list[str]:
    """Honesty check on the rendered resume. Flags:
      (a) any genuinely off-limits term (unique to the 'will NOT claim' section);
      (b) any metric/number that does NOT appear in the profile — i.e. invented or
          inflated figures, the specific risk the candidate calls out.
    """
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
