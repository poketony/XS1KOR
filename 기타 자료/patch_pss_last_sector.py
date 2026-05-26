#!/usr/bin/env python3
"""
Replace the final tail sectors of a translated PSS with the original ones.

Required final sector shape:
  start: 00 00 01 BA
  size : 0x4004 bytes
  end  : 00 00 01 B9

By default this writes a new *_lastsector_fixed.pss file and does not modify
the translated input in place.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import sys


PACK_MAGIC = b"\x00\x00\x01\xBA"
END_MAGIC = b"\x00\x00\x01\xB9"
PACK_SIZE = 0x4004
DEFAULT_SCAN_SECTORS = 100
DEFAULT_FF_RUN = 300


def read_sector(path: pathlib.Path, offset: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(offset)
        sector = handle.read(PACK_SIZE)
    if len(sector) != PACK_SIZE:
        raise ValueError(
            f"Could not read a complete 0x{PACK_SIZE:X}-byte sector at 0x{offset:X}: {path}"
        )
    return sector


def final_sector(path: pathlib.Path) -> tuple[int, bytes, int]:
    size = path.stat().st_size
    if size < PACK_SIZE:
        raise ValueError(f"File is smaller than one 0x{PACK_SIZE:X}-byte sector: {path}")

    offset = size - PACK_SIZE
    sector = read_sector(path, offset)
    if not sector.startswith(PACK_MAGIC):
        raise ValueError(
            f"Final 0x{PACK_SIZE:X}-byte block does not start with 00 00 01 BA: {path}"
        )
    return offset, sector, size


def target_sector(path: pathlib.Path, output_size: int) -> tuple[int, bytes]:
    if output_size < PACK_SIZE:
        raise ValueError(f"Target output size is smaller than one sector: {output_size} bytes")

    offset = output_size - PACK_SIZE
    sector = read_sector(path, offset)
    if not sector.startswith(PACK_MAGIC):
        raise ValueError(
            "The sector that would become the final output sector does not start "
            f"with 00 00 01 BA: offset 0x{offset:X}, file {path}"
        )
    return offset, sector


def require_end_code(label: str, sector: bytes, path: pathlib.Path) -> None:
    if not sector.endswith(END_MAGIC):
        tail = " ".join(f"{byte:02X}" for byte in sector[-16:])
        raise ValueError(
            f"{label} final sector does not end with 00 00 01 B9: {path}\n"
            f"Last 16 bytes: {tail}"
        )


def longest_ff_run(data: bytes) -> int:
    longest = 0
    current = 0
    for byte in data:
        if byte == 0xFF:
            current += 1
            if current > longest:
                longest = current
        else:
            current = 0
    return longest


def detect_tail_start(
    path: pathlib.Path,
    *,
    scan_sectors: int,
    ff_run_threshold: int,
) -> tuple[int, int, int]:
    final_offset, _, size = final_sector(path)
    first_offset = max(0, final_offset - ((scan_sectors - 1) * PACK_SIZE))
    best_offset = final_offset
    best_run = 0

    for offset in range(first_offset, final_offset + 1, PACK_SIZE):
        sector = read_sector(path, offset)
        if not sector.startswith(PACK_MAGIC):
            raise ValueError(
                f"Tail scan found a non-pack sector at 0x{offset:X}: {path}"
            )
        run = longest_ff_run(sector)
        if run > best_run:
            best_offset = offset
            best_run = run
        if run >= ff_run_threshold:
            return offset, run, size - offset

    return final_offset, best_run, PACK_SIZE


def output_path_for(translated: pathlib.Path) -> pathlib.Path:
    return translated.with_name(f"{translated.stem}_lastsector_fixed{translated.suffix}")


def patch_last_sector(
    original: pathlib.Path,
    translated: pathlib.Path,
    output: pathlib.Path,
    *,
    truncate_to_original: bool,
    overwrite: bool,
    scan_sectors: int,
    ff_run_threshold: int,
) -> None:
    if not original.exists():
        raise FileNotFoundError(f"Original PSS not found: {original}")
    if not translated.exists():
        raise FileNotFoundError(f"Translated PSS not found: {translated}")
    if original.resolve() == translated.resolve():
        raise ValueError("Original and translated paths are the same file.")
    if output.exists() and not overwrite:
        raise FileExistsError(f"Output already exists. Use --overwrite: {output}")

    original_offset, original_sector, original_size = final_sector(original)
    require_end_code("Original", original_sector, original)
    tail_offset, ff_run, tail_size = detect_tail_start(
        original,
        scan_sectors=scan_sectors,
        ff_run_threshold=ff_run_threshold,
    )
    tail_sector_count = (original_size - tail_offset + PACK_SIZE - 1) // PACK_SIZE
    with original.open("rb") as handle:
        handle.seek(tail_offset)
        original_tail = handle.read(tail_size)
    if len(original_tail) != tail_size:
        raise ValueError(f"Could not read original tail from 0x{tail_offset:X}: {original}")

    translated_size = translated.stat().st_size
    output_size_limit = (
        original_size
        if truncate_to_original and translated_size > original_size
        else translated_size
    )
    if output_size_limit < tail_size:
        raise ValueError(
            f"Output size limit is smaller than tail copy size: {output_size_limit} < {tail_size}"
        )
    translated_offset = output_size_limit - tail_size
    translated_sector = read_sector(translated, translated_offset)
    if not translated_sector.startswith(PACK_MAGIC):
        raise ValueError(
            "The translated sector corresponding to the original tail start does not "
            f"start with 00 00 01 BA: offset 0x{translated_offset:X}, file {translated}"
        )

    shutil.copyfile(translated, output)
    with output.open("r+b") as handle:
        handle.seek(translated_offset)
        handle.write(original_tail)
        if truncate_to_original:
            handle.flush()
            handle.seek(0, 2)
            current_size = handle.tell()
            if current_size > original_size:
                handle.truncate(original_size)

    _, output_sector, final_size = final_sector(output)
    require_end_code("Output", output_sector, output)

    print(f"Original     : {original}")
    print(f"Translated   : {translated}")
    print(f"Output       : {output}")
    print(f"Tail source  : {original.name}")
    print(f"Original last sector offset   : 0x{original_offset:X}")
    print(f"Original tail start offset    : 0x{tail_offset:X}")
    print(f"Output tail target offset     : 0x{translated_offset:X}")
    print(f"Sector size  : 0x{PACK_SIZE:X} bytes")
    print(f"Tail sectors : {tail_sector_count}")
    print(f"Tail bytes   : {tail_size}")
    print(f"FF run found : {ff_run} bytes")
    print("End code     : verified 00 00 01 B9")
    print(f"Original size: {original_size} bytes")
    print(f"Input size   : {translated_size} bytes")
    print(f"Output size  : {final_size} bytes")
    translated_tail = " ".join(f"{byte:02X}" for byte in translated_sector[-4:])
    print(f"Input target sector tail      : {translated_tail}")
    if truncate_to_original and translated_size > original_size:
        print("Size policy  : translated input was larger; output was truncated to original size")
    else:
        print("Size policy  : no truncation needed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Patch translated PSS final pack sector from original PSS.")
    parser.add_argument("original", type=pathlib.Path)
    parser.add_argument("translated", type=pathlib.Path)
    parser.add_argument("--out", type=pathlib.Path)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--no-truncate",
        action="store_true",
        help="Do not truncate output to original size when translated input is larger.",
    )
    parser.add_argument(
        "--scan-sectors",
        type=int,
        default=DEFAULT_SCAN_SECTORS,
        help="How many final sectors of the original PSS to scan for a long 0xFF run.",
    )
    parser.add_argument(
        "--ff-run",
        type=int,
        default=DEFAULT_FF_RUN,
        help="Minimum consecutive 0xFF bytes that marks the original tail copy start.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    original = args.original.resolve()
    translated = args.translated.resolve()
    output = args.out.resolve() if args.out else output_path_for(translated).resolve()

    patch_last_sector(
        original,
        translated,
        output,
        truncate_to_original=not args.no_truncate,
        overwrite=args.overwrite,
        scan_sectors=args.scan_sectors,
        ff_run_threshold=args.ff_run,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
