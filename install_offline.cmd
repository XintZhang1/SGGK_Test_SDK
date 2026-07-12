@echo off
setlocal
cd /d "%~dp0"
set "RUNTIME=%CD%\.offline_runtime"
set "PYTHON=%RUNTIME%\python"
set "CMAKE=%RUNTIME%\cmake"

if not exist "%SystemRoot%\System32\tar.exe" (
  echo [ERROR] Windows tar.exe is required.
  exit /b 1
)
if not exist "offline_bundle\manifest.json" (
  echo [ERROR] offline_bundle\manifest.json is missing.
  exit /b 1
)

if exist "%RUNTIME%" rmdir /s /q "%RUNTIME%"
mkdir "%PYTHON%" "%CMAKE%" || exit /b 1

echo [1/5] Extracting CPython 3.11...
tar -xf "offline_bundle\archives\python-3.11.9-embeddable-amd64.zip" -C "%PYTHON%" || exit /b 1
"%PYTHON%\python.exe" "offline_bundle\configure_embed.py" || exit /b 1

echo [2/5] Installing pinned wheels without network access...
"%PYTHON%\python.exe" "offline_bundle\install_wheels.py" || exit /b 1

echo [3/5] Extracting portable CMake...
tar -xf "offline_bundle\archives\cmake-4.3.3-windows-x86_64.zip" -C "%CMAKE%" --strip-components=1 || exit /b 1

echo [4/5] Verifying hashes and imports...
"%PYTHON%\python.exe" "offline_bundle\verify_offline.py" || exit /b 1

echo [5/5] Offline runtime is ready.
echo Launch with SGGK_Harness_UI.cmd
exit /b 0
