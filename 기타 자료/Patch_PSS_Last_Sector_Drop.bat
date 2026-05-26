@echo off
setlocal EnableExtensions

set "SCRIPT=%~dp0patch_pss_last_sector.py"

if not exist "%SCRIPT%" (
    echo [ERROR] patch_pss_last_sector.py was not found.
    echo Put this BAT file and patch_pss_last_sector.py in the same folder.
    pause
    exit /b 1
)

if "%~1"=="" (
    echo Drop the ORIGINAL .pss file onto this BAT file.
    echo.
    pause
    exit /b 1
)

set "ORIGINAL=%~f1"

if not exist "%ORIGINAL%" (
    echo [ERROR] Original file was not found:
    echo %ORIGINAL%
    pause
    exit /b 1
)

if /i not "%~x1"==".pss" (
    echo [ERROR] Original file is not a .pss file:
    echo %ORIGINAL%
    pause
    exit /b 1
)

if not "%~2"=="" (
    set "TRANSLATED=%~f2"
    goto RUN_PATCH
)

echo.
echo Waiting for translated PSS.
echo Drag the TRANSLATED .pss file into this window, then press Enter.
echo.
set /p "TRANSLATED=> "

if "%TRANSLATED%"=="" (
    echo [ERROR] Translated PSS path was empty.
    pause
    exit /b 1
)

set "TRANSLATED=%TRANSLATED:"=%"

:RUN_PATCH
if not exist "%TRANSLATED%" (
    echo [ERROR] Translated file was not found:
    echo %TRANSLATED%
    pause
    exit /b 1
)

if /i not "%TRANSLATED:~-4%"==".pss" (
    echo [ERROR] Translated file is not a .pss file:
    echo %TRANSLATED%
    pause
    exit /b 1
)

echo.
echo Original:
echo %ORIGINAL%
echo.
echo Tail source:
echo %~nx1
echo.
echo Translated:
echo %TRANSLATED%
echo.

python "%SCRIPT%" "%ORIGINAL%" "%TRANSLATED%" --overwrite
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to patch the final tail.
    pause
    exit /b 1
)

echo.
echo Done.
echo Output file has _lastsector_fixed before .pss.
pause
