#!/usr/bin/env python3
"""
Patch a translated PSS by copying the original PSS tail from the 4th-last
00 00 01 BA pack start through EOF.

The output is written as *_lastsector_fixed.pss by default. The translated
input is not modified in place.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import sys


PACK_MAGIC = b"\x00\x00\x01\xBA"
END_MAGIC = b"\x00\x00\x01\xB9"
COPY_SECTORS = 4
DEFAULT_ORIGINAL_ROOT = pathlib.Path(r"E:\제노사가1 영상 작업소\original")


def read_bytes(path: pathlib.Path, limit: int | None = None) -> bytes:
    with path.open("rb") as handle:
        return handle.read() if limit is None else handle.read(limit)


def find_pack_offsets(data: bytes) -> list[int]:
    offsets: list[int] = []
    start = 0
    while True:
        found = data.find(PACK_MAGIC, start)
        if found < 0:
            return offsets
        offsets.append(found)
        start = found + 1


def tail_start_from_last_packs(path: pathlib.Path, *, limit: int | None = None) -> tuple[int, int, int]:
    data = read_bytes(path, limit)
    size = len(data)
    offsets = find_pack_offsets(data)
    if len(offsets) < COPY_SECTORS:
        raise ValueError(
            f"Need at least {COPY_SECTORS} pack starts, found {len(offsets)}: {path}"
        )
    start = offsets[-COPY_SECTORS]
    tail_size = size - start
    return start, tail_size, len(offsets)


def output_path_for(translated: pathlib.Path) -> pathlib.Path:
    return translated.with_name(f"{translated.stem}_lastsector_fixed{translated.suffix}")


def original_name_from_translated(translated: pathlib.Path) -> str:
    stem = translated.stem
    marker = "_KOR"
    if marker.lower() in stem.lower():
        index = stem.lower().index(marker.lower())
        stem = stem[:index]
    return f"{stem}.pss"


def find_original_for_translated(translated: pathlib.Path, root: pathlib.Path) -> pathlib.Path:
    if not root.exists():
        raise FileNotFoundError(f"Original search root not found: {root}")

    original_name = original_name_from_translated(translated)
    matches = [
        path
        for path in root.rglob("*.pss")
        if path.name.lower() == original_name.lower()
    ]
    if not matches:
        raise FileNotFoundError(f"Original PSS not found under {root}: {original_name}")
    if len(matches) > 1:
        joined = "\n".join(f"  {match}" for match in matches[:20])
        raise ValueError(f"Multiple original PSS matches found for {original_name}:\n{joined}")
    return matches[0]


def patch_last_four_sectors(
    original: pathlib.Path,
    translated: pathlib.Path,
    output: pathlib.Path,
    *,
    truncate_to_original: bool,
    overwrite: bool,
) -> None:
    if not original.exists():
        raise FileNotFoundError(f"Original PSS not found: {original}")
    if not translated.exists():
        raise FileNotFoundError(f"Translated PSS not found: {translated}")
    if original.resolve() == translated.resolve():
        raise ValueError("Original and translated paths are the same file.")
    if output.exists() and not overwrite:
        raise FileExistsError(f"Output already exists. Use --overwrite: {output}")

    original_size = original.stat().st_size
    translated_size = translated.stat().st_size
    output_size_limit = (
        original_size
        if truncate_to_original and translated_size > original_size
        else translated_size
    )

    original_tail_start, original_tail_size, original_pack_count = tail_start_from_last_packs(original)
    with original.open("rb") as handle:
        handle.seek(original_tail_start)
        original_tail = handle.read()
    if not original_tail.endswith(END_MAGIC):
        tail = " ".join(f"{byte:02X}" for byte in original_tail[-16:])
        raise ValueError(
            f"Original tail does not end with 00 00 01 B9: {original}\n"
            f"Last 16 bytes: {tail}"
        )

    target_start, target_tail_size, translated_pack_count = tail_start_from_last_packs(
        translated,
        limit=output_size_limit,
    )
    if target_tail_size < original_tail_size:
        raise ValueError(
            "Translated output tail is smaller than original 4-sector tail: "
            f"{target_tail_size} < {original_tail_size}"
        )

    shutil.copyfile(translated, output)
    with output.open("r+b") as handle:
        handle.seek(target_start)
        handle.write(original_tail)
        if truncate_to_original:
            handle.flush()
            handle.seek(0, 2)
            if handle.tell() > original_size:
                handle.truncate(original_size)

    final_size = output.stat().st_size
    with output.open("rb") as handle:
        handle.seek(max(0, final_size - 16))
        final_tail = handle.read()
    if not final_tail.endswith(END_MAGIC):
        tail = " ".join(f"{byte:02X}" for byte in final_tail)
        raise ValueError(f"Output does not end with 00 00 01 B9. Last bytes: {tail}")

    print(f"Original     : {original}")
    print(f"Original name: {original.name}")
    print(f"Translated   : {translated}")
    print(f"Output       : {output}")
    print(f"Copied packs : last {COPY_SECTORS} original pack sectors")
    print(f"Original tail start offset : 0x{original_tail_start:X}")
    print(f"Output target start offset : 0x{target_start:X}")
    print(f"Copied bytes : {len(original_tail)}")
    print(f"Original size: {original_size} bytes")
    print(f"Input size   : {translated_size} bytes")
    print(f"Output size  : {final_size} bytes")
    print(f"Original pack starts seen   : {original_pack_count}")
    print(f"Translated pack starts used : {translated_pack_count}")
    print("End code     : verified 00 00 01 B9")
    if truncate_to_original and translated_size > original_size:
        print("Size policy  : translated input was larger; output was truncated to original size")
    else:
        print("Size policy  : no truncation needed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Patch translated PSS with original final 4 pack sectors.")
    parser.add_argument(
        "paths",
        type=pathlib.Path,
        nargs="+",
        help="Either TRANSLATED.pss, or ORIGINAL.pss TRANSLATED.pss.",
    )
    parser.add_argument(
        "--original-root",
        type=pathlib.Path,
        default=DEFAULT_ORIGINAL_ROOT,
        help="Root folder used when only translated PSS is provided.",
    )
    parser.add_argument("--out", type=pathlib.Path)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--no-truncate",
        action="store_true",
        help="Do not truncate output to original size when translated input is larger.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if len(args.paths) == 1:
        translated = args.paths[0].resolve()
        original = find_original_for_translated(translated, args.original_root.resolve()).resolve()
        print(f"Auto original search root: {args.original_root.resolve()}")
        print(f"Auto original target     : {original_name_from_translated(translated)}")
        print(f"Auto original matched    : {original}")
    elif len(args.paths) == 2:
        original = args.paths[0].resolve()
        translated = args.paths[1].resolve()
    else:
        raise ValueError("Expected either TRANSLATED.pss or ORIGINAL.pss TRANSLATED.pss.")
    output = args.out.resolve() if args.out else output_path_for(translated).resolve()
    patch_last_four_sectors(
        original,
        translated,
        output,
        truncate_to_original=not args.no_truncate,
        overwrite=args.overwrite,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
