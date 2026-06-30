@echo off
setlocal EnableDelayedExpansion
title M2V converter - original-size guard

echo [INFO] Convert edited video back to M2V.
echo [INFO] Output is retried until it is not larger than the detected original M2V.
echo.

:loop
if "%~1"=="" goto end

set "INPUT=%~f1"
set "DIR=%~dp1"
set "BASE=%~n1"
set "ORIGBASE=%~n1"
set "ORIG="
set "ORIGSIZE="
set "DURATION="
set "KBPS="

echo Processing: %~nx1

if /i "!ORIGBASE:~-4!"=="_KOR" set "ORIGBASE=!ORIGBASE:~0,-4!"
if /i "!ORIGBASE:~-6!"=="_fixed" set "ORIGBASE=!ORIGBASE:~0,-6!"

if exist "!DIR!!ORIGBASE!.m2v" set "ORIG=!DIR!!ORIGBASE!.m2v"
if not defined ORIG if exist "!DIR!!ORIGBASE!_video_0.m2v" set "ORIG=!DIR!!ORIGBASE!_video_0.m2v"

if not defined ORIG (
  for %%M in ("!DIR!*.m2v") do (
    set "CAND=%%~nxM"
    if /i not "!CAND:~-10!"=="_fixed.m2v" if /i not "!CAND:~-8!"=="_KOR.m2v" (
      if not defined ORIG set "ORIG=%%~fM"
    )
  )
)

if not defined ORIG (
  echo [ERROR] Original M2V not found in:
  echo !DIR!
  goto fail
)

for %%O in ("!ORIG!") do set "ORIGSIZE=%%~zO"
if not defined ORIGSIZE (
  echo [ERROR] Could not read original M2V size.
  goto fail
)

for /f "usebackq delims=" %%D in (`ffprobe -v error -show_entries format^=duration -of default^=nokey^=1:noprint_wrappers^=1 "%INPUT%"`) do set "DURATION=%%D"
if not defined DURATION (
  echo [ERROR] ffprobe could not read input duration.
  goto fail
)

for /f "usebackq delims=" %%K in (`python -c "import math,sys; orig=int(sys.argv[1]); dur=float(sys.argv[2]); kb=math.floor((orig*8/1000/dur)*0.965); print(max(500,min(7000,kb)))" "!ORIGSIZE!" "!DURATION!"`) do set "KBPS=%%K"
if not defined KBPS (
  echo [ERROR] Could not calculate bitrate.
  goto fail
)

echo [INFO] Original: !ORIG!
echo [INFO] Original size: !ORIGSIZE! bytes
echo [INFO] Duration: !DURATION! sec
echo [INFO] Initial bitrate: !KBPS!k

:retry
set "OUT=%~dpn1_fixed.m2v"
echo [INFO] Encoding with !KBPS!k

ffmpeg -y -i "%INPUT%" -vcodec mpeg2video -s 512x448 -r 30000/1001 -b:v !KBPS!k -maxrate !KBPS!k -minrate !KBPS!k -bufsize 1835k -bf 2 -g 18 -profile:v main -level:v main -pix_fmt yuv420p -an -f mpeg2video "!OUT!"
if errorlevel 1 goto fail

for %%O in ("!OUT!") do set "OUTSIZE=%%~zO"
echo [INFO] Output size: !OUTSIZE! bytes

if !OUTSIZE! GTR !ORIGSIZE! (
  echo [WARN] Output is larger than original. Retrying with lower bitrate.
  set /a STEP=!KBPS!/50
  if !STEP! LSS 20 set "STEP=20"
  set /a KBPS=!KBPS!-!STEP!
  if !KBPS! LSS 500 (
    echo [ERROR] Bitrate dropped below 500k but output is still too large.
    goto fail
  )
  del /f /q "!OUT!" >nul 2>nul
  goto retry
)

echo [INFO] OK: output is within original size limit.
echo.
shift
goto loop

:fail
echo.
echo [ERROR] Conversion failed: %~nx1
pause
exit /b 1

:end
echo.
echo Done.
pause
