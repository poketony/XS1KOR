@echo off
setlocal

cd /d "%~dp0"
set "PYTHONDONTWRITEBYTECODE=1"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERR] python was not found in PATH.
  pause
  exit /b 1
)

if not exist OV10_elf_strings_KOR.txt (
  echo [ERR] OV10_elf_strings_KOR.txt was not found.
  echo       Run "python ov10_elf_strings.py migrate OV10.OVL OV10_strings_KOR.txt" only when you intentionally want to recreate it.
  pause
  exit /b 1
)

echo [1/2] Rebuilding the compatible SLPS font-spacing patch...
python patch_slps_menu_spacing.py slps_290.02 slps_290_strings.KOR.txt slps_290_patched.02 --replace-output
if errorlevel 1 (
  echo [ERR] SLPS rebuild failed.
  pause
  exit /b 1
)

echo [2/2] Rebuilding OV10_patched.OVL from OV10_elf_strings_KOR.txt...
python ov10_elf_strings.py rebuild OV10.OVL OV10_elf_strings_KOR.txt OV10_patched.OVL
if errorlevel 1 (
  echo [ERR] rebuild failed.
  pause
  exit /b 1
)

echo [OK] Done: slps_290_patched.02
echo [OK] Done: OV10_patched.OVL
pause
