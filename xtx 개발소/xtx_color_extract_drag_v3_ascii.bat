@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM XTX color extract drag batch v3 - ASCII / no BOM

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
    echo Usage: drag .xtx file^(s^) onto this BAT.
    pause
    exit /b 1
)

:loop
if "%~1"=="" goto done

set "XTX=%~f1"
set "EXT=%~x1"
set "LEX=%~dpn1.lex"
set "OUT=%~dpn1_out"

if /I not "!EXT!"==".xtx" (
    echo [SKIP] Not an XTX file: "!XTX!"
    shift
    goto loop
)

echo.
echo ============================================================
echo [XTX] "!XTX!"
echo [OUT] "!OUT!"

if exist "!LEX!" (
    echo [LEX] "!LEX!"
    %PY% "%SCRIPT%" extract "!XTX!" --lex "!LEX!" --out "!OUT!" --save-full
) else (
    echo [WARN] LEX not found. Grayscale/fallback extract:
    echo        "!LEX!"
    %PY% "%SCRIPT%" extract "!XTX!" --out "!OUT!" --save-full
)

if errorlevel 1 (
    echo.
    echo [ERROR] Extract failed: "!XTX!"
)

shift
goto loop

:done
echo.
echo Done.
pause
