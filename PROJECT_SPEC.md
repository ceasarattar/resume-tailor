# PROJECT_SPEC.md — Local Resume-Tailoring System

This is the authoritative spec. Read it fully before writing any code. Build in
the phases at the bottom; do not skip ahead. Ask me before making any choice this
spec leaves open.

## 1. Goal
A free, local, cross-platform (macOS + Windows) system that:
1. Takes a pasted job description (JD), parses it into a structured brief using a
   local LLM.
2. Generates an ATS-safe, single-column LaTeX resume tailored to that JD, drawn
   from my stored profile + a base template, and compiles it to PDF.
3. Improves over time from my corrections (in-context learning, not fine-tuning).
4. Syncs everything across both machines through one GitHub repo.
5. Has a Chrome extension to grab a JD from a live posting and send it to the app.

Single user (me). Privacy is not a concern. Must cost nothing to run repeatedly.

## 2. Hardware (drives model choice)
- MacBook: M1 Pro, 16 GB unified memory. THE BINDING CONSTRAINT.
- Windows PC: Ryzen 7 (X3D), 32 GB RAM, RTX 5070 12 GB VRAM.
- Default model must run well on the 16 GB Mac. The Windows box may optionally use
  a larger model via a config flag.

## 3. Tech stack (decided — do not substitute without asking)
- **LLM runtime:** Ollama (OpenAI-compatible endpoint at http://localhost:11434/v1).
- **Default model:** `qwen3:8b` (Q4_K_M, ~5 GB). Windows-optional tier: `qwen3:14b`.
  Model tags MUST live in `config.yaml`, not hardcoded. Tags move over time —
  the setup script must verify the tag exists at pull time and fail loudly with a
  pointer to https://ollama.com/library if it doesn't.
- **Embeddings (for RAG):** `nomic-embed-text` via Ollama.
- **Backend:** Python + FastAPI. Serves the browser UI AND an endpoint the Chrome
  extension POSTs to. Enable CORS for the extension origin.
- **Browser UI:** plain HTML/CSS/JS served by FastAPI (no heavy framework). Must
  support: paste JD, pick/confirm company+role, generate, preview PDF, and a
  "this output is wrong → " correction box that appends to corrections.md.
- **LaTeX compile:** Tectonic (single self-contained binary). One-command compile.
  NOTE: Tectonic uses XeTeX. After compiling, verify the PDF has a selectable,
  correctly-ordered text layer (extract text and check it's not scrambled).
  **pdflatex fallback (defined path, not just "anticipated"):** `config.yaml`
  exposes a `pdflatex_fallback` block — when its `path` is set (a TeX Live
  `pdflatex`/`latexmk` binary), `compile.py` can run a "final submission" build
  through it as an alternate renderer. The default renderer is Tectonic; the
  fallback is opt-in per build (e.g. a `--final`/`pdflatex` flag) for the rare
  case a specific ATS parser struggles with the XeTeX output. If `path` is null,
  the fallback is simply unavailable and Tectonic is always used.
- **RAG:** lightweight. Store past (JD-context → correction/example) pairs with
  embeddings in a local store. Corpus is small (hundreds of items max), so use
  SQLite + cosine similarity over stored vectors (or sqlite-vec if trivial to
  install). Do NOT pull in a heavy vector DB. Retrieve top-k (default 4) and inject
  into the tailoring prompt.
- **Learning loop:** `corrections.md` is a structured, human-readable list of rules
  and preferences, injected into every tailoring prompt. RAG augments it once it
  grows. NO fine-tuning.

## 4. Repo structure (create exactly this)
```
resume-tailor/
├── PROJECT_SPEC.md          # this file
├── README.md                # how to set up + use, both OSes
├── .gitattributes           # line-ending normalization (see §7)
├── .gitignore               # venv, caches, embeddings store, optionally PDFs
├── config.example.yaml      # template config (committed)
├── config.yaml              # real config (gitignored; created by setup)
├── requirements.txt
├── setup.sh                 # macOS one-time setup (idempotent)
├── setup.ps1                # Windows one-time setup (idempotent)
├── run.sh                   # macOS: git pull → launch app → git push on exit
├── run.bat                  # Windows: same
├── app/
│   ├── main.py              # FastAPI app + routes + static serving + CORS
│   ├── llm.py               # Ollama client (chat + embeddings)
│   ├── parse.py             # JD text → structured JSON + jd.md brief
│   ├── tailor.py            # profile + base.tex + corrections + RAG → tailored .tex
│   ├── compile.py           # Tectonic wrapper + text-layer ATS check
│   ├── rag.py               # embed, store, retrieve correction/example pairs
│   ├── store.py             # write outputs, update applications.json
│   └── static/              # index.html, app.js, style.css
├── profile/
│   ├── about-me.md          # my full background (I fill this in)
│   └── experience.json      # structured roles/bullets (I fill this in)
├── templates/
│   └── base-resume.tex      # Jake's Resume, single-column (see §6)
├── corrections.md           # the learning file (starts with rules, grows)
├── data/
│   ├── jobs/                # parsed JD briefs (<date>_<company>_<role>.md)
│   └── rag.sqlite           # embeddings store (gitignored)
├── outputs/
│   └── <date>_<company>_<role>/   # tailored.tex, resume.pdf, jd.md
├── applications.json        # index/log of every application (tracker)
└── extension/
    ├── manifest.json        # Manifest V3
    ├── popup.html
    ├── popup.js
    ├── content.js           # scrape JD text from active tab
    └── background.js
```

## 5. Pipeline (the core flow)
1. **Input:** JD text arrives via browser paste or Chrome-extension POST.
2. **Parse (parse.py):** Call Ollama with a JSON schema via the `format` param and
   `temperature: 0`. Also state the schema in the prompt (Ollama doesn't inject it
   automatically). Validate the result with Pydantic; on invalid/truncated JSON,
   retry once with a higher token limit, then fail gracefully. Output: a structured
   object (title, company, must-have requirements, nice-to-haves, 6–8 priority
   keywords, seniority) AND a compact `jd.md` brief saved to `data/jobs/`.
3. **Retrieve (rag.py):** Embed the JD brief, fetch top-k relevant past corrections
   /examples.
4. **Tailor (tailor.py):** Prompt the LLM with: corrections.md (all of it) +
   retrieved RAG snippets + profile (about-me.md + experience.json) + base-resume.tex
   + the parsed JD. Instruct it to produce a COMPLETE, compile-ready, single-column
   `.tex` that (a) rewrites the summary to mirror the role, (b) reorders/relabels
   skills so JD keywords surface first — only skills I actually have, (c) reorders
   and rephrases experience bullets to foreground relevant impact with metrics,
   (d) weaves the priority keywords in naturally (NO stuffing, NO hidden text),
   (e) NEVER invents experience, employers, dates, metrics, or anything listed under
   "Things I will NOT claim" in about-me.md, (f) stays one page, single column.
   It must also return a short changelog and a list of JD requirements I appear to
   be missing (flag, don't fake).
5. **Compile (compile.py):** Run Tectonic → PDF. Run the text-layer ATS check.
6. **Store (store.py):** Write `tailored.tex`, `resume.pdf`, `jd.md` into
   `outputs/<date>_<company>_<role>/`; append an entry to `applications.json`
   (date, company, role, path, status="generated", missing_requirements,
   keywords_used).
7. **Correct:** When I submit a correction in the UI, append a structured line to
   corrections.md AND store it as a RAG example (with its JD context) so it's
   retrievable later.

## 6. Base resume template (ATS rules — non-negotiable)
- Start from Jake's Resume (github.com/jakegut/resume, MIT). Single column.
- Replace its `\begin{multicols}{4}` coursework block with a single-column
  comma-separated list (multicols can scramble ATS reading order).
- Clean text extraction: use a Unicode-native XeTeX setup via `fontspec` with an
  OpenType font Tectonic can fetch on a bare machine (Latin Modern, loaded BY OTF
  FILENAME — loading by font *name* fails where the font isn't installed system-
  wide); this produces correct ToUnicode maps natively. Do NOT use
  `\input{glyphtounicode}` / `\pdfgentounicode=1` — those are pdfTeX primitives
  that XeTeX does not define and that HALT the Tectonic compile. Set
  `Kerning=Off` on the font (bold kern pairs otherwise extract as spurious word
  breaks, e.g. "Frameworks" → "F rameworks", which breaks keyword tokenization),
  and prefer literal Unicode en-dashes (–, U+2013) over `--` in date ranges.
  Verify by extracting the text layer post-compile: assert one page, no U+FFFD,
  correct dash codepoints, and that bold keyword labels are not split.
- Standard section headings only (Experience, Education, Skills, Projects).
- No tables-for-layout, no icons, no text inside graphics, no header/footer contact
  info — contact line goes in the body.
- One page. Selectable text layer (verified by compile.py's check).

## 7. Cross-machine sync
- `.gitattributes`: `* text=auto`, plus `*.py text eol=lf`, `*.tex text eol=lf`,
  `*.md text eol=lf`, `*.sh text eol=lf`, `*.bat text eol=crlf`, `*.ps1 text eol=crlf`,
  `*.pdf binary`, `*.png binary`. Run `git add --renormalize .` once after creating it.
- `run.sh` / `run.bat`: `git pull --rebase` on launch; on exit, `git add -A`,
  commit with a timestamped message, `git push`. Handle "model not pulled yet" by
  telling the user to run setup.
- `.gitignore`: `.venv/`, `__pycache__/`, `config.yaml`, `data/rag.sqlite`,
  Tectonic cache. KEEP `outputs/` tracked (PDFs are tiny and double as a record).

## 8. Setup scripts (the "one command" ask — make these robust + idempotent)
Both scripts must be safe to re-run, detect already-installed tools, and print clear
next steps. They set a `machine_tier` in config (mac→8b default, windows→14b default).

**setup.sh (macOS):**
- Ensure Homebrew (install if missing). Install: ollama, tectonic, python (if needed), git.
- Start Ollama (`brew services start ollama` or background `ollama serve`).
- `ollama pull qwen3:8b` and `ollama pull nomic-embed-text` (verify tags first).
- Create `.venv`, `pip install -r requirements.txt`.
- Copy `config.example.yaml` → `config.yaml` if absent; set tier=mac.
- Print: "Setup complete. Run ./run.sh to start."

**setup.ps1 (Windows):**
- Use winget (fallback scoop) to install: Ollama, Tectonic, Python, Git.
- Start Ollama service.
- `ollama pull qwen3:8b`, `ollama pull qwen3:14b`, `ollama pull nomic-embed-text`.
- Create venv, install requirements.
- Copy config; set tier=windows (default model qwen3:14b).
- Print next steps.

## 9. Chrome extension (Manifest V3)
- `content.js`: grab JD text from the active tab (selected text if any, else a
  best-effort scrape of the main job-description container; fall back to full body).
- `popup.html`/`popup.js`: a "Send to Resume Tailor" button + optional company/role
  fields. POSTs the JD to `http://localhost:8000/ingest`.
- Backend must allow CORS from the extension. If the backend isn't running, the
  popup shows a clear "start the app first" message.
- Keep it minimal — it's just a capture front-end; all real work is server-side.

## 10. Config (config.example.yaml)
Include: ollama_base_url, parse_model, tailor_model (per tier), embed_model,
machine_tier, rag_top_k, tectonic_path, optional pdflatex_fallback, server_port,
output_dir, github auto-push on/off.

## 11. Acceptance criteria per phase
Each phase must end with a working, testable state and a one-line "how to verify."
Do not proceed to the next phase until the current one runs. Keep dependencies
minimal. Commit after each phase.

## Build phases
- **Phase 1 — Scaffold:** repo structure, .gitattributes, .gitignore,
  config.example.yaml, requirements.txt, README skeleton, setup.sh, setup.ps1,
  run.sh, run.bat, base-resume.tex (single-column Jake's), empty profile files,
  starter corrections.md with the hard rules from §5. Verify: setup script runs
  clean on this machine and Ollama + Tectonic respond.
- **Phase 2 — Core pipeline (CLI first):** parse.py, tailor.py, compile.py,
  store.py, llm.py. A CLI command that takes a JD file → tailored PDF in outputs/.
  Verify: feed a sample JD, get a compiling one-page ATS-safe PDF + applications.json
  entry.
- **Phase 3 — Backend + browser UI:** FastAPI app, routes, static UI, correction box.
  Verify: paste JD in browser → PDF preview; submitting a correction updates
  corrections.md.
- **Phase 4 — RAG + learning loop:** rag.py, embeddings store, retrieval injected
  into tailoring, corrections stored as retrievable examples. Verify: a correction
  given once visibly influences a later similar job.
- **Phase 5 — Chrome extension:** capture + POST to /ingest. Verify: on a real job
  posting, clicking the extension sends the JD and produces a tailored resume.

When in doubt about my preferences, ASK rather than assume.
