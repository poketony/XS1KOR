"""isobuild.py — Xenosaga Episode I 원본 레이아웃 고정 ISO 패처.

이 모듈은 ISO를 재배열하지 않는다. 원본 이미지를 바이트 단위로 복사한 뒤,
원본과 크기가 같은 루트 파일만 원래 extent에 덮어쓴다. LBA, ISO9660 메타
데이터, DVD-9 레이어 경계, 전체 이미지 크기는 원본 그대로 유지된다.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path


SECTOR = 0x800


class IsoLayoutError(ValueError):
    """원본 레이아웃을 보존할 수 없는 ISO 또는 교체 파일."""


@dataclass(frozen=True)
class IsoFileEntry:
    name: str
    lba: int
    size: int
    layer: int
    lba_rel: int
    dir_record_abs: int

    @property
    def end_byte(self) -> int:
        return self.lba * SECTOR + self.size


@dataclass(frozen=True)
class IsoLayout:
    layer1_files: list[IsoFileEntry]
    layer2_files: list[IsoFileEntry]
    layer2_base: int | None
    layer1_pvd_sector: int
    layer2_pvd_sector: int | None
    layer1_root_dir_abs: int
    layer1_root_dir_size: int
    layer2_root_dir_abs: int | None
    layer2_root_dir_size: int | None
    iso_size: int

    @property
    def files(self) -> list[IsoFileEntry]:
        return self.layer1_files + self.layer2_files


def _both_u16(data: bytes, offset: int, label: str) -> int:
    little = struct.unpack_from("<H", data, offset)[0]
    big = struct.unpack_from(">H", data, offset + 2)[0]
    if little != big:
        raise IsoLayoutError(f"{label}: little/big-endian values differ")
    return little


def _both_u32(data: bytes, offset: int, label: str) -> int:
    little = struct.unpack_from("<I", data, offset)[0]
    big = struct.unpack_from(">I", data, offset + 4)[0]
    if little != big:
        raise IsoLayoutError(f"{label}: little/big-endian values differ")
    return little


def _read_pvd(stream, sector: int) -> bytes:
    stream.seek(sector * SECTOR)
    pvd = stream.read(SECTOR)
    if len(pvd) != SECTOR:
        raise IsoLayoutError(f"PVD sector {sector}: truncated")
    if pvd[0] != 1 or pvd[1:6] != b"CD001" or pvd[6] != 1:
        raise IsoLayoutError(f"PVD sector {sector}: invalid ISO9660 descriptor")
    if _both_u16(pvd, 128, f"PVD sector {sector} block size") != SECTOR:
        raise IsoLayoutError(f"PVD sector {sector}: logical block size is not {SECTOR}")
    _both_u32(pvd, 80, f"PVD sector {sector} volume size")
    return pvd


def _is_pvd_at(stream, sector: int, total_sectors: int) -> bool:
    if sector < 16 or sector >= total_sectors:
        return False
    stream.seek(sector * SECTOR)
    return stream.read(7) == b"\x01CD001\x01"


def _find_layer2_base(stream, iso_size: int, layer1_pvd: bytes) -> int | None:
    total_sectors = iso_size // SECTOR

    # 이 디스크의 L0 PVD volume-space 값은 두 번째 PVD의 절대 섹터와
    # 일치한다. 우선 이 규격 정보를 사용하고, 변형 덤프만 중앙 탐색한다.
    layer1_volume_sectors = _both_u32(layer1_pvd, 80, "layer 1 volume size")
    candidate = layer1_volume_sectors - 16
    if candidate > 0 and _is_pvd_at(stream, candidate + 16, total_sectors):
        return candidate

    # base=0이면 L0의 sector 16 PVD를 L1으로 오인하므로 제외한다.
    start = max(1, total_sectors // 2 - 100)
    end = min(total_sectors // 2 + 1000, total_sectors - 16)
    for base in range(start, end):
        if _is_pvd_at(stream, base + 16, total_sectors):
            return base
    return None


def _parse_layer(
    stream,
    pvd_sector: int,
    lba_offset: int,
    layer: int,
    iso_size: int,
) -> tuple[list[IsoFileEntry], int, int]:
    pvd = _read_pvd(stream, pvd_sector)
    root_lba_rel = _both_u32(pvd, 158, f"layer {layer} root LBA")
    root_size = _both_u32(pvd, 166, f"layer {layer} root size")
    root_abs_byte = (lba_offset + root_lba_rel) * SECTOR
    if root_abs_byte + root_size > iso_size:
        raise IsoLayoutError(f"layer {layer} root directory is outside the image")

    stream.seek(root_abs_byte)
    read_size = _sectors(root_size) * SECTOR
    data = stream.read(read_size)
    if len(data) != read_size:
        raise IsoLayoutError(f"layer {layer} root directory is truncated")

    entries: list[IsoFileEntry] = []
    position = 0
    while position < root_size:
        record_len = data[position]
        if record_len == 0:
            position = ((position // SECTOR) + 1) * SECTOR
            continue
        if record_len < 34 or position + record_len > len(data):
            raise IsoLayoutError(
                f"layer {layer} root record @ {position:#x} is malformed"
            )
        name_len = data[position + 32]
        if 33 + name_len > record_len:
            raise IsoLayoutError(
                f"layer {layer} root name @ {position:#x} exceeds its record"
            )
        name = data[position + 33 : position + 33 + name_len].decode(
            "ascii", "replace"
        )
        lba_rel = _both_u32(
            data,
            position + 2,
            f"layer {layer} record {name!r} LBA",
        )
        size = _both_u32(
            data,
            position + 10,
            f"layer {layer} record {name!r} size",
        )
        flags = data[position + 25]
        if name not in ("\x00", "\x01") and not (flags & 2):
            absolute_lba = lba_offset + lba_rel
            if absolute_lba * SECTOR + size > iso_size:
                raise IsoLayoutError(f"{name}: extent is outside the image")
            entries.append(
                IsoFileEntry(
                    name=name,
                    lba=absolute_lba,
                    size=size,
                    layer=layer,
                    lba_rel=lba_rel,
                    dir_record_abs=root_abs_byte + position,
                )
            )
        position += record_len
    return entries, root_abs_byte, root_size


def parse_iso_layout(iso_path: Path) -> IsoLayout:
    """ISO9660의 두 레이어와 모든 루트 파일 extent를 검증해 읽는다."""
    iso_path = Path(iso_path)
    iso_size = iso_path.stat().st_size
    if iso_size % SECTOR:
        raise IsoLayoutError(f"ISO size is not sector aligned: {iso_size:,}")

    with iso_path.open("rb") as stream:
        layer1_pvd = _read_pvd(stream, 16)
        layer1, layer1_root_abs, layer1_root_size = _parse_layer(
            stream,
            pvd_sector=16,
            lba_offset=0,
            layer=1,
            iso_size=iso_size,
        )
        layer2_base = _find_layer2_base(stream, iso_size, layer1_pvd)
        layer2: list[IsoFileEntry] = []
        layer2_pvd_sector = None
        layer2_root_abs = None
        layer2_root_size = None
        if layer2_base is not None:
            layer2_pvd_sector = layer2_base + 16
            layer2, layer2_root_abs, layer2_root_size = _parse_layer(
                stream,
                pvd_sector=layer2_pvd_sector,
                lba_offset=layer2_base,
                layer=2,
                iso_size=iso_size,
            )

    return IsoLayout(
        layer1_files=sorted(layer1, key=lambda entry: entry.lba),
        layer2_files=sorted(layer2, key=lambda entry: entry.lba),
        layer2_base=layer2_base,
        layer1_pvd_sector=16,
        layer2_pvd_sector=layer2_pvd_sector,
        layer1_root_dir_abs=layer1_root_abs,
        layer1_root_dir_size=layer1_root_size,
        layer2_root_dir_abs=layer2_root_abs,
        layer2_root_dir_size=layer2_root_size,
        iso_size=iso_size,
    )


def _copy_exact(source, output, size: int, chunk: int = 16 * 1024 * 1024) -> None:
    remaining = size
    while remaining:
        data = source.read(min(chunk, remaining))
        if not data:
            raise EOFError(f"source ended with {remaining:,} bytes left")
        output.write(data)
        remaining -= len(data)


def _compare_file_slice(
    iso_path: Path,
    replacement: Path,
    iso_offset: int,
    replacement_offset: int,
    size: int,
    chunk: int = 4 * 1024 * 1024,
) -> bool:
    with iso_path.open("rb") as iso, replacement.open("rb") as source:
        iso.seek(iso_offset)
        source.seek(replacement_offset)
        remaining = size
        while remaining:
            take = min(chunk, remaining)
            if iso.read(take) != source.read(take):
                return False
            remaining -= take
    return True


def _layout_signature(layout: IsoLayout) -> tuple:
    files = tuple(
        (entry.name, entry.lba, entry.size, entry.layer, entry.lba_rel)
        for entry in layout.files
    )
    return layout.iso_size, layout.layer2_base, files


def rebuild_iso(
    orig_iso: Path,
    out_iso: Path,
    replacements: dict[str, Path],
    layout: IsoLayout | None = None,
    progress=print,
) -> None:
    """원본 이미지 복사본의 기존 extent에 같은 크기의 파일만 기록한다."""
    orig_iso = Path(orig_iso)
    out_iso = Path(out_iso)
    layout = layout or parse_iso_layout(orig_iso)

    if orig_iso.resolve() == out_iso.resolve():
        raise IsoLayoutError("output ISO must not overwrite the original ISO")
    if orig_iso.stat().st_size != layout.iso_size:
        raise IsoLayoutError("source ISO changed after its layout was parsed")

    by_name: dict[str, IsoFileEntry] = {}
    ambiguous: set[str] = set()
    for entry in layout.files:
        if entry.name in by_name:
            ambiguous.add(entry.name)
        by_name[entry.name] = entry
    unknown = sorted(set(replacements) - set(by_name))
    if unknown:
        raise IsoLayoutError(f"replacement is not in the ISO root: {unknown}")
    duplicate_targets = sorted(set(replacements) & ambiguous)
    if duplicate_targets:
        raise IsoLayoutError(f"replacement name is ambiguous between layers: {duplicate_targets}")

    normalized: dict[str, Path] = {}
    for name, replacement in replacements.items():
        replacement = Path(replacement)
        if not replacement.is_file():
            raise FileNotFoundError(replacement)
        entry = by_name[name]
        replacement_size = replacement.stat().st_size
        if replacement_size != entry.size:
            raise IsoLayoutError(
                f"{name}: replacement size {replacement_size:,} != "
                f"original extent size {entry.size:,}; layout would change"
            )
        normalized[name] = replacement

    # L1의 마지막 파일이 L2 시스템 영역과 겹치는 원본 구조를 보호한다.
    protected_ranges: list[tuple[int, int, str]] = []
    if layout.layer2_base is not None and layout.layer2_files:
        protected_start = layout.layer2_base * SECTOR
        protected_end = layout.layer2_files[0].lba * SECTOR
        protected_ranges.append((protected_start, protected_end, "layer 2 metadata"))

    for name, replacement in normalized.items():
        entry = by_name[name]
        entry_start = entry.lba * SECTOR
        entry_end = entry_start + entry.size
        for protected_start, protected_end, label in protected_ranges:
            overlap_start = max(entry_start, protected_start)
            overlap_end = min(entry_end, protected_end)
            if overlap_start >= overlap_end:
                continue
            if not _compare_file_slice(
                orig_iso,
                replacement,
                overlap_start,
                overlap_start - entry_start,
                overlap_end - overlap_start,
            ):
                raise IsoLayoutError(
                    f"{name}: replacement changes the overlapping {label} range "
                    f"{overlap_start // SECTOR}..{_sectors(overlap_end) - 1}"
                )

    progress(f"  [layout] fixed ISO size: {layout.iso_size:,} bytes")
    progress(f"  [layout] fixed layer 2 base: {layout.layer2_base}")
    out_iso.parent.mkdir(parents=True, exist_ok=True)
    with orig_iso.open("rb") as source, out_iso.open("wb") as output:
        _copy_exact(source, output, layout.iso_size)

    with out_iso.open("r+b") as output:
        for name, replacement in normalized.items():
            entry = by_name[name]
            progress(f"  [patch] {name} @ LBA {entry.lba} ({entry.size:,} bytes)")
            output.seek(entry.lba * SECTOR)
            with replacement.open("rb") as source:
                _copy_exact(source, output, entry.size)

    if out_iso.stat().st_size != layout.iso_size:
        raise IsoLayoutError("output ISO size changed")
    output_layout = parse_iso_layout(out_iso)
    if _layout_signature(output_layout) != _layout_signature(layout):
        raise IsoLayoutError("output ISO layout differs from the original")
    for name, replacement in normalized.items():
        entry = by_name[name]
        if not _compare_file_slice(
            out_iso,
            replacement,
            entry.lba * SECTOR,
            0,
            entry.size,
        ):
            raise IsoLayoutError(f"{name}: output read-back mismatch")
    progress(f"  [done] {out_iso}")


def _sectors(size: int) -> int:
    return (size + SECTOR - 1) // SECTOR
