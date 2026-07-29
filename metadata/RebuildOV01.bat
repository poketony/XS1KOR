@echo off
cd /d "%~dp0"
set "PYTHONDONTWRITEBYTECODE=1"
python ov01_elf_strings.py ov01.ovl.ori --translation ov01_strings.KOR.txt --replace-table XENOSAGA_KOR-JPN.json --current-source OV01.OVL --rebuild-output OV01_patched.OVL --replace-output
if errorlevel 1 echo [ERROR] OV01 rebuild failed.
pause
