@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "SCRIPT=%~dp0pss_safe_tail_graft.py"
if not exist "%SCRIPT%" (
    echo [ERROR] pss_safe_tail_graft.py was not found.
    echo Put this BAT file and pss_safe_tail_graft.py in the same folder.
    pause
    exit /b 1
)

if "%~1"=="" (
    echo Drop the ps2str-muxed PSS file onto this BAT file.
    echo The original PSS is searched automatically under the default original root.
    echo.
    echo Output file has _safe_tail before .pss.
    echo Video packets, GOPs, PTS, SCR, and ADPCM are preserved.
    echo Only the missing MPEG sequence-end marker is restored.
    echo Do not run any old last-sector patch step afterward.
    echo The output is refused if it would be larger than the original PSS.
    echo.
    pause
    exit /b 1
)

for %%I in (%*) do (
    if not exist "%%~fI" (
        echo.
        echo [ERROR] Input file was not found:
        echo %%~fI
        pause
        exit /b 1
    )
    if /i not "%%~xI"==".pss" (
        echo.
        echo [ERROR] Input file is not a .pss file:
        echo %%~fI
        pause
        exit /b 1
    )
    echo.
    echo === Safe MPEG termination patch: %%~fI ===
    echo Searching original PSS automatically from the input file name.
    echo.
    python "%SCRIPT%" "%%~fI" --overwrite
    if errorlevel 1 (
        echo.
        echo [ERROR] Failed: %%~fI
        pause
        exit /b 1
    )
)

echo.
echo Done.
pause
