#Requires -Version 5.1
<#
  setup.ps1 - Windows one-time setup. Idempotent: safe to re-run.
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

# --- Helpers -----------------------------------------------------------------
function Refresh-Path {
  $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
              [System.Environment]::GetEnvironmentVariable("Path","User")
}

function Ensure-Tool($bin, $wingetId, $scoopId) {
  if (Have $bin) { Say "$bin already installed."; return }
  if ($useWinget) {
    Say "Installing $bin via winget ($wingetId)..."
    winget install --id $wingetId --accept-source-agreements --accept-package-agreements --silent -e
  } elseif ($useScoop) {
    Say "Installing $bin via scoop ($scoopId)..."
    scoop install $scoopId
  } else {
    Die "Cannot install ${bin}: no package manager. See README."
  }
  Refresh-Path
}

Ensure-Tool "git"    "Git.Git"            "git"
Ensure-Tool "python" "Python.Python.3.12" "python"

# --- Ollama (resolve full path; its installer doesn't always update PATH) -----
function Resolve-Ollama {
  $c = Get-Command ollama -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  foreach ($p in @("$env:LOCALAPPDATA\Programs\Ollama\ollama.exe",
                   "$env:ProgramFiles\Ollama\ollama.exe")) {
    if (Test-Path $p) { return $p }
  }
  return $null
}

if (-not (Resolve-Ollama)) {
  if ($useWinget)     { Say "Installing Ollama via winget..."; winget install --id Ollama.Ollama --accept-source-agreements --accept-package-agreements --silent -e }
  elseif ($useScoop)  { Say "Installing Ollama via scoop...";  scoop install ollama }
  else                { Die "Cannot install Ollama: no package manager. See README." }
  Refresh-Path
}
$Ollama = Resolve-Ollama
if (-not $Ollama) { Die "Ollama installed but ollama.exe not found. Open a new terminal and re-run." }
Say "Ollama: $Ollama"

# --- Start Ollama -------------------------------------------------------------
function Ollama-Up {
  try { Invoke-RestMethod -Uri "http://localhost:11434/api/version" -TimeoutSec 10 | Out-Null; return $true }
  catch { return $false }
}
if (-not (Ollama-Up)) {
  Say "Starting Ollama..."
  Start-Process -WindowStyle Hidden -FilePath $Ollama -ArgumentList "serve" | Out-Null
  for ($i = 0; $i -lt 60; $i++) { if (Ollama-Up) { break }; Start-Sleep -Seconds 1 }
}
if (-not (Ollama-Up)) { Die "Ollama did not start. Run 'ollama serve' in another window, then re-run." }
Say "Ollama is responding."

# --- Pull models (verify tag exists; fail loudly) -----------------------------
function Pull-Model($tag) {
  $listed = (& $Ollama list) -split "`n" | ForEach-Object { ($_ -split "\s+")[0] }
  if ($listed -contains $tag) { Say "Model $tag already pulled."; return }
  Say "Pulling $tag ..."
  & $Ollama pull $tag
  if ($LASTEXITCODE -ne 0) {
    Die "Could not pull '$tag'. Tags move over time - check $OllamaLibrary and update config.yaml."
  }
}

# Windows tier pulls both the default 8b and the optional 14b, plus embeddings.
Pull-Model "qwen3:8b"
Pull-Model "qwen3:14b"
Pull-Model "nomic-embed-text"

# --- Tectonic (not in winget; prefer scoop, else download release binary) -----
$ToolsDir = Join-Path $PSScriptRoot ".tools"
function Resolve-Tectonic {
  $c = Get-Command tectonic -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  $p = Join-Path $ToolsDir "tectonic.exe"
  if (Test-Path $p) { return $p }
  return $null
}

if (-not (Resolve-Tectonic)) {
  $installed = $false
  if ($useScoop) {
    Say "Installing Tectonic via scoop..."
    scoop install tectonic
    Refresh-Path
    if (Resolve-Tectonic) { $installed = $true }
  }
  if (-not $installed) {
    Say "Downloading Tectonic release binary from GitHub..."
    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $rel = Invoke-RestMethod "https://api.github.com/repos/tectonic-typesetting/tectonic/releases/latest" -Headers @{ "User-Agent" = "resume-tailor" }
    $asset = $rel.assets | Where-Object { $_.name -match "x86_64-pc-windows-msvc.*\.zip$" } | Select-Object -First 1
    if (-not $asset) { Die "No Windows Tectonic release asset found. Install manually: https://tectonic-typesetting.github.io" }
    $zip = Join-Path $env:TEMP $asset.name
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -UseBasicParsing
    $tmp = Join-Path $env:TEMP ("tectonic_" + [guid]::NewGuid().ToString("N"))
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $exe = Get-ChildItem -Path $tmp -Recurse -Filter "tectonic.exe" | Select-Object -First 1
    if (-not $exe) { Die "tectonic.exe not found in the downloaded archive." }
    Copy-Item $exe.FullName (Join-Path $ToolsDir "tectonic.exe") -Force
    Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue
  }
}
$Tectonic = Resolve-Tectonic
if (-not $Tectonic) { Die "Tectonic install failed. See https://tectonic-typesetting.github.io" }
Say "Tectonic: $Tectonic"

# --- Python venv + deps -------------------------------------------------------
if (-not (Test-Path ".venv")) {
  Say "Creating .venv..."
  python -m venv .venv
}
Say "Installing Python dependencies..."
& .\.venv\Scripts\python.exe -m pip install --upgrade pip | Out-Null
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

# --- config.yaml (tier=windows) ----------------------------------------------
# YAML double-quoted strings treat backslash as an escape, so store forward slashes.
$TectonicYaml = ($Tectonic -replace '\\','/')
if (-not (Test-Path "config.yaml")) {
  Say "Creating config.yaml (tier=windows)..."
  Copy-Item "config.example.yaml" "config.yaml"
}
# Always reconcile tier + tectonic_path (idempotent).
(Get-Content "config.yaml") `
  -replace '^machine_tier:.*', 'machine_tier: "windows"' `
  -replace '^tectonic_path:.*', "tectonic_path: `"$TectonicYaml`"" |
  Set-Content "config.yaml"

# --- Verify Tectonic ----------------------------------------------------------
& $Tectonic --version | Out-Null
if ($LASTEXITCODE -eq 0) { Say "Tectonic is responding." }
else { Warn "Tectonic did not respond to --version. Check $Tectonic" }

Say "Setup complete. Run run.bat to start."
