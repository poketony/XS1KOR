@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
set "CHARMAP=%ROOT%XENOSAGA_KOR-JPN.json"

if not exist "%CHARMAP%" (
    echo [ERROR] Missing charmap: "%CHARMAP%"
    exit /b 1
)

where py >nul 2>nul
if %ERRORLEVEL%==0 (
    set "PYTHON_CMD=py -3"
) else (
    set "PYTHON_CMD=python"
)

%PYTHON_CMD% -c "import pathlib, sys; p = pathlib.Path(sys.argv[1]); sys.argv = [str(p)] + sys.argv[2:]; s = p.read_text(encoding='utf-8'); marker = ':' + 'PYTHON_PAYLOAD'; code = s.split(marker, 1)[1].lstrip('\r\n'); exec(compile(code, str(p), 'exec'))" "%~f0" "%ROOT%." "%CHARMAP%" %*
exit /b %ERRORLEVEL%

:PYTHON_PAYLOAD
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ENCODING_OUT = "euc_jis_2004"


def usage() -> None:
    print("Usage:")
    print("  Drag translated .txt files onto make_euc_jis_2004_txt.bat")
    print("  make_euc_jis_2004_txt.bat [--dry-run] <translated.txt> [more.txt ...]")
    print("")
    print("Writes each converted file next to the source as <original name>.new.")
    print("The source .txt files are not overwritten.")


def load_charmap(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    table = data.get("replace-table")
    if not isinstance(table, dict):
        raise ValueError(f"replace-table not found in {path}")
    return {str(k): str(v) for k, v in table.items()}


def build_replacer(charmap: dict[str, str]):
    keys = sorted(charmap, key=len, reverse=True)
    if not keys:
        return lambda text: (text, 0)

    pattern = re.compile("|".join(re.escape(key) for key in keys))

    def replace(text: str) -> tuple[str, int]:
        count = 0

        def repl(match: re.Match[str]) -> str:
            nonlocal count
            count += 1
            return charmap[match.group(0)]

        return pattern.sub(repl, text), count

    return replace


def main() -> int:
    if len(sys.argv) < 3:
        usage()
        return 2

    _root = Path(sys.argv[1]).resolve()
    charmap_path = Path(sys.argv[2]).resolve()
    args = sys.argv[3:]

    dry_run = False
    if args and args[0] == "--dry-run":
        dry_run = True
        args = args[1:]

    if not args:
        usage()
        return 2

    charmap = load_charmap(charmap_path)
    replace_text = build_replacer(charmap)

    txt_files = [Path(arg).resolve() for arg in args]

    converted = 0
    skipped = 0
    failed = 0

    print(f"[INFO] Charmap    : {charmap_path.name} ({len(charmap)} entries)")
    print(f"[INFO] Mode       : {'dry-run' if dry_run else 'write .new files'}")
    print("")

    for src in txt_files:
        if not src.is_file():
            skipped += 1
            print(f"[SKIP] {src} (not a file)")
            continue

        if src.suffix.lower() != ".txt":
            skipped += 1
            print(f"[SKIP] {src} (not a .txt file)")
            continue

        raw = src.read_bytes()

        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            skipped += 1
            print(f"[SKIP] {src} (not UTF-8)")
            continue

        mapped, replacements = replace_text(text)
        try:
            encoded = mapped.encode(ENCODING_OUT)
        except UnicodeEncodeError as exc:
            failed += 1
            print(f"[FAIL] {src} (cannot encode U+{ord(exc.object[exc.start]):04X} at char {exc.start})")
            continue

        converted += 1
        dst = Path(str(src) + ".new")
        print(f"[OK]   {src} -> {dst} ({replacements} replacements)")

        if not dry_run:
            dst.write_bytes(encoded)

    print("")
    print(f"[DONE] converted={converted}, skipped={skipped}, failed={failed}")
    if dry_run:
        print("[DONE] No files were written.")
    elif failed:
        print("[WARN] Some files failed. Check the [FAIL] lines above.")
    else:
        print("[DONE] Wrote .new files next to the source files.")

    return 1 if failed else 0


raise SystemExit(main())
