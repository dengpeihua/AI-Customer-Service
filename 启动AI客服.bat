@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Start-AICustomerService.ps1"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo.
  echo AI customer service failed to start. Check the error above and logs\widget.err.log.
  pause
)
exit /b %EXIT_CODE%
