# Resume Tailor

Turn a pasted job description into an **ATS-safe, single-column, one-page PDF
résumé** tailored from *your* stored profile — written to sound like a human, and
**never inventing anything**.

How it works:
1. **Parse** — the LLM turns the JD into a structured brief (role, must-haves, ATS keywords).
2. **Tailor** — it selects, reorders, and rephrases *your real* bullets through the
   lens of that job, reframing them in the job's vocabulary (never adding skills you
   don't have). It returns **structured content**, never LaTeX.
3. **Humanize** — a dedicated pass rewrites every line into natural, concrete,
   senior-engineer English and strips AI/boilerplate tells ("leverage", "robust",
   "seamless", "in order to", …) — while preserving every fact, metric, and date.
4. **Fit to one page** — Python renders the LaTeX, compiles with Tectonic, counts
   pages, and trims the least-relevant material first until it's *exactly one page*.
5. **Check** — an ATS text-layer check (clean, selectable text) and an **honesty
   gate** (blocks anything in your "will NOT claim" list and any invented metric).

Because the model never emits LaTeX or your contact/dates, résumés always compile
and the facts stay true to `profile/`.

## LLM provider

The tool is provider-agnostic (set `provider` in `config.yaml`):

- **`anthropic` (default, recommended)** — Claude via the official SDK. Best quality
  and most consistent. Needs an **API key** (pay-as-you-go, *separate from any
  ChatGPT/Claude.ai subscription*). A tailored résumé costs roughly **a cent**.
  Default model `claude-sonnet-4-6`; switch to `claude-haiku-4-5` (cheapest) or
  `claude-opus-4-8` (best) in `config.yaml`.
- **`ollama`** — fully local, free, no key. Uses a model you've pulled (e.g.
  `qwen3:14b`). Lower quality than Claude but $0.

## Quickstart

### Windows (PowerShell)

```powershell
git clone https://github.com/ceasarattar/resume-tailor.git
cd resume-tailor
./setup.ps1                       # installs Python, Git, Tectonic, venv, deps
$env:ANTHROPIC_API_KEY = "sk-ant-..."   # or set it in config.yaml
./run.bat                         # opens http://localhost:8000
```

### macOS

```sh
git clone https://github.com/ceasarattar/resume-tailor.git
cd resume-tailor
./setup.sh                        # installs Homebrew, Python, Tectonic, venv, deps
export ANTHROPIC_API_KEY="sk-ant-..."   # or set it in config.yaml
./run.sh                          # opens http://localhost:8000
```

Get a key at <https://platform.claude.com/settings/keys> (add a few dollars of
credit). Prefer to run **fully local for free**? Run `./setup.ps1 -WithOllama`
(or `./setup.sh --with-ollama`) and the setup sets `provider: ollama` for you.

Setup is **idempotent** — safe to re-run.

## Fill in your profile (required)

The system refuses to generate from an empty profile — it won't make up a
background for you. Edit:

- **`profile/experience.json`** — contact, jobs (company/title/dates/bullets),
  projects, education, skills. *All metadata on the résumé comes from here.*
- **`profile/about-me.md`** — your story, and a **"Things I will NOT claim"**
  list (tools you've never used, degrees you don't hold). The honesty gate treats
  these as forbidden and blocks any résumé that claims them or invents a metric.

## Using it

- **Browser:** `./run.bat` / `./run.sh` → <http://localhost:8000> → paste JD →
  **Parse** → confirm company/role → **Generate**. The result lands in
  `outputs/<date>_<company>_<role>/` (`resume.pdf`, `tailored.tex`, `jd.md`).
- **CLI:** `.venv\Scripts\python.exe -m app.cli path\to\jd.txt` (Windows) /
  `./.venv/bin/python -m app.cli path/to/jd.txt` (macOS).
- **Chrome extension:** load `extension/` unpacked to capture a JD off a posting.
- **Corrections:** the "this output is wrong → " box appends a rule to
  `corrections.md` (applied to every future résumé) and indexes it for retrieval.

Each run reports the **page count**, what it **trimmed** to fit, any **residual AI
tells**, and **possibly missing requirements** (JD asks you can't truthfully meet).

## Configuration

Edit `config.yaml` (created by setup; gitignored). Key fields: `provider`,
`anthropic_model`, `anthropic_api_key`, `humanize`, `one_page`, `rag_top_k`,
`tectonic_path`, `server_port`, `github_auto_push`. See `config.example.yaml` for
the documented template.

## Cross-machine sync

`run.sh` / `run.bat` pull on launch and (if `github_auto_push: true`) commit + push
on exit. `config.yaml` (which holds your key), `.venv`, `.tools/`, and
`data/rag.sqlite` are per-machine (gitignored); `profile/`, `corrections.md`,
`outputs/`, and `applications.json` sync.

## Design

See `PROJECT_SPEC.md` for the design and `CLAUDE.md` for a map of the code.

## License

Résumé template adapted from [Jake's Resume](https://github.com/jakegut/resume) (MIT).
