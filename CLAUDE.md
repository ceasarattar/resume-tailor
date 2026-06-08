# CLAUDE.md — guidance for Claude Code working in this repo

Local-first résumé-tailoring tool. An LLM turns a job description into structured
content; Python renders ATS-safe LaTeX and compiles it with Tectonic. The cardinal
rule is **honesty: never invent anything not in `profile/`.**

The LLM backend is **provider-agnostic** (`provider` in `config.yaml`):
- **`anthropic` (default):** Claude via the official `anthropic` SDK, using
  structured outputs (`messages.parse(output_format=...)`) + prompt caching.
  Needs `ANTHROPIC_API_KEY` (env or `config.yaml`). Default model
  `claude-sonnet-4-6`; configurable to `claude-haiku-4-5` / `claude-opus-4-8`.
- **`ollama`:** fully-local fallback (free, no key), e.g. `qwen3:14b`.

All LLM calls go through `app/llm.py::complete_json(...)`, which returns a validated
Pydantic model, so the rest of the app never branches on provider. If you add a
Claude feature, use the official SDK (see the `claude-api` skill) — never an
OpenAI-compatible shim.

## Setting it up (first time on a machine)

- **Windows:** `./setup.ps1` (Claude) or `./setup.ps1 -WithOllama` (local).
- **macOS:** `./setup.sh` (Claude) or `./setup.sh --with-ollama` (local).

Both are idempotent and install Python, Git, Tectonic, a `.venv`, deps (incl. the
Anthropic SDK), and write `config.yaml`. For the Claude path the only extra step is
the API key. Then fill `profile/` and run `./run.sh` (or `run.bat`).

If asked to "set up the project," run the right setup script for the OS
(`uname` → Darwin = mac). Don't reinstall things already present.

## Run / test

```sh
.venv/Scripts/python.exe -m app.cli path\to\jd.txt   # one-shot CLI (Windows)
.venv/bin/python -m app.cli path/to/jd.txt           # one-shot CLI (mac)
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000   # web UI
```

Generation needs a filled `profile/` and either a Claude key or a running Ollama.
There's no automated test suite — verify by generating and checking the PDF is one
page + the ATS/honesty result. To verify the whole pipeline without spending API
credit, set `provider: ollama` in `config.yaml` and run against a local model.

## Code map (`app/`)

- `config.py` — loads `config.yaml`, repo paths, provider/model/feature resolvers.
- `llm.py` — provider-agnostic `complete_json()` (Claude SDK or Ollama) + `embed()`
  (Ollama-only, best-effort, for RAG) + `health()`.
- `parse.py` — JD text → structured `ParsedJD` (schema-enforced).
- `tailor.py` — builds the prompt (role-lens, ethical JD-keyword injection,
  native-tech English, hard honesty rules), gets a **structured plan**, and
  `assemble()`s it back onto the real profile metadata (no length capping). Also the
  honesty checks (`forbidden_terms`, `grounding_violations`). **Model never emits LaTeX.**
- `humanize.py` — `find_ai_tells()` (deterministic linter, Wikipedia "signs of AI
  writing") + `humanize()` (LLM rewrite to natural voice). A **metric guard** reverts
  any line whose numbers changed, so the humanizer can't alter facts.
- `fit.py` — `fit_to_one_page()`: render → compile → count pages; trim least-relevant
  material first (bullets → projects → summary → density nudge → skills) until exactly
  one page. **This is what guarantees one page.**
- `render.py` — deterministically renders the `.tex` from `experience.json` metadata
  + the model's content, reusing the audited preamble from `templates/base-resume.tex`.
  Accepts a `density` knob (font scale + line spread) for the fitter's last resort.
- `compile.py` — Tectonic wrapper + `page_count()` + ATS text-layer check (pypdf +
  pdfminer; flags U+FFFD, f-ligatures, split words, >1 page).
- `rag.py` — SQLite embeddings of past corrections (via Ollama embeddings, best-effort);
  top-k retrieval into the prompt; bootstraps from `corrections.md`.
- `store.py` — writes outputs + `applications.json`; appends to `corrections.md`.
- `cli.py` — `generate_resume()` orchestrates parse → tailor → humanize → fit →
  ATS check → honesty gate (regenerate once on violation) → store. Shared by the
  CLI and the web server.
- `main.py` — FastAPI app + browser UI (`app/static/`) + extension `/ingest`.

## Non-negotiable conventions

- **Honesty.** The résumé contains only facts from `profile/`. The model
  rephrases/reorders; it must not invent employers, dates, metrics, or skills, and
  must never claim anything in "Things I will NOT claim". The humanizer's metric
  guard + `grounding_violations()` enforce this after generation. Unsupported JD
  requirements go in `missing_requirements`, not the résumé.
- **One page, always.** `fit.py` guarantees it. Don't reintroduce static bullet caps
  in `tailor.py` — selection is the model's job, length is the fitter's.
- **Humanize.** Bullets must read like a person wrote them. Keep `humanize.py`'s
  banned-tell list aligned with the tailor prompt's banned words.
- **ATS / LaTeX template** (see `PROJECT_SPEC.md` §6 + the header in
  `templates/base-resume.tex`): single column; Tectonic/XeTeX with `fontspec` +
  Latin Modern **by OTF filename**; `Kerning=Off` + `Ligatures=NoCommon`; colons
  inside bold labels; literal en-dashes. Do **not** add `\input{glyphtounicode}` /
  `\pdfgentounicode=1` (they break the XeTeX compile). After any template/render
  change, extract the PDF text and confirm: 1 page, no U+FFFD, no ligatures, no split
  words. The fitter's `density` knob only adjusts font scale / leading.
- **`config.yaml` is gitignored and per-machine** (it can hold your API key). Edit
  `config.example.yaml` for shared defaults.
- **Commit after a working change; keep `PROJECT_SPEC.md` the source of truth.**
