@echo off
setlocal
cd /d "%~dp0"
if not exist ".offline_runtime\python\pythonw.exe" (
  call install_offline.cmd || exit /b 1
)
set "PATH=%CD%\.offline_runtime\cmake\bin;%CD%\.offline_runtime\python\Scripts;%PATH%"
start "SGGK Harness" ".offline_runtime\python\pythonw.exe" -m test_harness.ui
