"""Ollama client: chat (with optional JSON-schema enforcement) + embeddings.

Uses Ollama's native API (http://localhost:11434/api/...) because the `format`
parameter (JSON schema enforcement) and the `think` toggle for qwen3 live there.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from .config import load_config, ollama_native_base

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class OllamaError(RuntimeError):
    pass


def _strip_think(text: str) -> str:
    """qwen3 emits <think>...</think>; remove it so callers get clean output."""
    return _THINK_RE.sub("", text).strip()


def chat(
    messages: list[dict[str, str]],
    *,
    model: str,
    temperature: float = 0.0,
    num_predict: int = 2048,
    fmt: Any | None = None,
    think: bool = False,
    timeout: float = 600.0,
) -> str:
    """Single-turn (non-streaming) chat completion.

    fmt: either "json" or a JSON-schema dict, passed to Ollama's `format` param.
    Returns the assistant message content with any <think> block stripped.
    """
    cfg = load_config()
    url = f"{ollama_native_base(cfg)}/api/chat"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": think,
        "options": {"temperature": temperature, "num_predict": num_predict},
    }
    if fmt is not None:
        payload["format"] = fmt
    try:
        resp = httpx.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
    except httpx.HTTPError as exc:  # connection refused, timeout, non-2xx
        raise OllamaError(
            f"Ollama chat request failed ({exc}). Is Ollama running? "
            f"Try setup, or 'ollama serve'."
        ) from exc
    data = resp.json()
    content = (data.get("message") or {}).get("content", "")
    return _strip_think(content)


def embed(text: str, *, model: str, timeout: float = 120.0) -> list[float]:
    """Return an embedding vector for `text` (used by RAG in Phase 4)."""
    cfg = load_config()
    url = f"{ollama_native_base(cfg)}/api/embeddings"
    try:
        resp = httpx.post(url, json={"model": model, "prompt": text}, timeout=timeout)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise OllamaError(f"Ollama embeddings request failed ({exc}).") from exc
    return resp.json().get("embedding", [])


def health() -> str:
    """Return the running Ollama version, or raise OllamaError."""
    cfg = load_config()
    url = f"{ollama_native_base(cfg)}/api/version"
    try:
        resp = httpx.get(url, timeout=5.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise OllamaError(f"Ollama not reachable at {url} ({exc}).") from exc
    return resp.json().get("version", "unknown")
