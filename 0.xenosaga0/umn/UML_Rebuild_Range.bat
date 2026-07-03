@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set /p START_ID=Start UML id or all: 
if /I "%START_ID%"=="all" (
    set "END_ID="
) else (
    set /p END_ID=End UML id: 
)

where python3 >nul 2>nul
if %ERRORLEVEL%==0 (
    set "PYTHON=python3"
) else (
    set "PYTHON=python"
)

"%PYTHON%" "%~dp0uml_rebuild_range.py" "%START_ID%" "%END_ID%"
echo.
pause
