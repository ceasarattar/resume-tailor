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

# Your task
Produce a COMPLETE compile-ready LaTeX document tailored to this job:
(a) rewrite the summary/contact to mirror the role using only true facts,
(b) reorder/relabel skills so JD keywords surface first — only skills I actually have,
(c) reorder and rephrase experience bullets to foreground relevant impact with real metrics,
(d) weave priority keywords in naturally (no stuffing, no hidden text),
(e) NEVER invent anything; keep it one page, single column,
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


def tailor(jd: ParsedJD, *, rag_snippets: list[str] | None = None, num_predict: int = 6144) -> TailorResult:
    cfg = load_config()
    model = tailor_model(cfg)
    messages = build_messages(jd, rag_snippets=rag_snippets)
    raw = llm.chat(messages, model=model, temperature=0.2, num_predict=num_predict)
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
