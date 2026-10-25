@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 exit /b 1
set "PYTHONDONTWRITEBYTECODE=1"

echo [1/2] Building think BIN files from translations.json...
python -B think_translate.py build
if errorlevel 1 goto failed

echo [2/2] Updating mapped scene archives...
python -B ..\thinktool.py patch all
if errorlevel 1 goto failed

echo [OK] Think BIN and mapped .a archives updated.
echo Unmapped BIN files are built but cannot be applied to an archive.
popd
pause
exit /b 0

:failed
echo [ERROR] Build stopped. Check the error above before retrying.
popd
pause
exit /b 1
