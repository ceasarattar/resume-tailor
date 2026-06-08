#!/usr/bin/env bash
# setup.sh — macOS one-time setup. Idempotent: safe to re-run.
#
# Primary path uses Claude (Anthropic API) — needs only Python, Git, Tectonic,
# a venv, and an ANTHROPIC_API_KEY. Ollama (the free local fallback) is OPTIONAL;
# pass --with-ollama to install it and pull the local models.
#
# Usage:
#   ./setup.sh                 # Claude path (recommended)
#   ./setup.sh --with-ollama   # also install Ollama + pull local models
set -euo pipefail

OLLAMA_LIBRARY="https://ollama.com/library"
WITH_OLLAMA=0
for arg in "$@"; do
  case "$arg" in
    --with-ollama) WITH_OLLAMA=1 ;;
  esac
done

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "setup.sh is for macOS. On Windows use setup.ps1."

cd "$(dirname "$0")"

# --- Homebrew -----------------------------------------------------------------
if ! command -v brew >/dev/null 2>&1; then
  say "Installing Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  if [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"; fi
else
  say "Homebrew already installed."
fi

brew_ensure() {
  local pkg="$1" bin="${2:-$1}"
  if command -v "$bin" >/dev/null 2>&1; then say "$bin already installed."
  else say "Installing $pkg..."; brew install "$pkg"; fi
}

brew_ensure git git
brew_ensure python python3
brew_ensure tectonic tectonic

# --- Optional: Ollama (local, free fallback) ----------------------------------
setup_ollama() {
  brew_ensure ollama ollama
  if ! curl -fsS http://localhost:11434/api/version >/dev/null 2>&1; then
    say "Starting Ollama..."
    brew services start ollama 2>/dev/null || (nohup ollama serve >/tmp/ollama.log 2>&1 &)
    for _ in $(seq 1 30); do
      curl -fsS http://localhost:11434/api/version >/dev/null 2>&1 && break
      sleep 1
    done
  fi
  if ! curl -fsS http://localhost:11434/api/version >/dev/null 2>&1; then
    warn "Ollama did not start; skipping model pulls."; return
  fi
  say "Ollama is responding."
  pull_model() {
    local tag="$1"
    if ollama list | awk '{print $1}' | grep -qx "$tag"; then say "Model $tag already pulled."; return; fi
    say "Pulling $tag ..."
    ollama pull "$tag" || warn "Could not pull '$tag'. Tags move over time — check $OLLAMA_LIBRARY."
  }
  pull_model "qwen3:8b"
  pull_model "nomic-embed-text"
}
if [ "$WITH_OLLAMA" -eq 1 ]; then setup_ollama
else say "Skipping Ollama (Claude is the default). Re-run with --with-ollama for the free local fallback."; fi

# --- Python venv + deps -------------------------------------------------------
if [ ! -d .venv ]; then say "Creating .venv..."; python3 -m venv .venv; fi
say "Installing Python dependencies (includes the Anthropic SDK)..."
./.venv/bin/python -m pip install --upgrade pip >/dev/null
./.venv/bin/python -m pip install -r requirements.txt

# --- config.yaml --------------------------------------------------------------
if [ ! -f config.yaml ]; then
  say "Creating config.yaml..."
  cp config.example.yaml config.yaml
fi
PROVIDER=$([ "$WITH_OLLAMA" -eq 1 ] && echo "ollama" || echo "anthropic")
/usr/bin/sed -i '' "s/^machine_tier:.*/machine_tier: \"mac\"/" config.yaml
/usr/bin/sed -i '' "s/^provider:.*/provider: \"$PROVIDER\"/" config.yaml

# --- Verify Tectonic ----------------------------------------------------------
tectonic --version >/dev/null 2>&1 && say "Tectonic is responding." \
  || warn "Tectonic not responding on PATH — check the install."

# --- Final guidance -----------------------------------------------------------
echo
say "Setup complete."
if [ "$WITH_OLLAMA" -eq 0 ]; then
  if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    say "ANTHROPIC_API_KEY is set in your environment — you're ready to go."
  else
    warn "One step left: add your Anthropic API key."
    echo "    Create a key (pay-as-you-go, separate from any ChatGPT/Claude subscription):"
    echo "      https://platform.claude.com/settings/keys"
    echo "    Then EITHER export it:   export ANTHROPIC_API_KEY=\"sk-ant-...\"   (add to ~/.zshrc)"
    echo "    OR put it in config.yaml:   anthropic_api_key: \"sk-ant-...\""
    echo "    A tailored resume costs about a cent. No key? Re-run: ./setup.sh --with-ollama"
  fi
fi
echo "    Then: fill profile/about-me.md + profile/experience.json, and run ./run.sh"
