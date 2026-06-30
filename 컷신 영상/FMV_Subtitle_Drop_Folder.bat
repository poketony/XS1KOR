@echo off
setlocal
cd /d "%~dp0"

set "SCRIPT=%~dp0fmv_subtitle_overlay.py"
if not exist "%SCRIPT%" set "SCRIPT=%~dp0tools\fmv_subtitle_overlay.py"
if not exist "%SCRIPT%" (
  echo Could not find fmv_subtitle_overlay.py.
  echo Put this BAT next to fmv_subtitle_overlay.py, or run it from the repository root.
  echo.
  pause
  exit /b 1
)

if "%~1"=="" (
  echo Drag and drop a folder containing .m2v and subtitle files onto this BAT.
  echo.
  pause
  exit /b 1
)

for %%I in (%*) do (
  echo.
  echo === Processing: %%~fI ===
  python "%SCRIPT%" "%%~fI"
  if errorlevel 1 (
    echo.
    echo Failed: %%~fI
    echo.
    pause
    exit /b 1
  )
)

echo.
echo Done.
pause
