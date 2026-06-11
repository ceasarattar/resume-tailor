r"""SQLite spine for the discovery pipeline: dedup + per-job state.

Every pipeline run discovers many postings; this store makes runs **idempotent**
(a posting seen twice is one row) and tracks each job through the pipeline:

    discovered -> scored -> tailored -> queued -> applied
                       \-> skipped (filtered out / blocked)
                       \-> error

The DB lives at data/jobs.sqlite (gitignored alongside the other data/*.sqlite).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from typing import Any, Iterable

from .config import PATHS
from .discover import JobPosting

STATES = ("discovered", "scored", "tailored", "queued", "applied", "skipped", "error")


def _conn() -> sqlite3.Connection:
    PATHS.data.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(PATHS.data / "jobs.sqlite"))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
            uid TEXT PRIMARY KEY,
            ats TEXT, company TEXT, title TEXT, location TEXT, remote INTEGER,
            url TEXT, apply_url TEXT, jd_text TEXT, department TEXT,
            employment_type TEXT, posted_at TEXT, updated_at TEXT,
            salary_min REAL, salary_max REAL,
            score REAL, score_reasons TEXT, missing TEXT, blockers TEXT,
            recommend INTEGER,
            state TEXT NOT NULL DEFAULT 'discovered',
            resume_path TEXT, note TEXT,
            discovered_at TEXT, scored_at TEXT, tailored_at TEXT, applied_at TEXT
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state)")
    # Migration for DBs created before posted_at existed.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    for col, decl in (("posted_at", "TEXT"), ("salary_min", "REAL"), ("salary_max", "REAL")):
        if col not in cols:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------- writes
def upsert_discovered(jobs: Iterable[JobPosting]) -> dict[str, int]:
    """Insert newly-seen postings as 'discovered'. Existing rows keep their state
    (so re-runs don't reset scoring/applied), but refresh volatile JD fields.
    Returns {"new": n, "seen": n, "total_rows": n}."""
    conn = _conn()
    new = seen = 0
    try:
        for j in jobs:
            row = conn.execute("SELECT uid FROM jobs WHERE uid=?", (j.uid,)).fetchone()
            if row:
                seen += 1
                conn.execute(
                    "UPDATE jobs SET jd_text=?, title=?, location=?, apply_url=?, posted_at=?, updated_at=? WHERE uid=?",
                    (j.jd_text, j.title, j.location, j.apply_url, j.posted_at, j.updated_at, j.uid),
                )
            else:
                new += 1
                conn.execute(
                    """INSERT INTO jobs (uid, ats, company, title, location, remote, url,
                        apply_url, jd_text, department, employment_type, posted_at, updated_at,
                        salary_min, salary_max, state, discovered_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'discovered', ?)""",
                    (j.uid, j.ats, j.company, j.title, j.location,
                     None if j.remote is None else int(j.remote), j.url, j.apply_url,
                     j.jd_text, j.department, j.employment_type, j.posted_at, j.updated_at,
                     j.salary_min, j.salary_max, _now()),
                )
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    finally:
        conn.close()
    return {"new": new, "seen": seen, "total_rows": total}


def set_score(uid: str, *, score: float, reasons: list[str], missing: list[str],
              blockers: list[str], recommend: bool) -> None:
    conn = _conn()
    try:
        state = "scored" if recommend else "skipped"
        conn.execute(
            """UPDATE jobs SET score=?, score_reasons=?, missing=?, blockers=?,
               recommend=?, state=?, scored_at=? WHERE uid=?""",
            (round(float(score), 3), json.dumps(reasons), json.dumps(missing),
             json.dumps(blockers), int(recommend), state, _now(), uid),
        )
        conn.commit()
    finally:
        conn.close()


def set_tailored(uid: str, resume_path: str, *, note: str = "") -> None:
    _update(uid, state="tailored", resume_path=resume_path, tailored_at=_now(), note=note)


def set_state(uid: str, state: str, *, note: str = "") -> None:
    assert state in STATES, state
    extra: dict[str, Any] = {"note": note} if note else {}
    if state == "applied":
        extra["applied_at"] = _now()
    _update(uid, state=state, **extra)


def _update(uid: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    conn = _conn()
    try:
        conn.execute(f"UPDATE jobs SET {cols} WHERE uid=?", (*fields.values(), uid))
        conn.commit()
    finally:
        conn.close()


# -------------------------------------------------------------------- reads
def _row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    for k in ("score_reasons", "missing", "blockers"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                d[k] = []
        else:
            d[k] = []
    return d


def get(uid: str) -> dict[str, Any] | None:
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM jobs WHERE uid=?", (uid,)).fetchone()
        return _row_to_dict(r) if r else None
    finally:
        conn.close()


def by_state(state: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    conn = _conn()
    try:
        # Recency-first (the user prioritizes the most recent), then best score.
        q = ("SELECT * FROM jobs WHERE state=? "
             "ORDER BY COALESCE(NULLIF(posted_at,''),'0') DESC, COALESCE(score,0) DESC, discovered_at DESC")
        if limit:
            q += f" LIMIT {int(limit)}"
        return [_row_to_dict(r) for r in conn.execute(q, (state,)).fetchall()]
    finally:
        conn.close()


def counts() -> dict[str, int]:
    conn = _conn()
    try:
        rows = conn.execute("SELECT state, COUNT(*) FROM jobs GROUP BY state").fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        conn.close()


def applied_today() -> int:
    conn = _conn()
    try:
        today = date.today().isoformat()
        return conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE state='applied' AND applied_at LIKE ?",
            (today + "%",),
        ).fetchone()[0]
    finally:
        conn.close()
