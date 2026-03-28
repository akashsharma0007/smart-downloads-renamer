@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  Smart Downloads Renamer — Setup
::  Run this once to install. No admin rights required.
:: ============================================================

set "PROJECT_DIR=%~dp0"
set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"
set "TASK_NAME=SmartDownloadsRenamer"
set "PYTHON_CMD="
set "EMBED_PYTHON=%PROJECT_DIR%\python-embed\python.exe"

echo.
echo  ============================================
echo   Smart Downloads Renamer — Setup
echo  ============================================
echo.

:: ── Step 1: Find Python ─────────────────────────────────────
echo  [1/4] Checking for Python...

python --version >nul 2>&1
if %errorlevel% == 0 (
    set "PYTHON_CMD=python"
    echo        Found system Python.
    goto :check_existing
)

py --version >nul 2>&1
if %errorlevel% == 0 (
    set "PYTHON_CMD=py"
    echo        Found Python launcher.
    goto :check_existing
)

:: Check for bundled portable Python
if exist "%EMBED_PYTHON%" (
    set "PYTHON_CMD=%EMBED_PYTHON%"
    echo        Found bundled portable Python.
    goto :check_existing
)

:: Try winget silently (no admin needed for user-scoped install)
echo        Python not found. Trying winget install...
winget install --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements >nul 2>&1

:: Refresh PATH in this session
for /f "tokens=*" %%i in ('where python 2^>nul') do (
    set "PYTHON_CMD=%%i"
    goto :python_found
)

:: Final fallback — download portable Python embed
echo        winget unavailable. Downloading portable Python...
echo        (This is a one-time ~10MB download)

if not exist "%PROJECT_DIR%\python-embed" mkdir "%PROJECT_DIR%\python-embed"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.0/python-3.12.0-embed-amd64.zip' -OutFile '%PROJECT_DIR%\python-embed\python-embed.zip' -UseBasicParsing" >nul 2>&1

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Expand-Archive -Path '%PROJECT_DIR%\python-embed\python-embed.zip' -DestinationPath '%PROJECT_DIR%\python-embed' -Force" >nul 2>&1

del "%PROJECT_DIR%\python-embed\python-embed.zip" >nul 2>&1

if exist "%EMBED_PYTHON%" (
    set "PYTHON_CMD=%EMBED_PYTHON%"
    echo        Portable Python ready.
) else (
    echo.
    echo  ERROR: Could not install Python automatically.
    echo  Please install Python from https://www.python.org/downloads/
    echo  Make sure to tick "Add python.exe to PATH" during install.
    echo  Then run setup.bat again.
    echo.
    pause
    exit /b 1
)

:python_found
echo        Python ready: !PYTHON_CMD!

:: ── Step 2: Check if already installed ──────────────────────
:check_existing
echo.
echo  [2/4] Checking existing installation...

schtasks /query /tn "%TASK_NAME%" >nul 2>&1
if %errorlevel% == 0 (
    echo        Already installed. Updating task...
    schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1
)

:: ── Step 3: Register with Task Scheduler ────────────────────
echo.
echo  [3/4] Registering background task...

:: Build the watcher command using the found Python
set "WATCHER_CMD=!PYTHON_CMD! "%PROJECT_DIR%\smart_renamer.py" --watch --auto"

:: Create task: runs at logon for current user, no admin needed
schtasks /create ^
  /tn "%TASK_NAME%" ^
  /tr "!WATCHER_CMD!" ^
  /sc onlogon ^
  /ru "%USERNAME%" ^
  /rl limited ^
  /f >nul 2>&1

if %errorlevel% == 0 (
    echo        Task registered successfully.
) else (
    echo        Note: Task Scheduler registration failed.
    echo        The watcher will still work — just won't auto-start on reboot.
    echo        You can manually run: start_watcher.bat
)

:: Save the resolved Python path for use by other scripts
echo !PYTHON_CMD!> "%PROJECT_DIR%\.python_path"

:: ── Step 4: Start watcher now ───────────────────────────────
echo.
echo  [4/4] Starting watcher now...

:: Generate start_watcher.bat with correct paths for this machine
(
    echo @echo off
    echo !PYTHON_CMD! "%PROJECT_DIR%\smart_renamer.py" --watch --auto
) > "%PROJECT_DIR%\start_watcher.bat"

:: Launch watcher silently in background using Task Scheduler
schtasks /run /tn "%TASK_NAME%" >nul 2>&1

if %errorlevel% == 0 (
    echo        Watcher started in background.
) else (
    :: Fallback: start directly in background
    start /min "" cmd /c "!PYTHON_CMD! "%PROJECT_DIR%\smart_renamer.py" --watch --auto"
    echo        Watcher started.
)

:: ── Done ────────────────────────────────────────────────────
echo.
echo  ============================================
echo   Setup complete!
echo  ============================================
echo.
echo   Your Downloads folder is now being monitored.
echo   Files will be renamed automatically as you download them.
echo.
echo   To stop:     run uninstall.bat
echo   To rename manually:
echo     python smart_renamer.py --rename "file" --url "https://..."
echo.
echo   Logs (last 7 days only):
echo   %PROJECT_DIR%\rename_log_[date].json
echo.
pause
endlocal
