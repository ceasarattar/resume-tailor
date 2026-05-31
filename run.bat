@echo off
REM run.bat — Windows launcher. git pull on launch, app while running, git push on exit.
setlocal enableextensions enabledelayedexpansion
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo WARN: No .venv found. Run setup.ps1 first.
  exit /b 1
)

REM --- Ollama up? ---
powershell -NoProfile -Command "try { Invoke-RestMethod -Uri 'http://localhost:11434/api/version' -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  echo WARN: Ollama isn't responding. Run setup.ps1, or 'ollama serve' in another window.
  exit /b 1
)

REM --- Read config: port + auto-push ---
for /f "usebackq delims=" %%P in (`.venv\Scripts\python.exe -c "import yaml;print(yaml.safe_load(open('config.yaml')).get('server_port',8000))" 2^>nul`) do set "PORT=%%P"
if "%PORT%"=="" set "PORT=8000"
for /f "usebackq delims=" %%A in (`.venv\Scripts\python.exe -c "import yaml;print(str(yaml.safe_load(open('config.yaml')).get('github_auto_push',True)).lower())" 2^>nul`) do set "AUTOPUSH=%%A"
if "%AUTOPUSH%"=="" set "AUTOPUSH=true"

REM --- Sync down ---
git rev-parse --git-dir >nul 2>&1
if not errorlevel 1 (
  echo ==^> Pulling latest from GitHub...
  git pull --rebase || echo WARN: git pull --rebase failed (no upstream yet?). Continuing.
)

REM --- Launch app ---
if exist "app\main.py" (
  echo ==^> Starting Resume Tailor on http://localhost:%PORT% (Ctrl-C to stop)...
  .venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%
) else (
  echo WARN: app\main.py not built yet (Phase 1 scaffold). Nothing to launch.
)

REM --- Push on exit ---
if /i "%AUTOPUSH%"=="true" (
  git rev-parse --git-dir >nul 2>&1
  if not errorlevel 1 (
    echo ==^> Pushing changes to GitHub...
    git add -A
    git diff --cached --quiet
    if errorlevel 1 (
      for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm:ss'"`) do set "TS=%%T"
      git commit -m "auto: session !TS!" >nul
      git push || echo WARN: git push failed (set an upstream remote/branch first).
    ) else (
      echo ==^> No changes to push.
    )
  )
)

endlocal
