@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================================
echo XS1 EVT reextract - conservative v2 / CMD only / no labels
echo   ST0000-ST0869 listed files: shelter old txt, then reextract
echo   ST1000+ files: overwrite txt by reextracting
echo ============================================================
echo.

if not exist "xeno_evt.py" (
  echo [ERROR] xeno_evt.py was not found in this folder.
  echo Put the conservative v2 script here as xeno_evt.py.
  pause
  exit /b 1
)

set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  where python3 >nul 2>nul
  if not errorlevel 1 set "PY=python3"
)
if not defined PY (
  echo [ERROR] Python was not found. Install Python or add it to PATH.
  pause
  exit /b 1
)

set "SHELTER=__txt_shelter_current_byte_diff_0000_0869"
if exist "%SHELTER%" (
  set /a IDX=1
  :FIND_SHELTER_NAME
  set "SHELTER=__txt_shelter_current_byte_diff_0000_0869_!IDX!"
  if exist "!SHELTER!" (
    set /a IDX+=1
    goto FIND_SHELTER_NAME
  )
)
mkdir "%SHELTER%" >nul 2>nul

set "LOG=reextract_current_byte_diff_0869_and_1000plus.log"
echo XS1 EVT reextract log > "%LOG%"
echo Python command: %PY% >> "%LOG%"
echo Shelter: %SHELTER% >> "%LOG%"
echo. >> "%LOG%"

echo [STEP 1] ST0000-ST0869 listed files: shelter old txt, then reextract
echo [STEP 1] ST0000-ST0869 listed files >> "%LOG%"

for %%B in (ST0040 ST0060 ST0100 ST0120 ST0130 ST0140 ST0160 ST0230 ST0240 ST0241 ST0259 ST0321 ST0329 ST0359 ST0389 ST0409 ST0419 ST0511 ST0520 ST0521 ST0610 ST0620 ST0621 ST0700 ST0730 ST0840 ST0849 ST0851 ST0859) do (
  if exist "%%B.evt" (
    echo [0869] %%B.evt
    echo [0869] %%B.evt >> "%LOG%"
    if exist "%%B.evt.txt" (
      move /Y "%%B.evt.txt" "%SHELTER%" >> "%LOG%" 2>&1
    ) else (
      echo [INFO] No existing %%B.evt.txt to shelter >> "%LOG%"
    )
    %PY% "xeno_evt.py" "%%B.evt" >> "%LOG%" 2>&1
    if errorlevel 1 (
      echo [ERROR] Extract failed: %%B.evt
      echo [ERROR] Extract failed: %%B.evt >> "%LOG%"
    )
  ) else (
    echo [WARN] Missing %%B.evt
    echo [WARN] Missing %%B.evt >> "%LOG%"
  )
)

echo.
echo [STEP 2] ST1000+ files: overwrite txt by reextracting
echo. >> "%LOG%"
echo [STEP 2] ST1000+ files >> "%LOG%"

for %%F in (ST*.evt) do (
  set "BASE=%%~nF"
  set "NUMSTR=!BASE:~2!"
  set /a NUM=1!NUMSTR!-10000 2>nul
  if !NUM! GEQ 1000 (
    echo [1000+] %%F
    echo [1000+] %%F >> "%LOG%"
    %PY% "xeno_evt.py" "%%F" >> "%LOG%" 2>&1
    if errorlevel 1 (
      echo [ERROR] Extract failed: %%F
      echo [ERROR] Extract failed: %%F >> "%LOG%"
    )
  )
)

echo.
echo [DONE]
echo Log: %LOG%
echo Shelter: %SHELTER%
pause
exit /b 0
