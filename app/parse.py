"""Parse a raw job description into a structured brief + a compact jd.md.

Calls the configured LLM with a JSON schema (structured output), validates with
Pydantic. The provider layer (app/llm.py) constrains the model to the schema, so
the result is reliable across both Claude and Ollama.
"""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

from pydantic import BaseModel, Field

from . import llm
from .config import PATHS


class ParsedJD(BaseModel):
    title: str = Field(description="Job title / role")
    company: str = Field(description="Hiring company name; '' if not stated")
    seniority: str = Field(description="e.g. Intern, Junior, Mid, Senior, Lead")
    must_haves: list[str] = Field(default_factory=list, description="Required qualifications")
    nice_to_haves: list[str] = Field(default_factory=list, description="Preferred / bonus qualifications")
    keywords: list[str] = Field(default_factory=list, description="6-8 priority ATS keywords")


SYSTEM = (
    "You extract structured data from job descriptions for a resume-tailoring tool. "
    "Be precise and faithful to the posting: do not invent requirements that are not "
    "in the text. For keywords, pick the 6-8 most important ATS terms (concrete "
    "skills, tools, technologies, and methodologies) a resume screener would scan for."
)


def parse_jd(jd_text: str) -> ParsedJD:
    """Parse JD text into a structured ParsedJD via the configured LLM provider."""
    user = (
        "Extract the structured fields from this job description.\n\n"
        f'"""\n{jd_text.strip()}\n"""'
    )
    try:
        return llm.complete_json(
            system=SYSTEM, user=user, schema_model=ParsedJD, max_tokens=1536
        )
    except llm.LLMError:
        raise
    except Exception as exc:  # validation / transport
        raise ValueError(f"Could not parse the job description into structured data: {exc}")


def _slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip()).strip("-").lower()
    return s or fallback


def slugs(jd: ParsedJD) -> tuple[str, str, str]:
    """Return (date, company_slug, role_slug) for naming outputs."""
    date = _dt.date.today().isoformat()
    return date, _slug(jd.company, "company"), _slug(jd.title, "role")


def to_brief_md(jd: ParsedJD) -> str:
    """Render a compact human-readable jd.md brief."""
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {i}" for i in items) if items else "- (none stated)"

    return (
        f"# {jd.title or 'Role'} — {jd.company or 'Company'}\n\n"
        f"**Seniority:** {jd.seniority or 'unspecified'}\n\n"
        f"**Priority keywords:** {', '.join(jd.keywords) if jd.keywords else '(none)'}\n\n"
        f"## Must-haves\n{bullets(jd.must_haves)}\n\n"
        f"## Nice-to-haves\n{bullets(jd.nice_to_haves)}\n"
    )


def save_brief(jd: ParsedJD) -> Path:
    """Write the jd.md brief to data/jobs/<date>_<company>_<role>.md and return it."""
    date, company, role = slugs(jd)
    PATHS.jobs.mkdir(parents=True, exist_ok=True)
    path = PATHS.jobs / f"{date}_{company}_{role}.md"
    path.write_text(to_brief_md(jd), encoding="utf-8")
    return path
