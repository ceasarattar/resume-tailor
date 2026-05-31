"""Parse a raw job description into a structured brief + a compact jd.md.

Calls the local LLM with a JSON schema (Ollama `format` param) at temperature 0,
validates with Pydantic, retries once with a larger token budget on failure.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from . import llm
from .config import PATHS, load_config


class ParsedJD(BaseModel):
    title: str = Field(description="Job title / role")
    company: str = Field(description="Hiring company name; '' if not stated")
    seniority: str = Field(description="e.g. Intern, Junior, Mid, Senior, Lead")
    must_haves: list[str] = Field(default_factory=list, description="Required qualifications")
    nice_to_haves: list[str] = Field(default_factory=list, description="Preferred / bonus qualifications")
    keywords: list[str] = Field(default_factory=list, description="6-8 priority ATS keywords")


SYSTEM = (
    "You extract structured data from job descriptions. Output ONLY JSON matching "
    "the schema. Do not invent requirements that are not in the text. Pick 6-8 of "
    "the most important ATS keywords (skills, tools, technologies)."
)


def _prompt(jd_text: str) -> list[dict[str, str]]:
    schema = json.dumps(ParsedJD.model_json_schema(), indent=2)
    user = (
        f"JSON schema:\n{schema}\n\n"
        f"Job description:\n\"\"\"\n{jd_text.strip()}\n\"\"\"\n\n"
        "Return only the JSON object."
    )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def parse_jd(jd_text: str) -> ParsedJD:
    """Parse JD text into a ParsedJD. Retries once with a larger token budget."""
    cfg = load_config()
    model = cfg.get("parse_model", "qwen3:8b")
    schema = ParsedJD.model_json_schema()
    messages = _prompt(jd_text)

    last_err: Exception | None = None
    for num_predict in (1536, 3072):
        raw = llm.chat(messages, model=model, temperature=0.0, fmt=schema, num_predict=num_predict)
        try:
            return ParsedJD.model_validate_json(raw)
        except ValidationError as exc:
            last_err = exc
            # Try to salvage a JSON object embedded in extra prose.
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                try:
                    return ParsedJD.model_validate_json(match.group(0))
                except ValidationError as exc2:
                    last_err = exc2
    raise ValueError(f"Could not parse JD into valid JSON after retry: {last_err}")


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
