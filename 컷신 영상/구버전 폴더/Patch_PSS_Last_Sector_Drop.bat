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
    echo Drop the TRANSLATED *_KOR.pss file onto this BAT file.
    echo The original PSS is searched automatically under:
    echo E:\[Xenosaga original]\original
    echo.
    pause
    exit /b 1
)

set "TRANSLATED=%~f1"

if not exist "%TRANSLATED%" (
    echo [ERROR] Translated file was not found:
    echo %TRANSLATED%
    pause
    exit /b 1
)

if /i not "%~x1"==".pss" (
    echo [ERROR] Translated file is not a .pss file:
    echo %TRANSLATED%
    pause
    exit /b 1
)

echo.
echo Translated PSS accepted:
echo %~nx1
echo %TRANSLATED%
echo.
echo Searching original PSS by removing _KOR from the translated file name.
echo The search includes all subfolders, including disc 1 and disc 2.
echo.

python "%SCRIPT%" "%TRANSLATED%" --overwrite
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to patch the final 4 sectors.
    pause
    exit /b 1
)

echo.
echo Done.
echo Output file has _lastsector_fixed before .pss.
pause
