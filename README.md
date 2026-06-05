# Resume Tailor

A free, local, cross-platform (macOS + Windows) tool that turns a pasted job
description into an **ATS-safe, single-column PDF résumé** tailored from *your*
stored profile — and **never invents anything**. Everything runs locally via
[Ollama](https://ollama.com); nothing is sent to the cloud.

How it works: a local LLM parses the JD and produces **structured content**
(tailored summary, rephrased bullets, ordered skills); Python deterministically
renders the LaTeX and compiles it with Tectonic. Because the model never emits
LaTeX or your contact/dates, résumés always compile and the facts stay true to
`profile/`. A deterministic **honesty gate** blocks anything you listed as
off-limits.

## Quickstart (macOS)

```sh
git clone https://github.com/ceasarattar/resume-tailor.git
cd resume-tailor
./setup.sh          # installs Homebrew, Ollama, Tectonic, models, venv, deps
# → fill in profile/about-me.md and profile/experience.json with YOUR real info
./run.sh            # opens http://localhost:8000
```

Then in the browser: paste a job description → **Parse** → confirm company/role →
**Generate**. The tailored résumé lands in `outputs/<date>_<company>_<role>/`.

### Windows (PowerShell)

```powershell
git clone https://github.com/ceasarattar/resume-tailor.git
cd resume-tailor
./setup.ps1         # winget installs; downloads Tectonic to .tools/
./run.bat
```

Setup is **idempotent** — safe to re-run. It installs the tools, starts Ollama,
pulls the models (`qwen3` + `nomic-embed-text`), creates `.venv`, installs deps,
writes `config.yaml`, and runs a smoke test.

## Fill in your profile (required)

The system will refuse to generate from an empty profile — it won't make up a
background for you. Edit:

- **`profile/experience.json`** — contact, jobs (company/title/dates/bullets),
  projects, education, skills. *All metadata on the résumé comes from here.*
- **`profile/about-me.md`** — your story, and a **"Things I will NOT claim"**
  list (e.g. tools you've never used, degrees you don't hold). The honesty gate
  treats these as forbidden and blocks any résumé that claims them.

## Using it

- **CLI:** `./.venv/bin/python -m app.cli path/to/jd.txt`
- **Browser:** `./run.sh` → http://localhost:8000
- **Chrome extension:** load `extension/` unpacked (see `extension/README.md`) to
  capture a JD straight off a job posting.
- **Corrections:** the "this output is wrong →" box appends a rule to
  `corrections.md` (applied to every future résumé) and indexes it for retrieval.

Each run also flags **"possibly missing requirements"** — JD asks you can't
truthfully meet — so you know the gaps instead of faking them.

## Configuration

Edit `config.yaml` (created by setup; gitignored). Key fields: model tags,
`machine_tier`, `rag_top_k`, `tectonic_path`, `pdflatex_fallback.path`,
`server_port`, `github_auto_push`. Model tags move over time; if a pull fails,
check <https://ollama.com/library> and update `config.yaml`.

## Cross-machine sync

`run.sh` / `run.bat` pull on launch and (if `github_auto_push: true`) commit +
push on exit, so your Mac and Windows machines stay in sync. `config.yaml`,
`.venv`, `.tools/`, and `data/rag.sqlite` are per-machine (gitignored);
`profile/`, `corrections.md`, `outputs/`, and `applications.json` sync.

## Design

See `PROJECT_SPEC.md` for the full design and `CLAUDE.md` for a map of the code.

## License

Résumé template adapted from [Jake's Resume](https://github.com/jakegut/resume) (MIT).
