#!/usr/bin/env bash
# setup.sh — macOS one-time setup. Idempotent: safe to re-run.
set -euo pipefail

OLLAMA_LIBRARY="https://ollama.com/library"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "setup.sh is for macOS. On Windows use setup.ps1."

cd "$(dirname "$0")"

# --- Homebrew -----------------------------------------------------------------
if ! command -v brew >/dev/null 2>&1; then
  say "Installing Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  # Make brew available in this shell (Apple Silicon path).
  if [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"; fi
else
  say "Homebrew already installed."
fi

# --- Tools (install only if missing) ------------------------------------------
brew_ensure() {
  local pkg="$1" bin="${2:-$1}"
  if command -v "$bin" >/dev/null 2>&1; then
    say "$bin already installed."
  else
    say "Installing $pkg..."
    brew install "$pkg"
  fi
}

brew_ensure git git
brew_ensure python python3
brew_ensure ollama ollama
brew_ensure tectonic tectonic

# --- Start Ollama -------------------------------------------------------------
if ! curl -fsS http://localhost:11434/api/version >/dev/null 2>&1; then
  say "Starting Ollama..."
  brew services start ollama 2>/dev/null || (nohup ollama serve >/tmp/ollama.log 2>&1 &)
  # Wait for it to come up.
  for _ in $(seq 1 30); do
    curl -fsS http://localhost:11434/api/version >/dev/null 2>&1 && break
    sleep 1
  done
fi
curl -fsS http://localhost:11434/api/version >/dev/null 2>&1 \
  || die "Ollama did not start. Try 'ollama serve' in another terminal, then re-run."
say "Ollama is responding."

# --- Pull models (verify tag exists first) ------------------------------------
# Mac tier default model is qwen3:8b. Also pull the embedding model.
pull_model() {
  local tag="$1"
  if ollama list | awk '{print $1}' | grep -qx "$tag"; then
    say "Model $tag already pulled."
    return
  fi
  say "Pulling $tag ..."
  if ! ollama pull "$tag"; then
    die "Could not pull '$tag'. Tags move over time — check $OLLAMA_LIBRARY and update config.yaml."
  fi
}

pull_model "qwen3:8b"
pull_model "nomic-embed-text"

# --- Python venv + deps -------------------------------------------------------
if [ ! -d .venv ]; then
  say "Creating .venv..."
  python3 -m venv .venv
fi
say "Installing Python dependencies..."
./.venv/bin/python -m pip install --upgrade pip >/dev/null
./.venv/bin/python -m pip install -r requirements.txt

# --- config.yaml --------------------------------------------------------------
if [ ! -f config.yaml ]; then
  say "Creating config.yaml (tier=mac)..."
  cp config.example.yaml config.yaml
  # Default tier is already 'mac' in the template, but set it explicitly.
  /usr/bin/sed -i '' 's/^machine_tier:.*/machine_tier: "mac"/' config.yaml
else
  say "config.yaml already exists — leaving it untouched."
fi

# --- Verify Tectonic ----------------------------------------------------------
tectonic --version >/dev/null 2>&1 && say "Tectonic is responding." \
  || warn "Tectonic not responding on PATH — check the install."

say "Setup complete. Run ./run.sh to start."
