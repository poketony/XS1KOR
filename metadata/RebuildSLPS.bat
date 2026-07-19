@echo off
cd /d "%~dp0"
set "PYTHONDONTWRITEBYTECODE=1"
python patch_slps_menu_spacing.py slps_290.02 slps_290_strings.KOR.txt slps_290_patched.02 --replace-output
if errorlevel 1 echo [ERROR] SLPS rebuild failed.
pause
