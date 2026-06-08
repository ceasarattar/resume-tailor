"""LLM backend: provider-agnostic structured completion + embeddings.

Two providers, selected by `provider` in config.yaml:
  - "anthropic" (default): Claude via the official SDK. Uses structured outputs
    (`messages.parse(output_format=...)`) so the model is constrained to the exact
    JSON schema, and prompt caching on the stable system block to keep cost low.
  - "ollama": fully-local fallback (free, no key) via Ollama's native /api/chat
    with the `format` JSON-schema param.

All callers use `complete_json(...)`, which returns a validated Pydantic instance,
so the rest of the app never branches on provider.
"""
from __future__ import annotations

import json
import re
from typing import Type, TypeVar

import httpx
from pydantic import BaseModel

from . import config as cfg

T = TypeVar("T", bound=BaseModel)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    pass


# Backwards-compatible alias (older modules imported OllamaError).
OllamaError = LLMError


def _strip_think(text: str) -> str:
    return _THINK_RE.sub("", text or "").strip()


def _extract_json(text: str) -> str:
    """Pull the outermost JSON object out of a possibly-noisy string."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    return m.group(0) if m else (text or "")


# --------------------------------------------------------------------- anthropic
def _anthropic_client():
    try:
        import anthropic  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise LLMError(
            "The 'anthropic' package is not installed. Run setup, or "
            "'pip install anthropic'."
        ) from exc
    import anthropic

    key = cfg.anthropic_api_key()
    if not key:
        raise LLMError(
            "No Anthropic API key found. Set the ANTHROPIC_API_KEY environment "
            "variable, or put 'anthropic_api_key: sk-ant-...' in config.yaml "
            "(gitignored — it stays on this machine). Create a key at "
            "https://platform.claude.com/settings/keys (this is a pay-as-you-go "
            "API key, separate from any ChatGPT/Claude subscription; a tailored "
            "resume costs roughly a cent). To run fully local with no key, set "
            "'provider: ollama' in config.yaml."
        )
    return anthropic.Anthropic(api_key=key)


def _anthropic_json(system: str, user: str, schema_model: Type[T], max_tokens: int) -> T:
    import anthropic

    client = _anthropic_client()
    model = cfg.anthropic_model()
    system_blocks = [
        {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
    ]
    try:
        resp = client.messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=[{"role": "user", "content": user}],
            output_format=schema_model,
        )
    except anthropic.AuthenticationError as exc:
        raise LLMError(
            f"Anthropic rejected the API key ({exc}). Check ANTHROPIC_API_KEY / "
            "anthropic_api_key in config.yaml."
        ) from exc
    except anthropic.APIError as exc:
        raise LLMError(f"Anthropic API request failed ({exc}).") from exc

    parsed = getattr(resp, "parsed_output", None)
    if isinstance(parsed, schema_model):
        return parsed
    # Fallback: pull text and validate ourselves (covers refusals / SDK drift).
    text = "".join(
        getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text"
    )
    if not text.strip():
        raise LLMError("Anthropic returned no usable content (possible refusal).")
    return schema_model.model_validate_json(_extract_json(text))


# ----------------------------------------------------------------------- ollama
def _ollama_json(system: str, user: str, schema_model: Type[T], max_tokens: int) -> T:
    url = f"{cfg.ollama_native_base()}/api/chat"
    payload = {
        "model": cfg.tailor_model(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.3, "num_predict": max_tokens},
        "format": schema_model.model_json_schema(),
    }
    try:
        resp = httpx.post(url, json=payload, timeout=600.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise LLMError(
            f"Ollama request failed ({exc}). Is Ollama running? Try 'ollama serve', "
            "or set provider: anthropic in config.yaml."
        ) from exc
    content = _strip_think((resp.json().get("message") or {}).get("content", ""))
    try:
        return schema_model.model_validate_json(content)
    except Exception:
        return schema_model.model_validate_json(_extract_json(content))


# ------------------------------------------------------------------------- public
def complete_json(
    *,
    system: str,
    user: str,
    schema_model: Type[T],
    max_tokens: int | None = None,
) -> T:
    """Run a single structured completion and return a validated `schema_model`."""
    mt = max_tokens or cfg.anthropic_max_tokens()
    provider = cfg.provider()
    if provider == "anthropic":
        return _anthropic_json(system, user, schema_model, mt)
    if provider == "ollama":
        return _ollama_json(system, user, schema_model, mt)
    raise LLMError(
        f"Unknown provider '{provider}' in config.yaml (use 'anthropic' or 'ollama')."
    )


def embed(text: str, *, model: str | None = None, timeout: float = 120.0) -> list[float]:
    """Embed text via Ollama (used by RAG). Best-effort: RAG callers tolerate [].

    Anthropic has no embeddings endpoint, so embeddings always go through a local
    Ollama model when one is available. If Ollama isn't running, this raises and
    RAG degrades to injecting corrections.md in full (which it does anyway).
    """
    embed_model = model or cfg.load_config().get("embed_model", "nomic-embed-text")
    url = f"{cfg.ollama_native_base()}/api/embeddings"
    try:
        resp = httpx.post(url, json={"model": embed_model, "prompt": text}, timeout=timeout)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise LLMError(f"Ollama embeddings request failed ({exc}).") from exc
    return resp.json().get("embedding", [])


def health() -> dict:
    """Report whether the configured provider is reachable/usable."""
    provider = cfg.provider()
    out: dict = {"provider": provider, "model": None, "ok": False, "detail": ""}
    if provider == "anthropic":
        out["model"] = cfg.anthropic_model()
        key = cfg.anthropic_api_key()
        if not key:
            out["detail"] = "no API key set"
            return out
        # Don't spend a token on health; a present key is enough to report ready.
        out["ok"] = True
        out["detail"] = "API key present"
        return out
    # ollama
    out["model"] = cfg.tailor_model()
    try:
        url = f"{cfg.ollama_native_base()}/api/version"
        resp = httpx.get(url, timeout=5.0)
        resp.raise_for_status()
        out["ok"] = True
        out["detail"] = f"ollama {resp.json().get('version', '?')}"
    except httpx.HTTPError as exc:
        out["detail"] = f"ollama unreachable ({exc})"
    return out
