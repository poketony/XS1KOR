@echo off
cd /d "%~dp0"
set "PYTHONDONTWRITEBYTECODE=1"
python patch_ov02_mail_spacing.py OV02.OVL OV02_strings.KOR.txt OV02_patched.OVL --replace-output
if errorlevel 1 echo [ERROR] OV02 rebuild failed.
pause
