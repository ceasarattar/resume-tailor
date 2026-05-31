"""CLI: a job description file -> a tailored, compiled, ATS-checked PDF in outputs/.

Usage:
    python -m app.cli path/to/jd.txt [--final] [--keep-going]

The orchestration lives in `generate_resume()` so the FastAPI server (app.main)
and this CLI share exactly one pipeline path.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import compile as comp
from . import parse as parsemod
from . import store, tailor
from .llm import OllamaError


@dataclass
class GenerateResult:
    date: str
    company: str
    role: str
    out_dir: Path
    pdf_path: Path
    changelog: list[str] = field(default_factory=list)
    missing_requirements: list[str] = field(default_factory=list)
    keywords_used: list[str] = field(default_factory=list)
    ats_ok: bool = True
    ats_issues: list[str] = field(default_factory=list)


def generate_resume(
    jd_text: str,
    *,
    company: str | None = None,
    role: str | None = None,
    final: bool = False,
) -> GenerateResult:
    """Run the full pipeline on raw JD text and return a structured result.

    `company` / `role` optionally override what the parser inferred (used for
    output naming and the resume's target). No printing — callers render output.
    """
    if len(jd_text.strip()) < 30:
        raise ValueError("Job description looks empty.")

    jd = parsemod.parse_jd(jd_text)
    if company:
        jd.company = company
    if role:
        jd.title = role
    date, company_slug, role_slug = parsemod.slugs(jd)

    brief_md = parsemod.to_brief_md(jd)
    parsemod.save_brief(jd)

    result = tailor.tailor(jd)

    out_dir = store.output_dir_for(date, company_slug, role_slug)
    tex_path = out_dir / "tailored.tex"
    tex_path.write_text(result.tex, encoding="utf-8")

    try:
        pdf_path = comp.compile_tex(tex_path, out_dir, final=final)
    except comp.CompileError as exc:
        fixed = tailor.repair(result.tex, str(exc))
        tex_path.write_text(fixed, encoding="utf-8")
        result.tex = fixed
        pdf_path = comp.compile_tex(tex_path, out_dir, final=final)

    report = comp.ats_check(pdf_path)

    out_dir = store.write_outputs(
        date=date, company=company_slug, role=role_slug,
        tex=result.tex, pdf_path=pdf_path, brief_md=brief_md,
    )
    store.record(
        date=date, company=company_slug, role=role_slug, out_dir=out_dir,
        missing_requirements=result.missing_requirements,
        keywords_used=jd.keywords,
        status="generated" if report.ok else "generated_with_warnings",
    )

    return GenerateResult(
        date=date, company=company_slug, role=role_slug,
        out_dir=out_dir, pdf_path=out_dir / "resume.pdf",
        changelog=result.changelog,
        missing_requirements=result.missing_requirements,
        keywords_used=jd.keywords,
        ats_ok=report.ok, ats_issues=report.issues,
    )


def run(jd_path: Path, *, final: bool = False, keep_going: bool = False) -> int:
    jd_text = Path(jd_path).read_text(encoding="utf-8")
    print("==> Generating (parse -> tailor -> compile -> store)...", flush=True)
    res = generate_resume(jd_text, final=final)

    if res.ats_ok:
        print(f"==> ATS check passed ({res.out_dir.name}).")
    else:
        print("WARN: ATS check found issues:", file=sys.stderr)
        for issue in res.ats_issues:
            print(f"  - {issue}", file=sys.stderr)
        if not keep_going:
            print("(stored anyway; use the output with care)", file=sys.stderr)

    print(f"==> Done. Output: {res.out_dir}")
    if res.changelog:
        print("\nChangelog:")
        for c in res.changelog:
            print(f"  - {c}")
    if res.missing_requirements:
        print("\nPossibly missing requirements (flagged, not faked):")
        for m in res.missing_requirements:
            print(f"  - {m}")
    return 0 if (res.ats_ok or keep_going) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tailor a resume to a job description.")
    ap.add_argument("jd", type=Path, help="Path to a job-description text file")
    ap.add_argument("--final", action="store_true", help="Use the pdflatex fallback renderer")
    ap.add_argument("--keep-going", action="store_true", help="Exit 0 even if the ATS check fails")
    args = ap.parse_args(argv)
    try:
        return run(args.jd, final=args.final, keep_going=args.keep_going)
    except OllamaError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except (ValueError, comp.CompileError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
