"""Compile a .tex to PDF with Tectonic (default) or a pdflatex fallback, then run
an ATS text-layer check that the PDF is selectable, single-page, and tokenizes
cleanly.

The check cross-validates with TWO independent extractors (pypdf + pdfminer.six),
because real ATS engines vary; a source-level problem that fools one extractor
often fools others, so agreement raises confidence.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pdfminer.high_level import extract_text as _pdfminer_extract
from pypdf import PdfReader

from .config import load_config


class CompileError(RuntimeError):
    pass


@dataclass
class ATSReport:
    ok: bool
    pages: int
    issues: list[str] = field(default_factory=list)
    extractors: dict[str, str] = field(default_factory=dict)  # name -> extracted text


def _resolve_tectonic(cfg: dict) -> str:
    path = cfg.get("tectonic_path") or "tectonic"
    if path != "tectonic" and Path(path).exists():
        return path
    found = shutil.which(path) or shutil.which("tectonic")
    if not found:
        raise CompileError(
            "Tectonic not found. Set tectonic_path in config.yaml or run setup."
        )
    return found


def _pdflatex_fallback_path(cfg: dict) -> str | None:
    fb = cfg.get("pdflatex_fallback")
    if isinstance(fb, dict):
        return fb.get("path")
    return fb  # tolerate a bare string/null from older configs


def compile_tex(tex_path: Path, out_dir: Path, *, final: bool = False) -> Path:
    """Compile tex_path -> out_dir/<stem>.pdf. Returns the PDF path.

    final=True routes through the configured pdflatex fallback (for a final
    submission build); otherwise Tectonic (XeTeX) is used.
    """
    cfg = load_config()
    tex_path = Path(tex_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / (tex_path.stem + ".pdf")

    if final:
        fallback = _pdflatex_fallback_path(cfg)
        if not fallback:
            raise CompileError(
                "final=True requested but pdflatex_fallback.path is not set in config.yaml."
            )
        cmd = [fallback, "-interaction=nonstopmode", "-output-directory", str(out_dir), str(tex_path)]
    else:
        tectonic = _resolve_tectonic(cfg)
        cmd = [tectonic, "-o", str(out_dir), str(tex_path)]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not pdf_path.exists():
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-15:]
        raise CompileError(
            "LaTeX compile failed:\n" + "\n".join(tail)
        )
    return pdf_path


def compile_str(tex: str, out_dir: Path, stem: str = "resume", *, final: bool = False) -> Path:
    """Write tex to out_dir/<stem>.tex and compile it."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = out_dir / f"{stem}.tex"
    tex_path.write_text(tex, encoding="utf-8")
    return compile_tex(tex_path, out_dir, final=final)


def page_count(pdf_path: Path) -> int:
    """Number of pages in a PDF (used by the one-page fitter)."""
    return len(PdfReader(str(pdf_path)).pages)


def ats_check(pdf_path: Path) -> ATSReport:
    """Verify the PDF's text layer is ATS-safe.

    Checks: exactly one page, non-empty selectable text, no U+FFFD replacement
    chars, no spurious mid-word/label splits in expected bold labels, and that
    two independent extractors agree the text is non-trivial.
    """
    pdf_path = Path(pdf_path)
    reader = PdfReader(str(pdf_path))
    pages = len(reader.pages)
    pypdf_text = "\n".join((p.extract_text() or "") for p in reader.pages)
    try:
        pdfminer_text = _pdfminer_extract(str(pdf_path)) or ""
    except Exception as exc:  # pragma: no cover - extractor robustness
        pdfminer_text = ""
        miner_err = str(exc)
    else:
        miner_err = ""

    issues: list[str] = []
    if pages != 1:
        issues.append(f"resume should be exactly 1 page (got {pages})")
    if len(pypdf_text.strip()) < 50:
        issues.append("pypdf extracted little/no text (image-only or broken text layer?)")
    if len(pdfminer_text.strip()) < 50:
        issues.append("pdfminer extracted little/no text" + (f" ({miner_err})" if miner_err else ""))
    if "�" in pypdf_text or "�" in pdfminer_text:
        issues.append("U+FFFD replacement char present (missing ToUnicode mapping)")

    # Latin ligatures (ff/fi/fl/ffi/ffl -> U+FB00..U+FB06) extract as a single
    # glyph and break keyword search ("efficient" -> "e<ff>icient"). The template
    # disables them; flag any that slip through.
    ligatures = {chr(c) for c in range(0xFB00, 0xFB07)}
    for name, text in (("pypdf", pypdf_text), ("pdfminer", pdfminer_text)):
        present = sorted({hex(ord(c)) for c in text if c in ligatures})
        if present:
            issues.append(f"{name}: ligature codepoint(s) present {present} (set Ligatures=NoCommon)")

    # Spurious-split heuristic: a single uppercase letter followed by a space then
    # lowercase ("F rameworks") indicates a kerning/extraction artifact. Exclude
    # "A" and "I" — the only legitimate single-letter English words — so real
    # phrases like "I led a team" / "A backend role" don't false-positive.
    import re as _re

    for name, text in (("pypdf", pypdf_text), ("pdfminer", pdfminer_text)):
        for m in _re.finditer(r"\b([B-HJ-Z]) ([a-z]{2,})", text):
            issues.append(f"{name}: possible split word '{m.group(1)} {m.group(2)}'")

    report = ATSReport(
        ok=not issues,
        pages=pages,
        issues=issues,
        extractors={"pypdf": pypdf_text, "pdfminer": pdfminer_text},
    )
    return report
