"""
isobuild.py — PS2 DVD-9 ISO 스트리밍 리빌더.

원본 ISO의 레이아웃을 읽고, 지정된 파일을 교체(크기 변경 가능)하여
새 ISO를 생성한다. 파일이 커지면 후속 파일 전체를 shift하고
PVD/디렉터리 레코드를 갱신한다.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path


SECTOR = 0x800

# ---------------------------------------------------------------------------
# ISO 레이아웃 파싱
# ---------------------------------------------------------------------------

@dataclass
class IsoFileEntry:
    name: str           # "XENOSAGA.00;1"
    lba: int            # 원본 absolute LBA
    size: int           # 원본 바이트 크기
    layer: int          # 1 or 2
    lba_rel: int        # layer 내부 상대 LBA (layer 2에서 중요)
    dir_record_abs: int # 디렉터리 레코드의 ISO 내 절대 바이트 오프셋 (패치용)


@dataclass
class IsoLayout:
    layer1_files: list[IsoFileEntry]   # LBA 순
    layer2_files: list[IsoFileEntry]   # LBA 순
    layer2_base: int | None            # layer 2 시작 섹터 (없으면 None)
    layer1_pvd_sector: int             # 항상 16
    layer2_pvd_sector: int | None      # layer2_base + 16
    layer1_root_dir_abs: int           # root directory 절대 바이트 오프셋
    layer1_root_dir_size: int
    layer2_root_dir_abs: int | None
    layer2_root_dir_size: int | None
    iso_size: int


def parse_iso_layout(iso_path: Path) -> IsoLayout:
    """원본 ISO에서 전체 파일 레이아웃을 추출."""
    iso_size = iso_path.stat().st_size

    with open(iso_path, "rb") as f:
        # Layer 1 PVD
        l1_files, l1_root_abs, l1_root_size = _parse_layer(f, pvd_sector=16, lba_offset=0, layer=1)

        # Layer 2 PVD 탐색
        l2_base = _find_layer2_base_raw(f, iso_size)
        l2_files = []
        l2_root_abs = None
        l2_root_size = None
        l2_pvd = None
        if l2_base is not None:
            l2_pvd = l2_base + 16
            l2_files, l2_root_abs, l2_root_size = _parse_layer(
                f, pvd_sector=l2_pvd, lba_offset=l2_base, layer=2,
            )

    return IsoLayout(
        layer1_files=sorted(l1_files, key=lambda e: e.lba),
        layer2_files=sorted(l2_files, key=lambda e: e.lba),
        layer2_base=l2_base,
        layer1_pvd_sector=16,
        layer2_pvd_sector=l2_pvd,
        layer1_root_dir_abs=l1_root_abs,
        layer1_root_dir_size=l1_root_size,
        layer2_root_dir_abs=l2_root_abs,
        layer2_root_dir_size=l2_root_size,
        iso_size=iso_size,
    )


def _find_layer2_base_raw(f, iso_size: int) -> int | None:
    total = iso_size // SECTOR
    for sec in range(total // 2 - 100, min(total // 2 + 1000, total)):
        f.seek(sec * SECTOR)
        if f.read(6) == b"\x01CD001":
            return sec - 16
    return None


def _parse_layer(f, pvd_sector: int, lba_offset: int, layer: int):
    f.seek(pvd_sector * SECTOR)
    pvd = f.read(SECTOR)
    if pvd[0:6] != b"\x01CD001":
        return [], 0, 0

    root_lba_rel = struct.unpack_from("<I", pvd, 156 + 2)[0]
    root_size = struct.unpack_from("<I", pvd, 156 + 10)[0]
    root_abs_sector = lba_offset + root_lba_rel
    root_abs_byte = root_abs_sector * SECTOR

    f.seek(root_abs_byte)
    read_amt = ((root_size + SECTOR - 1) // SECTOR) * SECTOR
    data = f.read(read_amt)

    entries = []
    pos = 0
    while pos < root_size:
        rec_len = data[pos]
        if rec_len == 0:
            next_sec = ((pos // SECTOR) + 1) * SECTOR
            if next_sec >= root_size:
                break
            pos = next_sec
            continue
        if pos + 33 > len(data):
            break
        name_len = data[pos + 32]
        if pos + 33 + name_len > len(data):
            break
        name = data[pos + 33: pos + 33 + name_len].decode("ascii", "replace")
        lba_rel = struct.unpack_from("<I", data, pos + 2)[0]
        size = struct.unpack_from("<I", data, pos + 10)[0]
        flags = data[pos + 25]

        if name not in ("\x00", "\x01") and not (flags & 2):
            abs_lba = lba_offset + lba_rel
            entries.append(IsoFileEntry(
                name=name,
                lba=abs_lba,
                size=size,
                layer=layer,
                lba_rel=lba_rel,
                dir_record_abs=root_abs_byte + pos,
            ))
        pos += rec_len

    return entries, root_abs_byte, root_size


# ---------------------------------------------------------------------------
# ISO 리빌드
# ---------------------------------------------------------------------------

def rebuild_iso(
    orig_iso: Path,
    out_iso: Path,
    replacements: dict[str, Path],
    layout: IsoLayout,
) -> None:
    """
    원본 ISO에서 파일을 교체하여 새 ISO를 생성.

    replacements: {"XENOSAGA.02;1": Path("kansei/repack00/XENOSAGA.02"), ...}
    크기가 달라도 OK — 후속 파일을 shift하고 메타데이터를 갱신한다.
    """
    # 새 크기 계산
    def _new_size(entry: IsoFileEntry) -> int:
        if entry.name in replacements:
            return replacements[entry.name].stat().st_size
        return entry.size

    def _sectors(size: int) -> int:
        return (size + SECTOR - 1) // SECTOR

    # Layer 1 새 LBA 할당
    # 원본 gap(메타데이터/디렉터리/패딩)을 보존하면서 파일 크기 변경분만 shift.
    all_l1 = layout.layer1_files
    new_l1_lbas: dict[str, int] = {}
    shift = 0  # 누적 shift (섹터 수)
    if all_l1:
        for i, ent in enumerate(all_l1):
            new_l1_lbas[ent.name] = ent.lba + shift
            old_sectors = _sectors(ent.size)
            new_sectors_val = _sectors(_new_size(ent))
            shift += new_sectors_val - old_sectors

    # Layer 1 끝
    if all_l1:
        last = all_l1[-1]
        l1_data_end = new_l1_lbas[last.name] + _sectors(_new_size(last))
    else:
        l1_data_end = 0

    # Layer 2 base 재계산
    orig_l2_base = layout.layer2_base
    new_l2_base: int | None = None
    new_l2_lbas: dict[str, int] = {}
    l2_meta_size = 0

    if orig_l2_base is not None and layout.layer2_files:
        l2_first_file_rel = layout.layer2_files[0].lba_rel
        l2_meta_size = l2_first_file_rel

        # layer2 base: layer1 shift 반영 + L1 데이터 끝보다 뒤에 있어야 함
        # 원본에서 .13이 L2 base를 넘는 경우가 있음 (negative gap)
        new_l2_base = max(orig_l2_base + shift, l1_data_end)

        l2_shift = 0
        for i, ent in enumerate(layout.layer2_files):
            new_l2_lbas[ent.name] = (new_l2_base + ent.lba_rel) + l2_shift
            old_sectors = _sectors(ent.size)
            new_sectors_val = _sectors(_new_size(ent))
            l2_shift += new_sectors_val - old_sectors

    if layout.layer2_files and new_l2_lbas:
        last_l2 = layout.layer2_files[-1]
        new_iso_end = new_l2_lbas[last_l2.name] + _sectors(_new_size(last_l2))
    else:
        new_iso_end = l1_data_end
    new_iso_size = new_iso_end * SECTOR

    # 요약 출력
    orig_size = layout.iso_size
    print(f"  [layout] layer1 data end: {l1_data_end} (was {all_l1[-1].lba + _sectors(all_l1[-1].size) if all_l1 else 0})")
    if new_l2_base is not None:
        print(f"  [layout] layer2 base: {new_l2_base} (was {orig_l2_base})")
    print(f"  [layout] new ISO: {new_iso_size:,} bytes (was {orig_size:,}, diff={new_iso_size - orig_size:+,})")

    # ISO 쓰기: 원본을 스트리밍 복사하면서 파일 영역만 교체/shift
    with open(orig_iso, "rb") as src, open(out_iso, "wb") as dst:
        # 전략: 원본 ISO를 구간별로 복사
        #   - 파일 사이 gap(메타데이터 영역)은 원본에서 그대로 복사
        #   - 교체 파일은 새 소스에서 복사
        #   - shift가 발생하면 gap도 shift된 위치에 기록

        # Layer 1: 시작 ~ 첫 파일 전까지 (메타데이터)
        first_file_sector = all_l1[0].lba if all_l1 else 296
        src.seek(0)
        _stream_copy(src, dst, first_file_sector * SECTOR)

        # Layer 1 파일들 + 사이 gap
        for i, ent in enumerate(all_l1):
            new_lba = new_l1_lbas[ent.name]

            # 이 파일 앞의 gap (원본에서 이전 파일 끝 ~ 이 파일 시작 사이)
            if i == 0:
                # 첫 파일 전 gap은 이미 위에서 복사함
                pass
            else:
                prev = all_l1[i - 1]
                prev_end = prev.lba + _sectors(prev.size)
                gap_start = prev_end
                gap_size = ent.lba - gap_start
                if gap_size > 0:
                    src.seek(gap_start * SECTOR)
                    _stream_copy(src, dst, gap_size * SECTOR)

            # 파일 위치 맞춤 (shift로 인한 미세 조정)
            _pad_to(dst, new_lba * SECTOR)

            # 파일 데이터
            if ent.name in replacements:
                _write_file(dst, replacements[ent.name])
            else:
                src.seek(ent.lba * SECTOR)
                _stream_copy(src, dst, ent.size)
            _pad_to_sector(dst)

        # Layer 1 마지막 파일 뒤 ~ Layer 2 시작 사이 gap
        if new_l2_base is not None and orig_l2_base is not None:
            if all_l1:
                last = all_l1[-1]
                orig_last_end = last.lba + _sectors(last.size)
                gap_size = orig_l2_base - orig_last_end
                if gap_size > 0:
                    src.seek(orig_last_end * SECTOR)
                    _stream_copy(src, dst, gap_size * SECTOR)

            _pad_to(dst, new_l2_base * SECTOR)
            # Layer 2 메타 영역 복사
            src.seek(orig_l2_base * SECTOR)
            _stream_copy(src, dst, l2_meta_size * SECTOR)

            # Layer 2 파일들 + 사이 gap
            for i, ent in enumerate(layout.layer2_files):
                new_lba = new_l2_lbas[ent.name]

                if i > 0:
                    prev = layout.layer2_files[i - 1]
                    prev_end = prev.lba + _sectors(prev.size)
                    gap_size = ent.lba - prev_end
                    if gap_size > 0:
                        src.seek(prev_end * SECTOR)
                        _stream_copy(src, dst, gap_size * SECTOR)

                _pad_to(dst, new_lba * SECTOR)

                if ent.name in replacements:
                    _write_file(dst, replacements[ent.name])
                else:
                    src.seek(ent.lba * SECTOR)
                    _stream_copy(src, dst, ent.size)
                _pad_to_sector(dst)

        # 최종 크기 맞춤
        _pad_to(dst, new_iso_size)

    # PVD / 디렉터리 레코드 패치
    with open(out_iso, "r+b") as f:
        # Layer 1 디렉터리 레코드 패치
        for ent in all_l1:
            new_lba = new_l1_lbas[ent.name]
            new_sz = _new_size(ent)
            _patch_dir_record(f, ent.dir_record_abs, new_lba, new_sz)

        # Layer 2 디렉터리 레코드 패치
        if new_l2_base is not None and orig_l2_base is not None:
            shift = new_l2_base - orig_l2_base
            for ent in layout.layer2_files:
                new_abs_lba = new_l2_lbas[ent.name]
                new_rel_lba = new_abs_lba - new_l2_base
                new_sz = _new_size(ent)
                new_rec_abs = ent.dir_record_abs + shift * SECTOR
                _patch_dir_record(f, new_rec_abs, new_rel_lba, new_sz)

            # Layer 2 PVD 내 volume space size 갱신
            l2_vol_size = new_iso_end - new_l2_base
            new_l2_pvd_abs = (new_l2_base + 16) * SECTOR
            f.seek(new_l2_pvd_abs + 80)
            f.write(struct.pack("<I", l2_vol_size))
            f.write(struct.pack(">I", l2_vol_size))

            # Layer 1 PVD 의 volume_space 갱신
            # PCSX2 는 이 값으로 Layer 2 PVD 위치를 결정한다.
            # 실제 L2 메타 시작 위치는 파일 쓰기 시점에 l1_data_end 이후로 밀렸을 수 있음.
            # 정확한 PVD 위치 = 실제로 기록된 L2 base + 16
            actual_l2_base = max(new_l2_base, l1_data_end)
            new_l1_vol_space = actual_l2_base + 16
            f.seek(layout.layer1_pvd_sector * SECTOR + 80)
            f.write(struct.pack("<I", new_l1_vol_space))
            f.write(struct.pack(">I", new_l1_vol_space))

    print(f"  [done] {out_iso}")


def _patch_dir_record(f, abs_offset: int, new_lba: int, new_size: int):
    """ISO9660 디렉터리 레코드의 LBA와 size를 양-엔디안으로 패치."""
    f.seek(abs_offset + 2)
    f.write(struct.pack("<I", new_lba))
    f.write(struct.pack(">I", new_lba))
    f.seek(abs_offset + 10)
    f.write(struct.pack("<I", new_size))
    f.write(struct.pack(">I", new_size))


def _stream_copy(src, dst, n: int, chunk: int = 8 * 1024 * 1024):
    remaining = n
    while remaining > 0:
        take = min(remaining, chunk)
        data = src.read(take)
        if not data:
            break
        dst.write(data)
        remaining -= len(data)


def _write_file(dst, path: Path, chunk: int = 8 * 1024 * 1024):
    with open(path, "rb") as f:
        while True:
            data = f.read(chunk)
            if not data:
                break
            dst.write(data)


def _pad_to(dst, target: int):
    cur = dst.tell()
    if cur < target:
        gap = target - cur
        zeros = b"\x00" * min(gap, 1024 * 1024)
        while gap > 0:
            take = min(gap, len(zeros))
            dst.write(zeros[:take])
            gap -= take


def _pad_to_sector(dst):
    cur = dst.tell()
    rem = cur % SECTOR
    if rem:
        dst.write(b"\x00" * (SECTOR - rem))
