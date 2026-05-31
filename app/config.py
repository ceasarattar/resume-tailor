"""Configuration loading + shared paths for Resume Tailor.

Reads config.yaml (created by setup from config.example.yaml). All modules import
`load_config()` and `PATHS` from here so there is one source of truth.
"""
from __future__ import annotations

import functools
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


def tailor_model(cfg: dict[str, Any] | None = None) -> str:
    """Resolve the tailoring model for the current machine tier."""
    cfg = load_config() if cfg is None else cfg
    tier = cfg.get("machine_tier", "mac")
    tm = cfg.get("tailor_model")
    if isinstance(tm, dict):
        return tm.get(tier) or tm.get("mac") or "qwen3:8b"
    return tm or "qwen3:8b"


def ollama_native_base(cfg: dict[str, Any] | None = None) -> str:
    """Return the native Ollama base URL (strip a trailing /v1 if present)."""
    cfg = load_config() if cfg is None else cfg
    base = (cfg.get("ollama_base_url") or "http://localhost:11434/v1").rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base
