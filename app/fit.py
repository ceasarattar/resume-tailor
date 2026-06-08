r"""Guarantee a single-page resume.

Renders the tailored content, compiles it, counts pages, and — if it spills onto
a second page — trims the least-relevant material first (a balanced sequence:
overflow bullets → projects → extra bullets → summary → a small density nudge →
skills), recompiling after each cut until it fits on exactly one page.

The model decides what's *relevant* (selection + ordering); this module decides
how *much* fits, deterministically. That's why the output is consistently one page
without ever silently dropping the most important content.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import compile as comp
from . import render
from .tailor import Tailoring

# Initial sane caps so we don't start from an absurdly long draft (fewer compiles).
_EXP_START_CAPS = [4, 3, 3]      # by recency; deeper roles fall back to the last value
_PROJ_START_MAX = 3              # keep at most this many projects to begin with
_PROJ_BULLET_CAP = 2            # bullets per project to begin with
_SKILL_START_CAP = 10           # items per skill group to begin with

# Floors the trimmer won't cut below (keeps the resume substantive).
_EXP_FLOOR_FIRST = 2            # most-recent role keeps >= 2 bullets
_EXP_FLOOR_REST = 1            # other roles keep >= 1 bullet
_SKILL_FLOOR = 4              # each kept skill group keeps >= this many items

# Density nudges (font scale, line spread), applied only after content is minimal.
_DENSITY_LADDER = [
    {"font_scale": 1.0, "line_spread": 0.98},
    {"font_scale": 0.97, "line_spread": 0.97},
    {"font_scale": 0.94, "line_spread": 0.96},
    {"font_scale": 0.91, "line_spread": 0.95},
]


@dataclass
class FitResult:
    tex: str
    pdf_path: Path
    pages: int
    one_page: bool
    trims: list[str] = field(default_factory=list)


def _exp_floor(i: int) -> int:
    return _EXP_FLOOR_FIRST if i == 0 else _EXP_FLOOR_REST


def fit_to_one_page(
    t: Tailoring,
    out_dir: Path,
    *,
    final: bool = False,
    max_iters: int = 80,
) -> FitResult:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = out_dir / "tailored.tex"

    # Mutable working copy.
    summary = (t.summary or "").strip()
    exp_bullets = [list(b) for b in t.experience_bullets]
    proj_entries = list(t.projects)
    proj_bullets = [list(b) for b in t.project_bullets]
    skills = {k: list(v) for k, v in t.skills.items()}
    density_idx = -1  # -1 == base appearance (no density patch)
    trims: list[str] = []

    # Initial sane caps (one-time; keeps the first draft from being huge).
    for i in range(len(exp_bullets)):
        cap = _EXP_START_CAPS[min(i, len(_EXP_START_CAPS) - 1)]
        exp_bullets[i] = exp_bullets[i][:cap]
    if len(proj_entries) > _PROJ_START_MAX:
        proj_entries = proj_entries[:_PROJ_START_MAX]
        proj_bullets = proj_bullets[:_PROJ_START_MAX]
    proj_bullets = [b[:_PROJ_BULLET_CAP] for b in proj_bullets]
    skills = {k: v[:_SKILL_START_CAP] for k, v in skills.items()}

    def _density():
        return _DENSITY_LADDER[density_idx] if density_idx >= 0 else None

    def render_and_count():
        tex = render.render_resume(
            contact=t.contact,
            summary=summary,
            experience=t.experience,
            education=t.education,
            projects=proj_entries,
            skills=skills,
            experience_bullets=exp_bullets,
            project_bullets=proj_bullets,
            density=_density(),
        )
        tex_path.write_text(tex, encoding="utf-8")
        pdf = comp.compile_tex(tex_path, out_dir, final=final)
        return tex, pdf, comp.page_count(pdf)

    def next_trim() -> str | None:
        """Apply the single least-damaging next reduction; return its label, or
        None when there's nothing left to cut."""
        nonlocal summary, density_idx

        # 1. Drop an overflow experience bullet from the role with the most bullets
        #    that is still above its floor.
        best_i, best_len = -1, -1
        for i, bs in enumerate(exp_bullets):
            if len(bs) > _exp_floor(i) and len(bs) > best_len:
                best_i, best_len = i, len(bs)
        if best_i >= 0:
            exp_bullets[best_i].pop()
            return f"trimmed a bullet from experience #{best_i + 1}"

        # 2. Trim the LAST (least-relevant) project: drop its last bullet, and if it
        #    falls to zero bullets, remove the project entirely.
        for pi in range(len(proj_entries) - 1, -1, -1):
            if proj_bullets[pi]:
                proj_bullets[pi].pop()
                if not proj_bullets[pi]:
                    name = (proj_entries[pi].get("name") or "project")
                    del proj_entries[pi]
                    del proj_bullets[pi]
                    return f"dropped project '{name}'"
                return f"trimmed a bullet from a project"

        # 3. No project bullets left but project shells remain → drop them.
        if proj_entries:
            name = (proj_entries[-1].get("name") or "project")
            proj_entries.pop()
            proj_bullets.pop()
            return f"dropped project '{name}'"

        # 4. Drop the summary section.
        if summary:
            summary = ""
            return "dropped the summary section to save space"

        # 5. Nudge density down one notch.
        if density_idx < len(_DENSITY_LADDER) - 1:
            density_idx += 1
            d = _DENSITY_LADDER[density_idx]
            return f"tightened spacing (scale {d['font_scale']}, leading {d['line_spread']})"

        # 6. Last resort: shave the largest skill group above the floor.
        big_label, big_len = None, _SKILL_FLOOR
        for label, items in skills.items():
            if len(items) > big_len:
                big_label, big_len = label, len(items)
        if big_label:
            skills[big_label].pop()
            return f"trimmed a skill from '{big_label}'"

        return None

    tex, pdf, pages = render_and_count()
    it = 0
    while pages > 1 and it < max_iters:
        label = next_trim()
        if label is None:
            break
        trims.append(label)
        tex, pdf, pages = render_and_count()
        it += 1

    return FitResult(tex=tex, pdf_path=pdf, pages=pages, one_page=(pages == 1), trims=trims)
