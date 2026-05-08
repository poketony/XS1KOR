@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM XTX color import/rebuild drag batch v3 - ASCII / no BOM

set "SCRIPT=%~dp0xtx_tool_ver7_fixed_palette_formula.py"

if not exist "%SCRIPT%" (
    echo [ERROR] Script not found:
    echo "%SCRIPT%"
    echo Put this BAT in the same folder as xtx_tool_ver7_fixed_palette_formula.py
    pause
    exit /b 1
)

where python >nul 2>nul
if %errorlevel%==0 (
    set "PY=python"
) else (
    where py >nul 2>nul
    if %errorlevel%==0 (
        set "PY=py -3"
    ) else (
        echo [ERROR] python or py command not found.
        pause
        exit /b 1
    )
)

if "%~1"=="" (
    echo Usage: drag extracted folder^(s^) like BG1_out onto this BAT.
    pause
    exit /b 1
)

:loop
if "%~1"=="" goto done

set "FOLDER=%~f1"

if not exist "!FOLDER!\" (
    echo [SKIP] Not a folder: "!FOLDER!"
    shift
    goto loop
)

for %%A in ("!FOLDER!") do (
    set "PARENT=%%~dpA"
    set "BASENAME=%%~nxA"
)

set "BASE=!BASENAME!"
if /I "!BASE:~-4!"=="_out" set "BASE=!BASE:~0,-4!"
if /I "!BASE:~-10!"=="_extracted" set "BASE=!BASE:~0,-10!"

set "XTX=!PARENT!!BASE!.xtx"
set "LEX=!PARENT!!BASE!.lex"
set "OUT=!PARENT!!BASE!_rebuilt.xtx"

echo.
echo ============================================================
echo [FOLDER] "!FOLDER!"
echo [BASE]   "!BASE!"
echo [XTX]    "!XTX!"
echo [OUT]    "!OUT!"

if not exist "!XTX!" (
    echo [ERROR] Original XTX not found.
    echo Expected: "!XTX!"
    shift
    goto loop
)

if exist "!LEX!" (
    echo [LEX]    "!LEX!"
    %PY% "%SCRIPT%" import "!XTX!" "!FOLDER!" --lex "!LEX!" --out "!OUT!"
) else (
    echo [WARN] LEX not found. Grayscale/fallback import:
    echo        "!LEX!"
    %PY% "%SCRIPT%" import "!XTX!" "!FOLDER!" --out "!OUT!"
)

if errorlevel 1 (
    echo.
    echo [ERROR] Import/Rebuild failed: "!FOLDER!"
)

shift
goto loop

:done
echo.
echo Done.
pause
