r"""Tailor a resume: profile + base template + corrections + parsed JD -> a
complete, compile-ready, single-column .tex, plus a changelog and a list of JD
requirements the candidate appears to be missing.

The model returns LaTeX between explicit markers (not JSON) so a full document
doesn't have to survive JSON string-escaping. A fenced-block / \documentclass
fallback parser handles models that ignore the markers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import llm
from .config import PATHS, load_config, tailor_model
from .parse import ParsedJD

TEX_START = "===TEX==="
TEX_END = "===END TEX==="
CHANGELOG = "===CHANGELOG==="
MISSING = "===MISSING==="


@dataclass
class TailorResult:
    tex: str
    changelog: list[str] = field(default_factory=list)
    missing_requirements: list[str] = field(default_factory=list)
    raw: str = ""


SYSTEM = (
    "You are an expert resume writer producing ATS-safe, single-column LaTeX "
    "resumes. You NEVER invent experience, employers, titles, dates, metrics, or "
    "skills. You only reorder, rephrase, and surface what the profile actually "
    "contains. You output a COMPLETE, compile-ready LaTeX document that compiles "
    "with Tectonic (XeTeX)."
)


def _read(path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def build_messages(
    jd: ParsedJD,
    *,
    rag_snippets: list[str] | None = None,
    extra_instructions: str = "",
) -> list[dict[str, str]]:
    corrections = _read(PATHS.corrections)
    about = _read(PATHS.about_me)
    experience = _read(PATHS.experience)
    base_tex = _read(PATHS.base_resume)
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

    user = f"""Tailor my resume to the job below.

# Rules (corrections.md — follow ALL of these)
{corrections}
{rag_block}

# My profile (ground truth — never claim anything not supported here)
## about-me.md
{about}

## experience.json
{experience}

# Base LaTeX template (keep this structure; single column; one page)
```latex
{base_tex}
```

# Target job
{jd_block}

{extra_instructions}
# Your task
Produce a COMPLETE compile-ready LaTeX document tailored to this job:
(a) FILL IN MY REAL DATA. The base template contains placeholders — you MUST
    replace every one with my actual information from experience.json:
      - contact: real name, email, phone, location, linkedin, github
      - each experience entry: real company, title, location, start/end dates
      - education: real school, degree, dates
      - projects: real project names, tech, and bullets
    The output must contain ZERO of these placeholder strings: "First Last",
    "email@example.com", "linkedin.com/in/username", "github.com/username",
    "Company", "Title", "University", "Degree", "Course One", "Project Name",
    "What it does and the impact". If I have no projects, omit the Projects
    section entirely rather than leaving the placeholder.
(b) keep the Skills section grouped as \textbf{{Languages:}}, \textbf{{Frameworks:}},
    \textbf{{Tools:}} (comma-separated real skills) — reorder so JD keywords I
    actually have surface first. Do NOT invent per-skill sub-labels.
(c) reorder and rephrase experience bullets to foreground relevant impact, and
    KEEP my real metrics (e.g. "12M requests/day", "40% latency") — do not drop them,
(d) weave priority keywords in naturally (no stuffing, no hidden text),
(e) NEVER invent anything: no fake employers, schools, skills, dates, or metrics.
    If a JD requirement isn't in my profile, leave it out (it will be flagged as
    missing). Keep it one page, single column,
(f) keep colons INSIDE bold labels and literal en-dashes (–) in date ranges.

# Output format (EXACTLY this, no extra prose)
{TEX_START}
<the full .tex from \\documentclass to \\end{{document}}>
{TEX_END}
{CHANGELOG}
- <short changelog bullet>
{MISSING}
- <each JD requirement I appear to be missing, or "- none">
"""
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def _extract_tex(raw: str) -> str:
    # 1) explicit markers
    m = re.search(re.escape(TEX_START) + r"(.*?)" + re.escape(TEX_END), raw, re.DOTALL)
    if m:
        block = m.group(1).strip()
    else:
        # 2) fenced ```latex ... ```
        m = re.search(r"```(?:latex|tex)?\s*(.*?)```", raw, re.DOTALL)
        block = m.group(1).strip() if m else raw
    # 3) trim to the actual document
    dm = re.search(r"\\documentclass.*?\\end\{document\}", block, re.DOTALL)
    return dm.group(0).strip() if dm else block.strip()


def _extract_list(raw: str, header: str, until: list[str]) -> list[str]:
    idx = raw.find(header)
    if idx == -1:
        return []
    rest = raw[idx + len(header):]
    for u in until:
        j = rest.find(u)
        if j != -1:
            rest = rest[:j]
    items = []
    for line in rest.splitlines():
        line = line.strip().lstrip("-*").strip()
        if line and line.lower() != "none":
            items.append(line)
    return items


def parse_output(raw: str) -> TailorResult:
    tex = _extract_tex(raw)
    changelog = _extract_list(raw, CHANGELOG, [MISSING, TEX_START])
    missing = _extract_list(raw, MISSING, [CHANGELOG, TEX_START])
    return TailorResult(tex=tex, changelog=changelog, missing_requirements=missing, raw=raw)


_FORBIDDEN_STOP = {
    "I", "A", "AN", "THE", "NOT", "NO", "OR", "AND", "IN", "OF", "ON", "TO",
    "WITH", "HAVE", "HAS", "NEVER", "DON'T", "DONT", "USED", "USE", "WORKED",
    "WORK", "EXPERIENCE", "PRODUCTION", "PROFESSIONALLY", "THINGS", "WILL",
    "CLAIM", "HARD", "BOUNDARY", "LIST", "ANYTHING", "MUST", "BE", "AS", "HAVING",
}


def forbidden_terms(about_me: str) -> list[str]:
    """Heuristically pull branded/proper-noun terms from the 'Things I will NOT
    claim' section of about-me.md (tokens containing an uppercase letter, minus
    common words). Used as a deterministic honesty tripwire — e.g. 'Kafka',
    'gRPC', 'IoT', 'Go'.
    """
    low = about_me.lower()
    idx = low.find("not claim")
    if idx == -1:
        return []
    section = about_me[idx:]
    # stop at the next top-level heading if any
    nl = section.find("\n## ")
    if nl != -1:
        section = section[:nl]
    terms: list[str] = []
    seen = set()
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9+#-]*", section):
        tok = raw.strip("-")
        if len(tok) < 2 or tok.upper() in _FORBIDDEN_STOP:
            continue
        if any(c.isupper() for c in tok) and tok.lower() not in seen:
            # split slash groups like gRPC/protobuf -> handled by regex already
            seen.add(tok.lower())
            terms.append(tok)
    return terms


# Unambiguous strings from base-resume.tex that must NEVER survive into output.
# (We deliberately omit ambiguous words like "Company"/"University" that can be
# substrings of real names — the "real employer present" check covers those.)
_PLACEHOLDERS = [
    "First Last", "email@example.com", "linkedin.com/in/username",
    "github.com/username", "Course One", "Course Two", "Project Name",
    "What it does and the impact", "Accomplishment with real",
    "Another bullet foregrounding", "Tech, Stack",
]


def backfill_contact(tex: str, experience: dict | None = None) -> str:
    """Deterministically replace contact-header placeholders with the real values
    from experience.json. Contact info is structured data — never trust the LLM
    with it. Idempotent; only replaces placeholders that are still present.
    """
    c = (experience or {}).get("contact", {}) or {}
    repl = {
        "First Last": c.get("name", ""),
        "email@example.com": c.get("email", ""),
        "linkedin.com/in/username": c.get("linkedin", ""),
        "github.com/username": c.get("github", ""),
    }
    for placeholder, value in repl.items():
        if value:
            # normalize linkedin/github to bare host/path (template adds https://)
            v = value.strip()
            for pre in ("https://", "http://"):
                if v.startswith(pre):
                    v = v[len(pre):]
            tex = tex.replace(placeholder, v)
    phone = c.get("phone", "")
    if phone:
        tex = re.sub(r"(?<![A-Za-z])Phone(?=\s*\$\|\$)", phone, tex)
    return tex


def grounding_violations(resume_text: str, *, about_me: str, experience: dict | None = None) -> list[str]:
    """Deterministic honesty + completeness checks on the generated resume text.

    Catches BOTH failure modes observed with local models:
      (a) fabrication — claims a 'will NOT claim' skill, or alters the real name;
      (b) laziness — leaves base-template placeholders or drops the real employer
          (i.e. didn't actually fill in the profile).
    """
    violations: list[str] = []
    low = resume_text.lower()
    experience = experience or {}

    # (a) fabrication: forbidden skills
    for term in forbidden_terms(about_me):
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(term.lower()) + r"(?![A-Za-z0-9])", low):
            violations.append(f"claims forbidden item from profile: '{term}'")

    # (a) fabrication: contact name altered/missing
    name = (experience.get("contact", {}) or {}).get("name", "").strip()
    if name and name.lower() not in low:
        violations.append(f"contact name '{name}' from profile not found (possibly altered)")

    # (b) laziness: leftover template placeholders
    for ph in _PLACEHOLDERS:
        if ph.lower() in low:
            violations.append(f"leftover template placeholder: '{ph}'")

    # (b) laziness: real employer dropped. If the profile lists employers, at
    # least one must appear (else the model kept the 'Company' placeholder or
    # invented one).
    employers = [e.get("company", "").strip() for e in experience.get("experience", []) if e.get("company", "").strip()]
    if employers and not any(emp.lower() in low for emp in employers):
        violations.append(f"no real employer from profile present (expected one of: {', '.join(employers)})")

    return violations


def tailor(
    jd: ParsedJD,
    *,
    rag_snippets: list[str] | None = None,
    extra_instructions: str = "",
    num_predict: int = 6144,
) -> TailorResult:
    cfg = load_config()
    model = tailor_model(cfg)
    messages = build_messages(jd, rag_snippets=rag_snippets, extra_instructions=extra_instructions)
    # temperature 0: tailoring must hew to the profile, not be "creative".
    raw = llm.chat(messages, model=model, temperature=0.0, num_predict=num_predict)
    result = parse_output(raw)
    if "\\documentclass" not in result.tex or "\\end{document}" not in result.tex:
        raise ValueError("Tailoring did not return a complete LaTeX document.")
    return result


def repair(tex: str, compile_error: str, *, num_predict: int = 6144) -> str:
    """Ask the model to fix a LaTeX document that failed to compile."""
    cfg = load_config()
    model = tailor_model(cfg)
    messages = [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": (
                "This LaTeX failed to compile with Tectonic (XeTeX). Return ONLY the "
                "corrected COMPLETE document between the markers, no prose.\n\n"
                f"Compile error:\n{compile_error}\n\n"
                f"{TEX_START}\n{tex}\n{TEX_END}\n"
            ),
        },
    ]
    raw = llm.chat(messages, model=model, temperature=0.0, num_predict=num_predict)
    return _extract_tex(raw)
