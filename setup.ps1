#Requires -Version 5.1
<#
  setup.ps1 — Windows one-time setup. Idempotent: safe to re-run.
  Installs Ollama, Tectonic, Python, Git via winget (fallback: scoop),
  starts Ollama, pulls models, creates venv + installs deps, writes config.yaml.
#>
$ErrorActionPreference = "Stop"
$OllamaLibrary = "https://ollama.com/library"

function Say  ($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Warn ($m) { Write-Host "WARN: $m" -ForegroundColor Yellow }
function Die  ($m) { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

Set-Location -Path $PSScriptRoot

function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

$useWinget = Have "winget"
$useScoop  = Have "scoop"
if (-not $useWinget -and -not $useScoop) {
  Warn "Neither winget nor scoop found. Install App Installer (winget) from the Microsoft Store, or scoop from https://scoop.sh, then re-run."
}

# --- Tool install (only if missing) ------------------------------------------
function Ensure-Tool($bin, $wingetId, $scoopId) {
  if (Have $bin) { Say "$bin already installed."; return }
  if ($useWinget) {
    Say "Installing $bin via winget ($wingetId)..."
    winget install --id $wingetId --accept-source-agreements --accept-package-agreements --silent -e
  } elseif ($useScoop) {
    Say "Installing $bin via scoop ($scoopId)..."
    scoop install $scoopId
  } else {
    Die "Cannot install $bin: no package manager. See README."
  }
}

Ensure-Tool "git"      "Git.Git"               "git"
Ensure-Tool "python"   "Python.Python.3.12"    "python"
Ensure-Tool "ollama"   "Ollama.Ollama"         "ollama"
Ensure-Tool "tectonic" "TectonicProject.Tectonic" "tectonic"

# Refresh PATH for this session so freshly-installed tools are found.
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("Path","User")

# --- Start Ollama -------------------------------------------------------------
function Ollama-Up {
  try { Invoke-RestMethod -Uri "http://localhost:11434/api/version" -TimeoutSec 2 | Out-Null; return $true }
  catch { return $false }
}
if (-not (Ollama-Up)) {
  Say "Starting Ollama..."
  Start-Process -WindowStyle Hidden -FilePath "ollama" -ArgumentList "serve" | Out-Null
  for ($i = 0; $i -lt 30; $i++) { if (Ollama-Up) { break }; Start-Sleep -Seconds 1 }
}
if (-not (Ollama-Up)) { Die "Ollama did not start. Run 'ollama serve' in another window, then re-run." }
Say "Ollama is responding."

# --- Pull models (verify tag exists; fail loudly) -----------------------------
function Pull-Model($tag) {
  $listed = (& ollama list) -split "`n" | ForEach-Object { ($_ -split "\s+")[0] }
  if ($listed -contains $tag) { Say "Model $tag already pulled."; return }
  Say "Pulling $tag ..."
  & ollama pull $tag
  if ($LASTEXITCODE -ne 0) {
    Die "Could not pull '$tag'. Tags move over time — check $OllamaLibrary and update config.yaml."
  }
}

# Windows tier pulls both the default 8b and the optional 14b, plus embeddings.
Pull-Model "qwen3:8b"
Pull-Model "qwen3:14b"
Pull-Model "nomic-embed-text"

# --- Python venv + deps -------------------------------------------------------
if (-not (Test-Path ".venv")) {
  Say "Creating .venv..."
  python -m venv .venv
}
Say "Installing Python dependencies..."
& .\.venv\Scripts\python.exe -m pip install --upgrade pip | Out-Null
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

# --- config.yaml (tier=windows) ----------------------------------------------
if (-not (Test-Path "config.yaml")) {
  Say "Creating config.yaml (tier=windows)..."
  Copy-Item "config.example.yaml" "config.yaml"
  (Get-Content "config.yaml") -replace '^machine_tier:.*', 'machine_tier: "windows"' |
    Set-Content "config.yaml"
} else {
  Say "config.yaml already exists — leaving it untouched."
}

# --- Verify Tectonic ----------------------------------------------------------
if (Have "tectonic") { & tectonic --version | Out-Null; Say "Tectonic is responding." }
else { Warn "Tectonic not on PATH — open a new terminal or check the install." }

Say "Setup complete. Run run.bat to start."
