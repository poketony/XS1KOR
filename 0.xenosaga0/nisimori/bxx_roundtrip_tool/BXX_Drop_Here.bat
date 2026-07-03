@echo off
setlocal
if "%~1"=="" (
  echo Drop a .bxx file or an extracted BXX folder onto this BAT.
  pause
  exit /b 1
)
python "%~dp0bxx_roundtrip_tool.py" "%~1"
if errorlevel 1 (
  echo.
  echo Failed.
  pause
  exit /b 1
)
echo.
echo Done.
pause
