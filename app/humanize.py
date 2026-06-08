r"""Humanizer — make the tailored resume read like a person wrote it.

Two layers:

1. A deterministic AI-tell linter (`find_ai_tells`) based on Wikipedia's
   "Signs of AI writing" guide: corporate/LLM vocabulary, filler phrases,
   em-dash overuse, passive voice, rule-of-three padding, weak openings.

2. An LLM rewrite pass (`humanize`) that rewrites the summary + every bullet into
   natural, concrete, senior-engineer English — short active sentences, strong
   varied verbs, no fluff — while preserving EVERY fact, metric, date, and
   technology verbatim. A deterministic metric guard reverts any line where a
   number was dropped or changed, so the humanizer can never alter the facts.

The result feeds the fitter (app/fit.py) for one-page rendering. If the LLM pass
fails for any reason, the original (already-tailored) content is returned — the
humanizer never breaks generation.
"""
from __future__ import annotations

import re
from dataclasses import replace

from pydantic import BaseModel, Field

from . import llm
from .tailor import Tailoring


# ----------------------------------------------------------------- the AI tells
# High-signal patterns only — each would make a recruiter or reader think "AI/boilerplate".
# (word/phrase regex, short label). Word patterns use \b...\b; case-insensitive.
_TELLS: list[tuple[str, str]] = [
    (r"\butili[sz]e(?:d|s)?\b", "utilize -> used"),
    (r"\bleverag(?:e|ed|es|ing)\b", "leverage -> used/built with"),
    (r"\bin order to\b", "in order to -> to"),
    (r"\bresponsible for\b", "responsible for -> led/built/owned"),
    (r"\bensur(?:e|ed|es|ing)\b", "ensure/ensuring (filler)"),
    (r"\bseamless(?:ly)?\b", "seamless (fluff)"),
    (r"\brobust\b", "robust (fluff)"),
    (r"\bspearhead(?:ed|ing|s)?\b", "spearheaded (cliché)"),
    (r"\borchestrat(?:e|ed|es|ing)\b", "orchestrated (cliché)"),
    (r"\bshowcas(?:e|ed|es|ing)\b", "showcase (cliché)"),
    (r"\bcutting[- ]edge\b", "cutting-edge (cliché)"),
    (r"\bstate[- ]of[- ]the[- ]art\b", "state-of-the-art (cliché)"),
    (r"\bfacilitat(?:e|ed|es|ing)\b", "facilitate -> ran/enabled"),
    (r"\bstreamlin(?:e|ed|es|ing)\b", "streamline (cliché)"),
    (r"\bdelv(?:e|ed|es|ing)\b", "delve (AI tell)"),
    (r"\b(?:testament|tapestry|realm|landscape)\b", "AI vocab (testament/tapestry/realm/landscape)"),
    (r"\b(?:pivotal|paramount)\b", "pivotal/paramount (inflated)"),
    (r"\bplays? a (?:key|vital|crucial|pivotal|critical|significant) role\b", "'plays a key role' filler"),
    (r"\bworth noting\b", "'worth noting' filler"),
    (r"\bwide range of\b", "'a wide range of' (vague)"),
    (r"\bnot only\b.*\bbut also\b", "not only…but also (negative parallelism)"),
    (r"\bvarious\b", "various (vague)"),
    (r"\bnumerous\b", "numerous (vague)"),
    (r"\bhighly\b", "highly (empty intensifier)"),
    (r"\bsuccessfully\b", "successfully (empty)"),
    (r"\bpassionate\b", "passionate (cliché)"),
    (r"\bsynerg(?:y|ies|istic)\b", "synergy (corporate)"),
    (r"\bcomprehensive\b", "comprehensive (inflated)"),
    (r"\bmeticulous(?:ly)?\b", "meticulous (inflated)"),
]
_TELL_RES = [(re.compile(p, re.IGNORECASE), label) for p, label in _TELLS]

# Weak verbs to avoid as the FIRST word of a bullet.
_WEAK_OPENERS = {
    "worked", "helped", "assisted", "participated", "involved", "responsible",
    "tasked", "various", "successfully", "utilized", "leveraged",
}

_METRIC_RE = re.compile(r"\d[\d,\.]*\s?%?")  # any number, incl. percentages


def find_ai_tells(text: str) -> list[str]:
    """Return labels for AI/boilerplate tells found in `text`."""
    if not text:
        return []
    hits: list[str] = []
    for rx, label in _TELL_RES:
        if rx.search(text):
            hits.append(label)
    # Em-dash overuse: more than one em-dash in a single line reads as AI.
    if text.count("—") + text.count(" -- ") >= 2:
        hits.append("em-dash overuse")
    # Weak opener.
    first = re.findall(r"[A-Za-z']+", text)
    if first and first[0].lower() in _WEAK_OPENERS:
        hits.append(f"weak opener '{first[0]}'")
    return hits


def _numbers(text: str) -> list[str]:
    return [m.group(0).replace(" ", "") for m in _METRIC_RE.finditer(text or "")]


def _metrics_preserved(original: str, rewritten: str) -> bool:
    """True if every number in `original` still appears in `rewritten`."""
    orig = _numbers(original)
    rew = set(_numbers(rewritten))
    return all(n in rew for n in orig)


# --------------------------------------------------------------- LLM rewrite pass
class _Line(BaseModel):
    id: int
    text: str


class _Rewrite(BaseModel):
    lines: list[_Line] = Field(default_factory=list)


_SYSTEM = (
    "You are a ruthless resume line editor. You rewrite each numbered line so it "
    "reads like a sharp senior engineer wrote it — natural, concrete, human — and "
    "strip every sign of AI/boilerplate writing.\n\n"
    "RULES:\n"
    "- Preserve meaning and EVERY fact: keep all numbers, percentages, metrics, "
    "dates, product names, and technologies EXACTLY as given. Never add a fact, "
    "metric, skill, or technology that isn't already in the line.\n"
    "- Start each bullet with a strong, specific past-tense verb; vary verbs across "
    "lines (don't start three bullets with the same word).\n"
    "- Short, active, direct. Cut filler. One line each (aim under ~165 characters).\n"
    "- BANNED words/phrases: utilize, leverage, in order to, responsible for, "
    "seamless, robust, ensure/ensuring, showcase, spearheaded, orchestrated, "
    "cutting-edge, state-of-the-art, facilitate, streamline, delve, pivotal, "
    "'plays a key role', 'a wide range of', 'not only … but also', highly, various, "
    "numerous, successfully, passionate, synergy, comprehensive, meticulous.\n"
    "- No rule-of-three adjective padding, no empty intensifiers, no em-dash pile-ups.\n"
    "- The first line (id 0) is the professional summary: 2-3 sentences, first person "
    "implied (no 'I'), confident and specific. Every other line is a resume bullet.\n"
    "- Return one rewritten line per input id, same ids. Do not merge or drop lines."
)


def _llm_rewrite(numbered: list[tuple[int, str]], max_tokens: int) -> dict[int, str]:
    listing = "\n".join(f"[{i}] {t}" for i, t in numbered)
    user = (
        "Rewrite each line below. Keep the same id for each.\n\n"
        f"{listing}"
    )
    out = llm.complete_json(
        system=_SYSTEM, user=user, schema_model=_Rewrite, max_tokens=max_tokens
    )
    return {ln.id: ln.text.strip() for ln in out.lines if ln.text and ln.text.strip()}


def humanize(t: Tailoring, *, max_tokens: int = 3072) -> Tailoring:
    """Rewrite the summary + all bullets to read naturally, preserving every fact.

    Best-effort: on any failure, returns `t` unchanged. A metric guard reverts any
    line whose numbers were altered, so honesty cannot regress here.
    """
    # Flatten everything into a numbered list with a stable index map.
    numbered: list[tuple[int, str]] = []
    index: list[tuple[str, int, int]] = []  # (kind, outer, inner) per id
    nid = 0

    if (t.summary or "").strip():
        numbered.append((nid, t.summary.strip()))
        index.append(("summary", -1, -1))
        nid += 1

    for ei, bullets in enumerate(t.experience_bullets):
        for bi, b in enumerate(bullets):
            numbered.append((nid, b))
            index.append(("exp", ei, bi))
            nid += 1
    for pi, bullets in enumerate(t.project_bullets):
        for bi, b in enumerate(bullets):
            numbered.append((nid, b))
            index.append(("proj", pi, bi))
            nid += 1

    if not numbered:
        return t

    try:
        rewritten = _llm_rewrite(numbered, max_tokens)
    except Exception:
        return t  # never break generation on a humanizer failure

    # Apply rewrites with the metric guard.
    new_summary = t.summary
    new_exp = [list(b) for b in t.experience_bullets]
    new_proj = [list(b) for b in t.project_bullets]

    for the_id, original in numbered:
        new_text = rewritten.get(the_id)
        if not new_text:
            continue
        if not _metrics_preserved(original, new_text):
            continue  # numbers changed -> keep the original line (honesty guard)
        kind, outer, inner = index[the_id]
        if kind == "summary":
            new_summary = new_text
        elif kind == "exp":
            new_exp[outer][inner] = new_text
        elif kind == "proj":
            new_proj[outer][inner] = new_text

    return replace(
        t, summary=new_summary, experience_bullets=new_exp, project_bullets=new_proj
    )


def remaining_tells(t: Tailoring) -> list[str]:
    """All AI tells still present across the summary + bullets (for reporting)."""
    tells: list[str] = []
    for label in find_ai_tells(t.summary):
        tells.append(f"summary: {label}")
    for bullets in t.experience_bullets + t.project_bullets:
        for b in bullets:
            for label in find_ai_tells(b):
                tells.append(label)
    # De-dup while preserving order.
    seen, out = set(), []
    for x in tells:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
