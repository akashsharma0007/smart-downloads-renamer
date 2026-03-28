@echo off
setlocal

:: ============================================================
::  Smart Downloads Renamer — Uninstall
::  Cleanly removes the watcher and all generated files.
:: ============================================================

set "PROJECT_DIR=%~dp0"
set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"
set "TASK_NAME=SmartDownloadsRenamer"

echo.
echo  ============================================
echo   Smart Downloads Renamer — Uninstall
echo  ============================================
echo.

:: ── Stop and remove scheduled task ──────────────────────────
echo  [1/3] Stopping background task...

schtasks /query /tn "%TASK_NAME%" >nul 2>&1
if %errorlevel% == 0 (
    schtasks /end /tn "%TASK_NAME%" >nul 2>&1
    schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1
    echo        Task stopped and removed.
) else (
    echo        No scheduled task found.
)

:: ── Kill any running python watcher process ──────────────────
echo.
echo  [2/3] Stopping any running watcher processes...

for /f "tokens=1" %%p in ('wmic process where "commandline like '%%smart_renamer%%'" get processid 2^>nul ^| findstr /r "[0-9]"') do (
    taskkill /pid %%p /f >nul 2>&1
)
echo        Done.

:: ── Remove generated files ───────────────────────────────────
echo.
echo  [3/3] Cleaning up generated files...

if exist "%PROJECT_DIR%\start_watcher.bat"  del "%PROJECT_DIR%\start_watcher.bat"
if exist "%PROJECT_DIR%\start_watcher.vbs"  del "%PROJECT_DIR%\start_watcher.vbs"
if exist "%PROJECT_DIR%\.python_path"       del "%PROJECT_DIR%\.python_path"

echo        Generated files removed.

:: Ask about logs
echo.
set /p "REMOVE_LOGS=  Remove rename logs too? (y/N): "
if /i "%REMOVE_LOGS%"=="y" (
    del "%PROJECT_DIR%\rename_log_*.json" >nul 2>&1
    echo        Logs removed.
) else (
    echo        Logs kept in: %PROJECT_DIR%
)

echo.
echo  ============================================
echo   Uninstall complete.
echo   To reinstall, run setup.bat again.
echo  ============================================
echo.
pause
endlocal
