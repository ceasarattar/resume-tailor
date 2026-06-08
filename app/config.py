"""Configuration loading + shared paths for Resume Tailor.

Reads config.yaml (created by setup from config.example.yaml). All modules import
`load_config()` and `PATHS` from here so there is one source of truth.

The system is provider-agnostic: `provider` selects the LLM backend.
  - "anthropic" (default): Claude via the official SDK. Best quality + consistency.
  - "ollama": fully-local fallback (free, no API key) using a model you've pulled.
"""
from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any

import yaml

# Repo root = parent of the app/ package directory.
ROOT = Path(__file__).resolve().parent.parent


class Paths:
    root = ROOT
    config = ROOT / "config.yaml"
    config_example = ROOT / "config.example.yaml"
    profile = ROOT / "profile"
    about_me = ROOT / "profile" / "about-me.md"
    experience = ROOT / "profile" / "experience.json"
    templates = ROOT / "templates"
    base_resume = ROOT / "templates" / "base-resume.tex"
    corrections = ROOT / "corrections.md"
    data = ROOT / "data"
    jobs = ROOT / "data" / "jobs"
    rag_db = ROOT / "data" / "rag.sqlite"
    outputs = ROOT / "outputs"
    applications = ROOT / "applications.json"


PATHS = Paths()


@functools.lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    """Load config.yaml, falling back to config.example.yaml if absent."""
    path = PATHS.config if PATHS.config.exists() else PATHS.config_example
    if not path.exists():
        raise FileNotFoundError(
            "No config.yaml or config.example.yaml found. Run setup first."
        )
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    return cfg


# ----------------------------------------------------------------- provider
def provider(cfg: dict[str, Any] | None = None) -> str:
    """Which LLM backend to use: 'anthropic' (default) or 'ollama'."""
    cfg = load_config() if cfg is None else cfg
    return str(cfg.get("provider", "anthropic")).strip().lower()


def anthropic_model(cfg: dict[str, Any] | None = None) -> str:
    """Claude model id. Default is the cheap+accurate Sonnet; configurable to
    claude-haiku-4-5 (cheapest) or claude-opus-4-8 (best)."""
    cfg = load_config() if cfg is None else cfg
    return str(cfg.get("anthropic_model") or "claude-sonnet-4-6")


def anthropic_api_key(cfg: dict[str, Any] | None = None) -> str | None:
    """Resolve the Anthropic API key from the environment first (preferred),
    then config.yaml (which is gitignored, so the key never leaves the machine).
    """
    cfg = load_config() if cfg is None else cfg
    env = os.environ.get("ANTHROPIC_API_KEY")
    if env and env.strip():
        return env.strip()
    key = (cfg.get("anthropic_api_key") or "").strip()
    return key or None


def anthropic_max_tokens(cfg: dict[str, Any] | None = None) -> int:
    cfg = load_config() if cfg is None else cfg
    return int(cfg.get("anthropic_max_tokens", 4096))


# --------------------------------------------------------------- ollama (fallback)
def tailor_model(cfg: dict[str, Any] | None = None) -> str:
    """Resolve the local (Ollama) tailoring model for the current machine tier.
    Only used when provider == 'ollama'."""
    cfg = load_config() if cfg is None else cfg
    tier = cfg.get("machine_tier", "windows")
    tm = cfg.get("tailor_model")
    if isinstance(tm, dict):
        return tm.get(tier) or tm.get("windows") or tm.get("mac") or "qwen3:14b"
    return tm or "qwen3:14b"


def ollama_native_base(cfg: dict[str, Any] | None = None) -> str:
    """Return the native Ollama base URL (strip a trailing /v1 if present)."""
    cfg = load_config() if cfg is None else cfg
    base = (cfg.get("ollama_base_url") or "http://localhost:11434/v1").rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


# --------------------------------------------------------------- feature flags
def humanize_enabled(cfg: dict[str, Any] | None = None) -> bool:
    cfg = load_config() if cfg is None else cfg
    return bool(cfg.get("humanize", True))


def one_page_enabled(cfg: dict[str, Any] | None = None) -> bool:
    cfg = load_config() if cfg is None else cfg
    return bool(cfg.get("one_page", True))
