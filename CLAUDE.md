# CLAUDE.md — guidance for Claude Code working in this repo

Local, cross-platform résumé-tailoring tool. A local LLM (Ollama) turns a job
description into structured content; Python renders ATS-safe LaTeX and compiles
it with Tectonic. The cardinal rule is **honesty: never invent anything not in
`profile/`.**

## Setting it up (first time on a machine)

- **macOS:** run `./setup.sh`
- **Windows:** run `./setup.ps1` (PowerShell)

Both are idempotent and install everything: Ollama, Tectonic, the models
(`qwen3`, `nomic-embed-text`), a `.venv`, Python deps, and `config.yaml`. Then
fill `profile/` and run `./run.sh` (or `run.bat`).

If asked to "set up the project," just run the right setup script for the OS
(`uname` → Darwin = mac). Don't reinstall things that are already present.

## Run / test

```sh
.venv/bin/python -m app.cli path/to/jd.txt        # one-shot CLI (mac path)
.venv/Scripts/python.exe -m app.cli path\to\jd.txt # Windows path
.venv/bin/python -m uvicorn app.main:app --port 8000   # web UI
```

Generation needs a filled `profile/` and a running Ollama; it takes a minute on
the local model. There is no automated test suite yet — verify by generating and
checking the PDF + the ATS/honesty result.

## Code map (`app/`)

- `config.py` — loads `config.yaml`, repo paths (single source of truth).
- `llm.py` — Ollama client (chat with JSON-schema `format`, embeddings, health).
- `parse.py` — JD text → structured `ParsedJD` (Pydantic, schema-enforced).
- `tailor.py` — builds the prompt, gets a **structured JSON plan** from the model
  (summary + rephrased bullets + skill groups), and the honesty checks
  (`forbidden_terms`, `grounding_violations`). **The model never emits LaTeX.**
- `render.py` — deterministically renders the `.tex` from `experience.json`
  metadata + the model's content. Reuses the audited preamble/macros from
  `templates/base-resume.tex`. Handles LaTeX escaping. This is why résumés always
  compile and metadata never drifts.
- `compile.py` — Tectonic wrapper + ATS text-layer check (dual extractor:
  pypdf + pdfminer; flags U+FFFD, f-ligatures, split words, >1 page).
- `rag.py` — SQLite embeddings of past corrections; top-k retrieval into the
  prompt; bootstraps from `corrections.md`.
- `store.py` — writes outputs + `applications.json`; appends to `corrections.md`.
- `cli.py` — `generate_resume()` orchestrates parse → tailor → render → compile →
  ATS check → honesty gate (regenerate once on violation) → store. Shared by the
  CLI and the web server.
- `main.py` — FastAPI app + the browser UI (`app/static/`) + extension `/ingest`.

## Non-negotiable conventions

- **Honesty.** The résumé must only contain facts from `profile/`. The model
  rephrases/reorders; it must not invent employers, dates, metrics, or skills,
  and must never claim anything in the "Things I will NOT claim" list. If a JD
  requirement isn't supported, it goes in `missing_requirements`, not the résumé.
- **ATS / LaTeX template** (see `PROJECT_SPEC.md` §6 and the header comment in
  `templates/base-resume.tex`): single column; Tectonic/XeTeX with `fontspec` +
  Latin Modern **by OTF filename**; `Kerning=Off` + `Ligatures=NoCommon`; colons
  inside bold labels; literal en-dashes. Do **not** add the pdfTeX primitives
  `\input{glyphtounicode}` / `\pdfgentounicode=1` (they break the XeTeX compile).
  After any template/render change, extract the PDF text and confirm: 1 page,
  no U+FFFD, no ligatures, no split words.
- **`config.yaml` is gitignored and per-machine.** Edit `config.example.yaml`
  for shared defaults.
- **Commit after a working change; keep `PROJECT_SPEC.md` the source of truth.**
