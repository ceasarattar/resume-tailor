"""CLI: a job description file -> a tailored, compiled, ATS-checked PDF in outputs/.

Usage:
    python -m app.cli path/to/jd.txt [--final] [--keep-going]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import compile as comp
from . import parse as parsemod
from . import store, tailor
from .llm import OllamaError


def _info(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def run(jd_path: Path, *, final: bool = False, keep_going: bool = False) -> int:
    jd_text = Path(jd_path).read_text(encoding="utf-8")
    if len(jd_text.strip()) < 30:
        print("ERROR: JD file looks empty.", file=sys.stderr)
        return 2

    _info("Parsing job description...")
    jd = parsemod.parse_jd(jd_text)
    date, company, role = parsemod.slugs(jd)
    _info(f"Parsed: {jd.title or '?'} @ {jd.company or '?'} ({jd.seniority or '?'})")

    brief_md = parsemod.to_brief_md(jd)
    parsemod.save_brief(jd)

    _info("Tailoring resume (this can take a minute on the local model)...")
    result = tailor.tailor(jd)

    out_dir = store.output_dir_for(date, company, role)
    tex_path = out_dir / "tailored.tex"
    tex_path.write_text(result.tex, encoding="utf-8")

    _info("Compiling with Tectonic...")
    try:
        pdf_path = comp.compile_tex(tex_path, out_dir, final=final)
    except comp.CompileError as exc:
        _info("Compile failed; asking the model to repair once...")
        fixed = tailor.repair(result.tex, str(exc))
        tex_path.write_text(fixed, encoding="utf-8")
        result.tex = fixed
        pdf_path = comp.compile_tex(tex_path, out_dir, final=final)

    _info("Running ATS text-layer check...")
    report = comp.ats_check(pdf_path)
    if report.ok:
        _info(f"ATS check passed ({report.pages} page).")
    else:
        print("WARN: ATS check found issues:", file=sys.stderr)
        for issue in report.issues:
            print(f"  - {issue}", file=sys.stderr)
        if not keep_going:
            print("Re-run with --keep-going to store anyway.", file=sys.stderr)
            return 1

    out_dir = store.write_outputs(
        date=date, company=company, role=role,
        tex=result.tex, pdf_path=pdf_path, brief_md=brief_md,
    )
    store.record(
        date=date, company=company, role=role, out_dir=out_dir,
        missing_requirements=result.missing_requirements,
        keywords_used=jd.keywords,
    )

    _info(f"Done. Output: {out_dir}")
    if result.changelog:
        print("\nChangelog:")
        for c in result.changelog:
            print(f"  - {c}")
    if result.missing_requirements:
        print("\nPossibly missing requirements (flagged, not faked):")
        for m in result.missing_requirements:
            print(f"  - {m}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tailor a resume to a job description.")
    ap.add_argument("jd", type=Path, help="Path to a job-description text file")
    ap.add_argument("--final", action="store_true", help="Use the pdflatex fallback renderer")
    ap.add_argument("--keep-going", action="store_true", help="Store even if the ATS check fails")
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
