@echo off
setlocal
cd /d "%~dp0"
python "%~dp0card_graphics_workflow.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" echo.
if not "%EXIT_CODE%"=="0" echo cardgfx failed with exit code %EXIT_CODE%.
exit /b %EXIT_CODE%
