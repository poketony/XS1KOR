@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" (
  echo Drop a .npr file to extract it.
  echo Drop an extracted folder containing npr_meta.json to rebuild it.
  pause
  exit /b 1
)

python "%~dp0npr_roundtrip_tool.py" "%~1"
echo.
echo Done.
pause
