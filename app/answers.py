"""Answer bank: resolve a job-application form field to a truthful answer.

This is the "delivery-half" counterpart to tailor.py. Given one form field
(label + type + options), `resolve()` returns the value to fill, a confidence,
and whether a human should review it. Resolution is tiered, cheapest first:

  1. Learned bank   — exact match on a field signature you've answered before.
  2. Profile map    — deterministic rules over your profile/application facts
                      (name, email, work authorization, EEO, salary, ...).
  3. Embedding bank — fuzzy match to a past answer (best-effort, Ollama embed).
  4. Grounded LLM   — only for free-text questions; reuses the honesty rules so
                      it can rephrase but never invent facts not in profile/.

The same honesty contract as the resume holds: we never guess work authorization,
never fabricate metrics or skills, and EEO fields default to "decline". Anything
we can't ground is returned with needs_review=True instead of a made-up value.

The bank LEARNS: when the extension reports what was actually submitted, `learn()`
upserts those answers so the next form for the same question is instant.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from . import config as cfg
from . import llm
from .config import PATHS


# --------------------------------------------------------------------- profile
def _read_json(path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_profile() -> dict:
    """Merge experience.json contact + application.json into one structured profile.

    application.json (gitignored, personal) is the source for application facts;
    if absent we fall back to application.example.json so the app still boots.
    """
    exp = _read_json(PATHS.experience)
    app = _read_json(PATHS.application) or _read_json(PATHS.application_example)
    contact = exp.get("contact", {}) or {}

    ident = app.get("identity", {}) or {}
    name = (contact.get("name") or "").strip()
    first = ident.get("first_name") or (name.split()[0] if name else "")
    last = ident.get("last_name") or (name.split()[-1] if len(name.split()) > 1 else "")

    return {
        "contact": contact,
        "identity": {
            "full_name": name,
            "first_name": first,
            "last_name": last,
            "preferred_name": ident.get("preferred_name") or "",
            "pronouns": ident.get("pronouns") or "",
        },
        "address": app.get("address", {}) or {},
        "work_authorization": app.get("work_authorization", {}) or {},
        "preferences": app.get("preferences", {}) or {},
        "screening": app.get("screening", {}) or {},
        "eeo": app.get("eeo", {}) or {},
        "documents": app.get("documents", {}) or {},
        "defaults": app.get("defaults", {}) or {},
    }


def _https(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    return url if url.startswith(("http://", "https://")) else "https://" + url


# --------------------------------------------------------- signature / matching
_STOP = re.compile(r"[\*∗]|\(required\)|\(optional\)|required|optional", re.I)


def signature(label: str, field_type: str = "") -> str:
    """Normalize a field label into a stable signature for bank lookup.

    Lowercase, drop punctuation / required-markers / extra whitespace, so
    "First Name *" and "first name" collide. field_type is folded in so a
    text "name" and a file "name" don't.
    """
    s = (label or "").lower()
    s = _STOP.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"\s+", " ", s)
    ft = (field_type or "").lower().strip()
    return f"{ft}:{s}" if ft else s


def _bool_to_text(v: bool) -> str:
    return "Yes" if v else "No"


# Deterministic profile rules. Each entry: (compiled keyword regex, resolver).
# Resolver returns (value, kind) where kind is "text" or "bool"; None = no answer.
def _builtin_value(sig_text: str, p: dict):
    """Map a normalized label to a profile value. Returns (value, kind) or (None, None).

    kind "bool" means value is True/False/None (None = unknown -> needs review).
    Order matters: most specific patterns first.
    """
    c = p["contact"]
    ident = p["identity"]
    addr = p["address"]
    wa = p["work_authorization"]
    pref = p["preferences"]
    scr = p["screening"]
    eeo = p["eeo"]
    s = f" {sig_text} "  # pad so word-ish checks are safe

    def has(*words):
        return any(w in s for w in words)

    # --- identity / contact ---
    if has(" first name", " given name", " legal first", " forename"):
        return ident["first_name"], "text"
    if has(" last name", " family name", " surname", " legal last"):
        return ident["last_name"], "text"
    if has(" preferred name", " nickname", " goes by"):
        return ident["preferred_name"] or ident["first_name"], "text"
    if has(" pronoun"):
        return ident["pronouns"], "text"
    if has(" full name", " your name", " candidate name") or sig_text.strip() in ("name", "text name"):
        return ident["full_name"], "text"
    if has(" email", " e mail"):
        return c.get("email", ""), "text"
    if has(" phone", " mobile", " telephone", " cell"):
        return c.get("phone", ""), "text"
    if has(" linkedin"):
        return _https(c.get("linkedin", "")), "text"
    if has(" github"):
        return _https(c.get("github", "")), "text"
    if has(" portfolio", " website", " personal site", " personal url", " web site"):
        return _https(c.get("website", "")), "text"

    # --- address ---
    if has(" street", " address line 1", " address line", " mailing address") or s.strip() == "address":
        return addr.get("street", ""), "text"
    if has(" city", " town"):
        return addr.get("city", ""), "text"
    if has(" state", " province", " region"):
        return addr.get("state", ""), "text"
    if has(" zip", " postal", " post code", " postcode"):
        return addr.get("postal_code", ""), "text"
    if has(" country"):
        return addr.get("country", ""), "text"
    if has(" location", " where are you", " based in", " current location"):
        return c.get("location", ""), "text"

    # --- work authorization (never guessed: bool may be None) ---
    if has(" sponsorship", " sponsor", " require visa", " need visa"):
        v = wa.get("require_visa_sponsorship_now_or_future")
        return (None if v is None else bool(v)), "bool"
    if has(" authorized to work", " legally authorized", " right to work",
           " work authorization", " eligible to work", " authorized for employment"):
        v = wa.get("authorized_to_work_in_us")
        return (None if v is None else bool(v)), "bool"
    if has(" visa status", " immigration status", " citizenship status"):
        return wa.get("visa_status", ""), "text"

    # --- preferences / screening ---
    if has(" salary", " compensation expectation", " expected pay", " desired pay",
           " pay expectation", " expected salary", " desired compensation"):
        return pref.get("desired_salary", ""), "text"
    if has(" relocate", " relocation", " willing to move"):
        v = pref.get("willing_to_relocate")
        return (None if v is None else bool(v)), "bool"
    if has(" remote", " work from home", " onsite preference", " hybrid"):
        return pref.get("remote_preference", ""), "text"
    if has(" start date", " available to start", " earliest start", " availability date", " when can you start"):
        return pref.get("earliest_start_date", ""), "text"
    if has(" notice period", " notice required"):
        return pref.get("notice_period", ""), "text"
    if has(" currently employed", " current employer"):
        v = pref.get("currently_employed")
        return (None if v is None else bool(v)), "bool"
    if has(" 18 ", " at least 18", " over 18", " eighteen", " legal working age", " age requirement"):
        v = scr.get("are_you_at_least_18")
        return (None if v is None else bool(v)), "bool"
    if has(" how did you hear", " referral source", " hear about", " source of application"):
        return scr.get("how_did_you_hear", ""), "text"
    if has(" previously employed", " worked here before", " former employee"):
        v = scr.get("previously_employed_here")
        return (None if v is None else bool(v)), "bool"

    # --- EEO / demographics (default = decline, always safe + honest) ---
    if has(" gender", " sex "):
        return eeo.get("gender", "Decline to self-identify"), "text"
    if has(" hispanic", " latino", " latinx"):
        return eeo.get("hispanic_or_latino", "Decline to self-identify"), "text"
    if has(" race", " ethnicity", " ethnic"):
        return eeo.get("race_ethnicity", "Decline to self-identify"), "text"
    if has(" veteran", " military service", " protected veteran"):
        return eeo.get("veteran_status", "I am not a protected veteran"), "text"
    if has(" disability", " disabled"):
        return eeo.get("disability_status", "I do not wish to answer"), "text"

    return None, None


# ----------------------------------------------------- option (select) matching
_DECLINE = ("decline", "prefer not", "do not wish", "not wish", "rather not", "no answer")


def match_option(value: Any, kind: str, options: list[str]) -> str | None:
    """Pick the option string that best represents `value`. None if no good match."""
    if not options:
        return None
    norm = [(o, re.sub(r"[^a-z0-9]+", " ", str(o).lower()).strip()) for o in options]

    # Booleans -> yes/no option.
    if kind == "bool":
        if value is None:
            return None
        want_yes = bool(value)
        for o, n in norm:
            if want_yes and re.search(r"\byes\b|\btrue\b|\bi am\b|\bi do\b", n):
                return o
            if not want_yes and re.search(r"\bno\b|\bfalse\b|\bi am not\b|\bi do not\b|\bnot\b", n):
                return o
        # Fall through to text matching on "Yes"/"No".
        value = _bool_to_text(value)

    v = re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()
    if not v:
        return None

    # "Decline"-style values map to any decline-ish option.
    if any(d in v for d in _DECLINE):
        for o, n in norm:
            if any(d in n for d in _DECLINE):
                return o

    # Exact, then contains, then token-overlap.
    for o, n in norm:
        if n == v:
            return o
    for o, n in norm:
        if v and (v in n or n in v):
            return o
    vt = set(v.split())
    best, best_score = None, 0.0
    for o, n in norm:
        nt = set(n.split())
        if not nt:
            continue
        score = len(vt & nt) / len(vt | nt)
        if score > best_score:
            best, best_score = o, score
    return best if best_score >= 0.5 else None


# ------------------------------------------------------------------- SQLite bank
def _conn() -> sqlite3.Connection:
    PATHS.answers_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(PATHS.answers_db))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS answers (
            sig TEXT NOT NULL,
            host TEXT NOT NULL DEFAULT '',
            label TEXT,
            field_type TEXT,
            value TEXT,
            source TEXT,
            hits INTEGER DEFAULT 1,
            updated_at TEXT,
            embedding TEXT,
            PRIMARY KEY (sig, host)
        )"""
    )
    return conn


def _host_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    host = (m.group(1) if m else "").lower()
    # Collapse Workday tenant subdomains so learning generalizes across one ATS.
    if host.endswith("myworkdayjobs.com") or "workday" in host:
        return "myworkdayjobs.com"
    for known in ("greenhouse.io", "lever.co", "ashbyhq.com", "icims.com", "smartrecruiters.com"):
        if host.endswith(known):
            return known
    return host


def _bank_lookup(sig: str, host: str) -> dict | None:
    conn = _conn()
    try:
        for h in (host, ""):  # host-specific first, then global
            row = conn.execute(
                "SELECT value, field_type, source FROM answers WHERE sig=? AND host=?",
                (sig, h),
            ).fetchone()
            if row and row[0] is not None:
                return {"value": row[0], "field_type": row[1], "source": "bank", "host": h}
        return None
    finally:
        conn.close()


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _bank_fuzzy(label: str, host: str, threshold: float = 0.86) -> dict | None:
    """Best-effort embedding match against learned answers. Silent on any failure."""
    try:
        q = llm.embed(label)
    except Exception:
        return None
    if not q:
        return None
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT sig, value, field_type, embedding FROM answers "
            "WHERE host IN (?, '') AND embedding IS NOT NULL",
            (host,),
        ).fetchall()
    finally:
        conn.close()
    best, best_score = None, 0.0
    for sig, value, ftype, emb in rows:
        if value is None or not emb:
            continue
        try:
            score = _cosine(q, json.loads(emb))
        except Exception:
            continue
        if score > best_score:
            best, best_score = {"value": value, "field_type": ftype}, score
    if best and best_score >= threshold:
        best.update(source="bank-fuzzy", score=round(best_score, 3))
        return best
    return None


# ----------------------------------------------------------- grounded LLM answer
class GeneratedAnswer(BaseModel):
    answer: str = Field(description="The answer to fill, in the candidate's first-person voice")
    grounded: bool = Field(description="True only if fully supported by the profile facts provided")
    note: str = Field(default="", description="If not grounded, what is missing")


_ANSWER_SYSTEM = (
    "You answer one job-application question for a candidate, truthfully and in their "
    "voice. You are given the candidate's profile as ground truth and optionally the "
    "job. Write a concise, natural, first-person answer.\n\n"
    "HONESTY — never violate:\n"
    "- Use ONLY facts present in the profile. Never invent employers, dates, metrics, "
    "skills, tools, or years of experience.\n"
    "- If the question asks for something not supported by the profile (a skill/number "
    "you can't ground), set grounded=false and write the closest TRUE answer you can "
    "(or leave it general) — never fabricate to fit.\n"
    "- For 'why this company/role', ground enthusiasm in the candidate's real "
    "background and the job's real responsibilities.\n\n"
    "STYLE — write like a person, not an AI:\n"
    "- Vary sentence length (mix short and long). Plain words. Active voice.\n"
    "- BANNED: 'leverage', 'utilize', 'passionate', 'robust', 'seamless', 'spearheaded', "
    "'cutting-edge', 'I am excited to', rule-of-three lists, em-dash pile-ups.\n"
    "- No preamble, no sign-off. Just the answer."
)


def _about_me() -> str:
    try:
        return PATHS.about_me.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def generate_answer(label: str, *, jd_context: str = "", max_chars: int = 900) -> GeneratedAnswer:
    """LLM-draft a grounded free-text answer. Honesty rules mirror the resume path."""
    profile = load_profile()
    experience = _read_json(PATHS.experience)
    user = (
        f"# Candidate profile (ground truth)\n{_about_me()}\n\n"
        f"# Structured experience\n{json.dumps(experience, indent=2)}\n\n"
        f"# Application facts\n{json.dumps(profile, indent=2)}\n\n"
        + (f"# Target job (for relevance only)\n{jd_context[:3000]}\n\n" if jd_context else "")
        + f"# Question to answer\n{label}\n\n"
        f"Answer in at most ~{max_chars} characters."
    )
    ans = llm.complete_json(
        system=_ANSWER_SYSTEM, user=user, schema_model=GeneratedAnswer, max_tokens=1024
    )
    ans.answer = (ans.answer or "").strip()[: max_chars + 200]
    return ans


# --------------------------------------------------------------------- resolve
@dataclass
class AnswerResult:
    value: Any = None            # string to fill, or option text, or None
    option: str | None = None    # chosen option for select/radio
    confidence: float = 0.0      # 0..1
    source: str = "none"         # bank | bank-fuzzy | profile | llm | none
    needs_review: bool = True
    kind: str = "text"           # text | bool
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# Field types we will let the LLM draft for, and the question heuristic.
_LLM_TYPES = {"textarea", "text", ""}
_QUESTION_RE = re.compile(r"\?|why|describe|tell us|explain|what|how|cover letter|interest", re.I)


def resolve(
    *,
    label: str,
    field_type: str = "text",
    options: list[str] | None = None,
    url: str = "",
    jd_context: str = "",
    allow_llm: bool = True,
) -> AnswerResult:
    """Resolve a single form field to an answer. Cheapest tier that succeeds wins."""
    options = options or []
    ftype = (field_type or "text").lower()
    host = _host_of(url)
    sig = signature(label, ftype)
    profile = load_profile()

    def finalize(value, kind, source, confidence) -> AnswerResult:
        # Normalize booleans to display text for text inputs.
        opt = None
        if options:
            opt = match_option(value, kind, options)
            display = opt
        else:
            display = _bool_to_text(value) if kind == "bool" and isinstance(value, bool) else value
        empty = display is None or (isinstance(display, str) and not display.strip())
        # Unknown work-auth bool (None) must always be reviewed, never guessed.
        needs_review = empty or confidence < 0.75 or (kind == "bool" and value is None)
        return AnswerResult(
            value=None if empty else display,
            option=opt,
            confidence=round(confidence, 3),
            source=source if not empty else "none",
            needs_review=needs_review,
            kind=kind,
            note="" if not empty else "no grounded value — fill manually",
        )

    # Tier 1: learned bank (exact signature).
    hit = _bank_lookup(sig, host)
    if hit:
        return finalize(hit["value"], "text", "bank", 0.99)

    # Tier 2: deterministic profile rules.
    value, kind = _builtin_value(f" {sig.split(':', 1)[-1]} ", profile)
    if kind is not None:
        # bool None (unknown work auth) -> needs review; else high confidence.
        conf = 0.95 if not (kind == "bool" and value is None) else 0.0
        return finalize(value, kind, "profile", conf)

    # Tier 3: fuzzy bank (embeddings, best-effort).
    fuzzy = _bank_fuzzy(label, host)
    if fuzzy:
        return finalize(fuzzy["value"], "text", "bank-fuzzy", float(fuzzy.get("score", 0.86)))

    # Tier 4: grounded LLM draft — only for free-text questions.
    looks_like_question = bool(_QUESTION_RE.search(label)) or len((label or "").split()) >= 5
    if allow_llm and cfg_answer_llm_enabled() and ftype in _LLM_TYPES and looks_like_question and not options:
        try:
            gen = generate_answer(label, jd_context=jd_context)
        except Exception as exc:  # noqa: BLE001
            return AnswerResult(source="none", note=f"llm error: {exc}", needs_review=True)
        if gen.answer:
            return AnswerResult(
                value=gen.answer,
                confidence=0.7 if gen.grounded else 0.4,
                source="llm",
                needs_review=not gen.grounded,
                kind="text",
                note=gen.note,
            )

    return AnswerResult(source="none", needs_review=True, note="no match")


# --------------------------------------------------------------------- learning
def learn(fields: list[dict], *, url: str = "") -> int:
    """Upsert submitted field->value pairs into the bank. Returns rows written.

    Each field: {label, value, field_type?}. We store both a host-scoped row (for
    ATS-specific quirks) and keep the global row fresh, and embed best-effort so
    fuzzy match improves over time. Empty values and pure-PII-free-text are kept
    locally only (the DB is gitignored).
    """
    host = _host_of(url)
    written = 0
    conn = _conn()
    try:
        for f in fields:
            label = (f.get("label") or "").strip()
            value = f.get("value")
            if not label or value is None or (isinstance(value, str) and not value.strip()):
                continue
            ftype = (f.get("field_type") or "text").lower()
            sig = signature(label, ftype)
            emb = None
            try:
                vec = llm.embed(label)
                emb = json.dumps(vec) if vec else None
            except Exception:
                emb = None
            now = datetime.now().isoformat(timespec="seconds")
            for h in {host, ""}:  # host-scoped + global
                conn.execute(
                    """INSERT INTO answers (sig, host, label, field_type, value, source, hits, updated_at, embedding)
                       VALUES (?, ?, ?, ?, ?, 'learned', 1, ?, ?)
                       ON CONFLICT(sig, host) DO UPDATE SET
                         value=excluded.value,
                         label=excluded.label,
                         field_type=excluded.field_type,
                         hits=answers.hits+1,
                         updated_at=excluded.updated_at,
                         embedding=COALESCE(excluded.embedding, answers.embedding)""",
                    (sig, h, label, ftype, str(value), now, emb),
                )
            written += 1
        conn.commit()
    finally:
        conn.close()
    return written


def stats() -> dict:
    conn = _conn()
    try:
        n = conn.execute("SELECT COUNT(*) FROM answers WHERE host=''").fetchone()[0]
        hosts = conn.execute(
            "SELECT host, COUNT(*) FROM answers WHERE host!='' GROUP BY host"
        ).fetchall()
    finally:
        conn.close()
    return {"global_answers": n, "by_host": {h: c for h, c in hosts}}


# --------------------------------------------------------------------- config
def cfg_answer_llm_enabled() -> bool:
    try:
        return bool(cfg.load_config().get("answer_llm", True))
    except Exception:
        return True
