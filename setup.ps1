#Requires -Version 5.1
<#
  setup.ps1 - Windows one-time setup. Idempotent: safe to re-run.

  Primary path uses Claude (Anthropic API) — needs only Python, Git, Tectonic,
  a venv, and an ANTHROPIC_API_KEY. Ollama (the free local fallback) is OPTIONAL;
  pass -WithOllama to install it and pull the local models.

  Usage:
    ./setup.ps1                 # Claude path (recommended)
    ./setup.ps1 -WithOllama     # also install Ollama + pull local models
#>
param([switch]$WithOllama)

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

Ensure-Tool "git"    "Git.Git"             "git"
Ensure-Tool "python" "Python.Python.3.12"  "python"

# --- Tectonic (not in winget; prefer scoop, else download release binary) ------
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

# --- Optional: Ollama (local, free fallback) ----------------------------------
function Setup-Ollama {
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
    if ($useWinget)    { Say "Installing Ollama via winget..."; winget install --id Ollama.Ollama --accept-source-agreements --accept-package-agreements --silent -e }
    elseif ($useScoop) { Say "Installing Ollama via scoop...";  scoop install ollama }
    else               { Warn "No package manager — skipping Ollama (optional)."; return }
    Refresh-Path
  }
  $Ollama = Resolve-Ollama
  if (-not $Ollama) { Warn "Ollama not found after install; skipping (optional)."; return }
  Say "Ollama: $Ollama"
  function Ollama-Up { try { Invoke-RestMethod -Uri "http://localhost:11434/api/version" -TimeoutSec 10 | Out-Null; return $true } catch { return $false } }
  if (-not (Ollama-Up)) {
    Say "Starting Ollama..."
    Start-Process -WindowStyle Hidden -FilePath $Ollama -ArgumentList "serve" | Out-Null
    for ($i = 0; $i -lt 60; $i++) { if (Ollama-Up) { break }; Start-Sleep -Seconds 1 }
  }
  if (-not (Ollama-Up)) { Warn "Ollama did not start; skipping model pulls."; return }
  function Pull-Model($tag) {
    $listed = (& $Ollama list) -split "`n" | ForEach-Object { ($_ -split "\s+")[0] }
    if ($listed -contains $tag) { Say "Model $tag already pulled."; return }
    Say "Pulling $tag ..."
    & $Ollama pull $tag
    if ($LASTEXITCODE -ne 0) { Warn "Could not pull '$tag'. Tags move over time - check $OllamaLibrary." }
  }
  Pull-Model "qwen3:14b"
  Pull-Model "nomic-embed-text"
}
if ($WithOllama) { Setup-Ollama }
else { Say "Skipping Ollama (Claude is the default). Re-run with -WithOllama for the free local fallback." }

# --- Python venv + deps -------------------------------------------------------
if (-not (Test-Path ".venv")) {
  Say "Creating .venv..."
  python -m venv .venv
}
Say "Installing Python dependencies (includes the Anthropic SDK)..."
& .\.venv\Scripts\python.exe -m pip install --upgrade pip | Out-Null
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

# --- config.yaml --------------------------------------------------------------
$TectonicYaml = ($Tectonic -replace '\\','/')
if (-not (Test-Path "config.yaml")) {
  Say "Creating config.yaml..."
  Copy-Item "config.example.yaml" "config.yaml"
}
$provider = if ($WithOllama) { "ollama" } else { "anthropic" }
$lines = Get-Content "config.yaml"
$lines = $lines -replace '^machine_tier:.*', 'machine_tier: "windows"'
$lines = $lines -replace '^tectonic_path:.*', "tectonic_path: `"$TectonicYaml`""
$lines = $lines -replace '^provider:.*', "provider: `"$provider`""
$lines | Set-Content "config.yaml"

# --- Verify Tectonic ----------------------------------------------------------
& $Tectonic --version | Out-Null
if ($LASTEXITCODE -eq 0) { Say "Tectonic is responding." }
else { Warn "Tectonic did not respond to --version. Check $Tectonic" }

# --- Final guidance -----------------------------------------------------------
Write-Host ""
Say "Setup complete."
if (-not $WithOllama) {
  if ($env:ANTHROPIC_API_KEY) {
    Say "ANTHROPIC_API_KEY is set in your environment — you're ready to go."
  } else {
    Warn "One step left: add your Anthropic API key."
    Write-Host "    Create a key (pay-as-you-go, separate from any ChatGPT/Claude subscription):"
    Write-Host "      https://platform.claude.com/settings/keys"
    Write-Host "    Then EITHER set an environment variable:"
    Write-Host '      setx ANTHROPIC_API_KEY "sk-ant-..."   (reopen the terminal afterward)'
    Write-Host "    OR put it in config.yaml:   anthropic_api_key: `"sk-ant-...`""
    Write-Host "    A tailored resume costs about a cent. No key? Re-run: ./setup.ps1 -WithOllama"
  }
}
Write-Host "    Then: fill profile/about-me.md + profile/experience.json, and run run.bat"
