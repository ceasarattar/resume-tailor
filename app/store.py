"""Persist tailored outputs and maintain applications.json (the tracker)."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import PATHS, load_config


def output_dir_for(date: str, company: str, role: str) -> Path:
    cfg = load_config()
    base = PATHS.root / cfg.get("output_dir", "outputs")
    d = base / f"{date}_{company}_{role}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_outputs(
    *,
    date: str,
    company: str,
    role: str,
    tex: str,
    pdf_path: Path,
    brief_md: str,
) -> Path:
    """Write tailored.tex, resume.pdf, jd.md into the output dir; return the dir."""
    out = output_dir_for(date, company, role)
    (out / "tailored.tex").write_text(tex, encoding="utf-8")
    (out / "jd.md").write_text(brief_md, encoding="utf-8")
    pdf_path = Path(pdf_path)
    dest = out / "resume.pdf"
    if pdf_path.resolve() != dest.resolve():
        if pdf_path.parent.resolve() == out.resolve():
            # Compiled inside the output dir (e.g. tailored.pdf) — move, don't duplicate.
            os.replace(pdf_path, dest)
        else:
            shutil.copyfile(pdf_path, dest)
    return out


def append_application(entry: dict[str, Any]) -> None:
    """Append an application record to applications.json (a JSON array)."""
    path = PATHS.applications
    records: list[dict[str, Any]] = []
    if path.exists():
        try:
            records = json.loads(path.read_text(encoding="utf-8")) or []
        except json.JSONDecodeError:
            records = []
    records.append(entry)
    path.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def append_correction(text: str, *, company: str = "", role: str = "") -> str:
    """Append a structured, dated correction line to corrections.md.

    Returns the line written. (Phase 4 will also store it as a RAG example.)
    """
    text = " ".join(text.split()).strip()
    if not text:
        raise ValueError("Correction text is empty.")
    date = datetime.now().date().isoformat()
    ctx = f" (re: {role} @ {company})" if (company or role) else ""
    line = f"- {date}{ctx}: {text}"

    path = PATHS.corrections
    content = path.read_text(encoding="utf-8") if path.exists() else "# corrections.md\n"
    if not content.endswith("\n"):
        content += "\n"
    # Append under the "## Learned corrections" section if present, else at end.
    marker = "## Learned corrections"
    if marker in content:
        content = content.rstrip("\n") + "\n" + line + "\n"
    else:
        content += f"\n{marker}\n\n{line}\n"
    path.write_text(content, encoding="utf-8")
    return line


def record(
    *,
    date: str,
    company: str,
    role: str,
    out_dir: Path,
    missing_requirements: list[str],
    keywords_used: list[str],
    status: str = "generated",
) -> dict[str, Any]:
    """Build + persist an applications.json entry; return it."""
    entry = {
        "date": date,
        "company": company,
        "role": role,
        "path": str(out_dir.relative_to(PATHS.root)),
        "status": status,
        "missing_requirements": missing_requirements,
        "keywords_used": keywords_used,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    append_application(entry)
    return entry
