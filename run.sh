#!/usr/bin/env bash
# run.sh — macOS launcher. git pull on launch, app while running, git push on exit.
set -uo pipefail

cd "$(dirname "$0")"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*"; }

# --- Preconditions ------------------------------------------------------------
if [ ! -d .venv ]; then
  warn "No .venv found. Run ./setup.sh first."
  exit 1
fi
if ! curl -fsS http://localhost:11434/api/version >/dev/null 2>&1; then
  warn "Ollama isn't responding. Run ./setup.sh (it starts Ollama), or 'ollama serve'."
  exit 1
fi

# --- Read config (port + auto-push) ------------------------------------------
PORT="$(./.venv/bin/python - <<'PY' 2>/dev/null || echo 8000
import yaml
try:
    print(yaml.safe_load(open("config.yaml")).get("server_port", 8000))
except Exception:
    print(8000)
PY
)"
AUTOPUSH="$(./.venv/bin/python - <<'PY' 2>/dev/null || echo true
import yaml
try:
    print(str(yaml.safe_load(open("config.yaml")).get("github_auto_push", True)).lower())
except Exception:
    print("true")
PY
)"

# --- Sync down ----------------------------------------------------------------
if git rev-parse --git-dir >/dev/null 2>&1; then
  say "Pulling latest from GitHub..."
  git pull --rebase 2>/dev/null || warn "git pull --rebase failed (no upstream yet?). Continuing."
fi

# --- Push on exit -------------------------------------------------------------
push_on_exit() {
  if [ "$AUTOPUSH" = "true" ] && git rev-parse --git-dir >/dev/null 2>&1; then
    say "Pushing changes to GitHub..."
    git add -A
    if ! git diff --cached --quiet; then
      git commit -m "auto: session $(date '+%Y-%m-%d %H:%M:%S')" >/dev/null
      git push 2>/dev/null || warn "git push failed (set an upstream remote/branch first)."
    else
      say "No changes to push."
    fi
  fi
}
trap push_on_exit EXIT

# --- Launch app ---------------------------------------------------------------
if [ -f app/main.py ]; then
  say "Starting Resume Tailor on http://localhost:${PORT} (Ctrl-C to stop)..."
  ./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
else
  warn "app/main.py not built yet (Phase 1 scaffold). Nothing to launch."
fi
