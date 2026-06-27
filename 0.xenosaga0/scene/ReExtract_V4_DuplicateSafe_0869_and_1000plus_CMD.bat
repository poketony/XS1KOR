@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================================
echo XS1 EVT reextract - duplicate-safe v3 / CMD only
echo   ST0000-ST0869 listed files: shelter old txt, then reextract
echo   ST1000+ files: overwrite txt by reextracting
echo ============================================================
echo.

if not exist "xeno_evt.py" (
  echo [ERROR] xeno_evt.py was not found in this folder.
  echo Put xeno_evt_conservative_v3_duplicates.py here as xeno_evt.py.
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

set "SHELTER=__txt_shelter_v3_duplicate_safe_0000_0869"
if not exist "%SHELTER%" mkdir "%SHELTER%" >nul 2>nul

set "LOG=reextract_v3_duplicate_safe_0869_and_1000plus.log"
echo XS1 EVT reextract log > "%LOG%"
echo Python command: %PY% >> "%LOG%"
echo Shelter: %SHELTER% >> "%LOG%"
echo. >> "%LOG%"

echo [STEP 1] ST0000-ST0869 listed files: shelter old txt, then reextract
echo [STEP 1] ST0000-ST0869 listed files >> "%LOG%"

for %%B in (ST0010 ST0020 ST0030 ST0040 ST0050 ST0060 ST0070 ST0090 ST0100 ST0110 ST0120 ST0130 ST0140 ST0160 ST0210 ST0230 ST0231 ST0239 ST0240 ST0241 ST0249 ST0250 ST0251 ST0259 ST0260 ST0261 ST0269 ST0320 ST0321 ST0322 ST0324 ST0325 ST0329 ST0330 ST0339 ST0350 ST0351 ST0359 ST0370 ST0380 ST0381 ST0382 ST0389 ST0390 ST0400 ST0409 ST0410 ST0411 ST0412 ST0413 ST0419 ST0510 ST0511 ST0516 ST0520 ST0521 ST0530 ST0531 ST0536 ST0540 ST0541 ST0550 ST0551 ST0560 ST0561 ST0570 ST0571 ST0580 ST0581 ST0590 ST0591 ST0596 ST0600 ST0601 ST0606 ST0610 ST0611 ST0616 ST0620 ST0621 ST0630 ST0631 ST0670 ST0680 ST0681 ST0682 ST0690 ST0691 ST0700 ST0701 ST0720 ST0730 ST0810 ST0811 ST0819 ST0820 ST0829 ST0830 ST0831 ST0840 ST0849 ST0850 ST0851 ST0859) do (
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
