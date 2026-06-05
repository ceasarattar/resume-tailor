"""Deterministically render a resume .tex from structured data.

The LLM is unreliable at emitting a whole LaTeX document — it corrupts braces
(e.g. `label={}]}` -> `label={}}}`) and drifts on dates/contact. So instead the
model returns structured CONTENT (summary + rephrased bullets + skill ordering),
and THIS module renders the .tex: it reuses the tested preamble/macros from
templates/base-resume.tex and fills the body from `profile/experience.json`
(metadata: name, employers, titles, dates, school) plus the model's content.

This guarantees: valid braces, correct/real metadata, proper LaTeX escaping.
"""
from __future__ import annotations

from .config import PATHS

# LaTeX special characters -> escaped forms (order-independent, char by char).
_LATEX_ESCAPE = {
    "\\": r"\textbackslash{}",
    "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}


def esc(text) -> str:
    """Escape a string for LaTeX (safe for any user/model content)."""
    if text is None:
        return ""
    return "".join(_LATEX_ESCAPE.get(ch, ch) for ch in str(text))


def _preamble() -> str:
    """Everything up to and including \\begin{document} from base-resume.tex,
    so the renderer reuses the audited, ATS-safe preamble and macros.
    """
    tex = PATHS.base_resume.read_text(encoding="utf-8")
    marker = r"\begin{document}"
    head = tex.split(marker, 1)[0]
    return head + marker + "\n"


def _strip_scheme(url: str) -> str:
    u = (url or "").strip()
    for pre in ("https://", "http://"):
        if u.startswith(pre):
            u = u[len(pre):]
    return u


def _contact_line(contact: dict) -> str:
    """Build the body contact line (phone | email | linkedin | github | site)."""
    parts: list[str] = []
    if contact.get("phone"):
        parts.append(esc(contact["phone"]))
    if contact.get("email"):
        e = esc(contact["email"])
        parts.append(rf"\href{{mailto:{e}}}{{\underline{{{e}}}}}")
    for key in ("linkedin", "github", "website"):
        val = contact.get(key)
        if val:
            disp = esc(_strip_scheme(val))
            parts.append(rf"\href{{https://{disp}}}{{\underline{{{disp}}}}}")
    return " $|$\n    ".join(parts)


def _dates(start: str, end: str) -> str:
    start, end = (start or "").strip(), (end or "").strip()
    if start and end:
        return f"{esc(start)} – {esc(end)}"  # literal en-dash
    return esc(start or end)


def _experience_section(entries: list[dict], bullets_override: list[list[str]] | None) -> str:
    if not entries:
        return ""
    out = ["\\section{Experience}", "  \\resumeSubHeadingListStart"]
    for i, e in enumerate(entries):
        bullets = e.get("bullets", [])
        if bullets_override and i < len(bullets_override) and bullets_override[i]:
            bullets = bullets_override[i]
        out.append(
            f"    \\resumeSubheading\n"
            f"      {{{esc(e.get('company'))}}}{{{_dates(e.get('start',''), e.get('end',''))}}}\n"
            f"      {{{esc(e.get('title'))}}}{{{esc(e.get('location'))}}}"
        )
        bullets = [b for b in bullets if str(b).strip()]
        if bullets:
            out.append("      \\resumeItemListStart")
            for b in bullets:
                out.append(f"        \\resumeItem{{{esc(b)}}}")
            out.append("      \\resumeItemListEnd")
    out.append("  \\resumeSubHeadingListEnd")
    return "\n".join(out)


def _education_section(entries: list[dict]) -> str:
    if not entries:
        return ""
    out = ["\\section{Education}", "  \\resumeSubHeadingListStart"]
    for e in entries:
        out.append(
            f"    \\resumeSubheading\n"
            f"      {{{esc(e.get('school'))}}}{{{esc(e.get('location'))}}}\n"
            f"      {{{esc(e.get('degree'))}}}{{{_dates(e.get('start',''), e.get('end',''))}}}"
        )
        notes = (e.get("notes") or "").strip()
        if notes:
            out.append("      \\resumeItemListStart")
            out.append(f"        \\resumeItem{{{esc(notes)}}}")
            out.append("      \\resumeItemListEnd")
    out.append("  \\resumeSubHeadingListEnd")
    return "\n".join(out)


def _projects_section(projects: list[dict], bullets_override: list[list[str]] | None) -> str:
    projects = [p for p in projects if (p.get("name") or "").strip()]
    if not projects:
        return ""
    out = ["\\section{Projects}", "    \\resumeSubHeadingListStart"]
    for i, p in enumerate(projects):
        tech = ", ".join(p.get("tech", []) or [])
        heading = rf"\textbf{{{esc(p.get('name'))}}}"
        if tech:
            heading += rf" $|$ \emph{{{esc(tech)}}}"
        date = esc(p.get("date", ""))
        out.append(f"      \\resumeProjectHeading\n          {{{heading}}}{{{date}}}")
        bullets = p.get("bullets", [])
        if bullets_override and i < len(bullets_override) and bullets_override[i]:
            bullets = bullets_override[i]
        bullets = [b for b in bullets if str(b).strip()]
        if bullets:
            out.append("          \\resumeItemListStart")
            for b in bullets:
                out.append(f"            \\resumeItem{{{esc(b)}}}")
            out.append("          \\resumeItemListEnd")
    out.append("    \\resumeSubHeadingListEnd")
    return "\n".join(out)


def _skills_section(skills: dict) -> str:
    """skills: ordered dict of label -> list[str] (e.g. {'Languages': [...]})."""
    rows = []
    for label, items in skills.items():
        items = [str(s).strip() for s in (items or []) if str(s).strip()]
        if items:
            rows.append(rf"     \textbf{{{esc(label)}:}} {esc(', '.join(items))} \\")
    if not rows:
        return ""
    body = "\n".join(rows)
    return (
        "\\section{Skills}\n"
        " \\begin{itemize}[leftmargin=0.15in, label={}]\n"
        "    \\small{\\item{\n"
        f"{body}\n"
        "    }}\n"
        " \\end{itemize}"
    )


def render_resume(
    *,
    contact: dict,
    summary: str = "",
    experience: list[dict],
    education: list[dict],
    projects: list[dict],
    skills: dict,
    experience_bullets: list[list[str]] | None = None,
    project_bullets: list[list[str]] | None = None,
) -> str:
    """Assemble the full compile-ready .tex."""
    name = esc(contact.get("name") or "Your Name")
    body_parts = [
        "%----------HEADING----------",
        "\\begin{center}",
        f"    \\textbf{{\\Huge \\scshape {name}}} \\\\ \\vspace{{1pt}}",
        f"    \\small {_contact_line(contact)}",
        "\\end{center}",
        "",
    ]
    if summary.strip():
        body_parts += ["\\section{Summary}", f"{esc(summary.strip())}\n\\vspace{{-6pt}}", ""]
    for section in (
        _experience_section(experience, experience_bullets),
        _education_section(education),
        _projects_section(projects, project_bullets),
        _skills_section(skills),
    ):
        if section:
            body_parts += [section, ""]
    body = "\n".join(body_parts)
    return _preamble() + "\n" + body + "\n\\end{document}\n"
