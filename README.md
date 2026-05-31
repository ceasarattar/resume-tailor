# Resume Tailor

A free, local, cross-platform (macOS + Windows) system that turns a pasted job
description into an ATS-safe, single-column LaTeX resume tailored from your stored
profile, compiles it to PDF with Tectonic, and improves over time from your
corrections. Everything runs locally via Ollama. See `PROJECT_SPEC.md` for the
full design.

> Status: **Phase 1 (scaffold)**. The pipeline, backend UI, RAG, and Chrome
> extension land in later phases.

## Requirements

- [Ollama](https://ollama.com) (local LLM runtime)
- [Tectonic](https://tectonic-typesetting.github.io) (LaTeX → PDF)
- Python 3.10+
- Git

The setup scripts install these for you where possible.

## One-time setup

### macOS
```sh
./setup.sh
```

### Windows (PowerShell)
```powershell
./setup.ps1
```

Setup is idempotent — safe to re-run. It installs the tools, starts Ollama, pulls
the required models, creates a `.venv`, installs Python deps, and creates
`config.yaml` from `config.example.yaml` with the right machine tier.

## Run

### macOS
```sh
./run.sh
```

### Windows
```bat
run.bat
```

`run` pulls the latest from GitHub on launch and (if enabled in config) commits and
pushes your changes on exit, so both machines stay in sync.

## Configuration

Edit `config.yaml` (created by setup; gitignored). Defaults come from
`config.example.yaml`. Key fields: model tags, `machine_tier`, `rag_top_k`,
`tectonic_path`, `server_port`, `github_auto_push`.

Model tags can change over time. If a pull fails, check
<https://ollama.com/library> for the current tag and update `config.yaml`.

## Your profile

Fill in before generating resumes:

- `profile/about-me.md` — your full background + a "Things I will NOT claim" list.
- `profile/experience.json` — structured roles, projects, education, skills.

## Repo layout

See `PROJECT_SPEC.md` §4 for the full tree.

## License

Resume template adapted from [Jake's Resume](https://github.com/jakegut/resume) (MIT).
