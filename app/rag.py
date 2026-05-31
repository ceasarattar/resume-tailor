"""Lightweight RAG over past corrections/examples.

Stores (JD-context -> correction/example) pairs with embeddings in a local
SQLite file (data/rag.sqlite, gitignored = per-machine). corrections.md is the
synced source of truth, so on a fresh machine the store bootstraps itself from
corrections.md the first time it's queried.

No vector DB and no numpy: embeddings are JSON blobs and similarity is plain
cosine in Python. The corpus is tiny (a handful of corrections), so brute force
is more than fast enough.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime

from . import llm
from .config import PATHS, load_config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind       TEXT NOT NULL,
    jd_context TEXT NOT NULL,
    content    TEXT NOT NULL UNIQUE,
    source     TEXT NOT NULL DEFAULT '',
    embedding  TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    PATHS.data.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(PATHS.rag_db))
    conn.execute(_SCHEMA)
    return conn


def _embed_model() -> str:
    return load_config().get("embed_model", "nomic-embed-text")


def _embed(text: str, *, is_query: bool) -> list[float]:
    """Embed text, adding nomic-embed-text's task prefixes when that model is in
    use. nomic is asymmetric: queries need 'search_query:' and stored documents
    need 'search_document:' or retrieval quality collapses.
    """
    model = _embed_model()
    if "nomic" in model.lower():
        text = ("search_query: " if is_query else "search_document: ") + text
    return llm.embed(text, model=model)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def add(content: str, *, jd_context: str = "", kind: str = "correction", source: str = "") -> bool:
    """Embed and store one entry. Returns True if stored, False on dup/failure.

    Never raises on an embedding/Ollama failure — RAG is best-effort and must not
    break the correction-saving or generation flows.
    """
    content = " ".join((content or "").split()).strip()
    if not content:
        return False
    key = jd_context.strip() or content
    try:
        vec = _embed(key, is_query=False)
    except Exception:
        return False
    if not vec:
        return False
    try:
        with _connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO entries (created_at, kind, jd_context, content, source, embedding) "
                "VALUES (?,?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), kind, jd_context.strip(),
                 content, source, json.dumps(vec)),
            )
            return cur.rowcount > 0  # 0 when an identical content was ignored
    except sqlite3.Error:
        return False


def _count() -> int:
    try:
        with _connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    except sqlite3.Error:
        return 0


def retrieve(query: str, k: int | None = None) -> list[str]:
    """Return up to k stored contents most similar to `query` (best-effort).

    Bootstraps from corrections.md if the store is empty. Returns [] on any
    failure (Ollama down, empty store) so the caller degrades gracefully.
    """
    query = (query or "").strip()
    if not query:
        return []
    if k is None:
        k = int(load_config().get("rag_top_k", 4))
    if _count() == 0:
        reindex_from_corrections()
    try:
        qvec = _embed(query, is_query=True)
    except Exception:
        return []
    if not qvec:
        return []
    try:
        with _connect() as conn:
            rows = conn.execute("SELECT content, embedding FROM entries").fetchall()
    except sqlite3.Error:
        return []
    scored = []
    for content, emb in rows:
        try:
            vec = json.loads(emb)
        except (json.JSONDecodeError, TypeError):
            continue
        scored.append((_cosine(qvec, vec), content))
    scored.sort(key=lambda t: t[0], reverse=True)
    # Keep only positively-similar matches; cosine on nomic-embed is ~0.4+ when related.
    return [c for s, c in scored[:k] if s > 0.2]


_LEARNED_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2})(?: \(re: (.*?)\))?: (.+)$")


def reindex_from_corrections() -> int:
    """(Re)embed the '## Learned corrections' lines from corrections.md.

    Returns the number of entries added. Existing entries are kept (INSERT OR
    IGNORE on the unique content), so this is safe to call repeatedly.
    """
    path = PATHS.corrections
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8")
    if "## Learned corrections" not in text:
        return 0
    section = text.split("## Learned corrections", 1)[1]
    added = 0
    for line in section.splitlines():
        m = _LEARNED_RE.match(line.strip())
        if not m:
            continue
        _date, ctx, content = m.groups()
        if add(content, jd_context=ctx or "", kind="correction", source="corrections.md"):
            added += 1
    return added
