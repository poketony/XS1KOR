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
    echo This mode keeps the translated final GOP video to avoid original subtitle flashes.
    echo The original PSS is searched automatically under the default original root.
    echo.
    echo Output file has _safe_tail_kor_video before .pss.
    echo Use the normal Safe Tail BAT unless the original subtitle appears at the end.
    echo The output is refused if translated video or audio does not fit safely.
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
    echo === Safe tail graft with translated video: %%~fI ===
    echo Searching original PSS automatically from the input file name.
    echo.
    python "%SCRIPT%" "%%~fI" --keep-translated-video --overwrite
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
