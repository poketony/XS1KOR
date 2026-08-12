"""
main.py — Xenosaga Episode I 한글화 메인 빌드 도구.

사용법
------
    python main.py unpack          xenosaga/ → hataraku/ (모든 세트 언팩)
    python main.py repack          hataraku/ + xenosaga/ → kansei/ ISO 생성

디렉터리 구조
-------------
    xenosaga/        원본 ISO에서 추출한 파일들 (읽기 전용 취급)
    hataraku/        작업 영역 (수정할 파일들)
        root/        ISO 루트 파일 작업 영역 (SLPS_290.02, OV*.OVL 등)
        out00/       그룹 0 (.00) — 엔진 코어/폰트/UI
            manifest.json
            tree/...
        out10/       그룹 1 (.10) — 스트리밍 AV
        out20/       그룹 2 (.20) — 스트리밍 AV (후반부)
    kansei/          최종 출력 — 패치된 ISO 및 중간 파일
    tsuru/           빌드 도구 라이브러리 (건드리지 말 것)
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

# tsuru/ 라이브러리를 import path에 추가
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tsuru"))

from xenoarc import SECTOR, discover_set, open_virtual, parse_toc  # noqa: E402

# ---------------------------------------------------------------------------
# 경로 설정
# ---------------------------------------------------------------------------

XENOSAGA_DIR = ROOT / "Original" / "iso"   # ISO에서 추출한 원본 파일들
HATARAKU_DIR = ROOT / "hataraku"            # 작업 영역 (수정할 파일들)
HATARAKU_ROOT_DIR = ROOT / "hataraku" / "root"  # ISO 루트 파일 작업 영역
KANSEI_DIR = ROOT / "kansei"                # 최종 출력 ISO
ISO_PATH = ROOT / "Original" / "Xenosaga Episode I - Der Wille zur Macht.iso"

# ISO 루트 파일 중 아카이브(XENOSAGA.*)는 별도 처리되므로 제외
# 나머지 전부 hataraku/root/ 에 추출
ROOT_EXCLUDE_PREFIX = "XENOSAGA."

# 각 그룹의 TOC 파일명
GROUPS = [
    {"suffix": ".00", "toc": "XENOSAGA.00", "label": "그룹 0 (엔진 코어)"},
    {"suffix": ".10", "toc": "XENOSAGA.10", "label": "그룹 1 (스트리밍)"},
    {"suffix": ".20", "toc": "XENOSAGA.20", "label": "그룹 2 (스트리밍)"},
]


# ---------------------------------------------------------------------------
# unpack 명령
# ---------------------------------------------------------------------------

def _extract_iso():
    """원본 ISO에서 모든 파일을 Original/iso/ 에 추출한다."""
    from isobuild import parse_iso_layout

    if not ISO_PATH.exists():
        print(f"[error] ISO not found: {ISO_PATH}")
        sys.exit(1)

    XENOSAGA_DIR.mkdir(parents=True, exist_ok=True)

    layout = parse_iso_layout(ISO_PATH)
    all_files = layout.layer1_files + layout.layer2_files

    # IOP 디렉터리 파일도 추출 (별도 처리)
    iop_dir = XENOSAGA_DIR / "IOP"
    iop_dir.mkdir(exist_ok=True)

    # IOP 파일은 pycdlib 또는 직접 파싱으로 추출
    _extract_iop_files()

    already = sum(1 for f in all_files if (XENOSAGA_DIR / f.name.replace(";1", "")).exists())
    if already == len(all_files):
        print(f"[iso] already extracted ({already} files). skipping.")
        return

    print(f"[iso] extracting {len(all_files)} files from ISO → {XENOSAGA_DIR.relative_to(ROOT)}")

    with open(ISO_PATH, "rb") as iso:
        for ent in all_files:
            out_name = ent.name.replace(";1", "")
            out_path = XENOSAGA_DIR / out_name
            if out_path.exists() and out_path.stat().st_size == ent.size:
                continue
            print(f"  {out_name} (lba={ent.lba}, {ent.size:,} bytes)")
            iso.seek(ent.lba * SECTOR)
            remaining = ent.size
            with open(out_path, "wb") as f:
                while remaining > 0:
                    take = min(remaining, 8 * 1024 * 1024)
                    buf = iso.read(take)
                    if not buf:
                        break
                    f.write(buf)
                    remaining -= len(buf)

    # SYSTEM.CNF 도 추출 (부팅 검증용)
    _extract_small_iso_files()
    print(f"[iso] extraction complete.")


def _extract_iop_files():
    """ISO에서 IOP 디렉터리 파일들을 추출."""
    iop_dir = XENOSAGA_DIR / "IOP"
    iop_dir.mkdir(exist_ok=True)

    try:
        import pycdlib
        iso = pycdlib.PyCdlib()
        iso.open(str(ISO_PATH))
        try:
            for child in iso.list_children(iso_path="/IOP"):
                if child.is_dir():
                    continue
                name = child.file_identifier().decode("ascii", "replace").replace(";1", "")
                out = iop_dir / name
                if out.exists():
                    continue
                iso.get_file_from_iso(str(out), iso_path=f"/IOP/{name};1")
        finally:
            iso.close()
    except Exception:
        pass  # IOP 파일 없어도 아카이브 언팩에는 지장 없음


def _extract_small_iso_files():
    """SYSTEM.CNF, SLPS_290.02, OVxx 등 소형 파일 추출."""
    try:
        import pycdlib
        iso = pycdlib.PyCdlib()
        iso.open(str(ISO_PATH))
        try:
            for child in iso.list_children(iso_path="/"):
                if child.is_dir():
                    continue
                name = child.file_identifier().decode("ascii", "replace")
                clean = name.replace(";1", "")
                if clean.startswith("XENOSAGA."):
                    continue  # 대형 파일은 _extract_iso에서 처리
                out = XENOSAGA_DIR / clean
                if out.exists():
                    continue
                iso.get_file_from_iso(str(out), iso_path=f"/{name}")
        finally:
            iso.close()
    except Exception:
        pass


def cmd_unpack():
    """ISO에서 파일을 추출하고, 3개 아카이브 그룹을 hataraku/ 에 언팩한다."""
    import json

    # 1단계: ISO → Original/iso/
    _extract_iso()

    HATARAKU_DIR.mkdir(parents=True, exist_ok=True)

    for grp in GROUPS:
        toc_path = XENOSAGA_DIR / grp["toc"]
        if not toc_path.exists():
            print(f"[skip] {grp['toc']} not found")
            continue

        out_dir = HATARAKU_DIR / f"out{grp['suffix'][1:]}"
        tree_dir = out_dir / "tree"
        manifest_path = out_dir / "manifest.json"

        print(f"\n=== {grp['label']}: {grp['toc']} → {out_dir.relative_to(ROOT)} ===")

        # 이미 manifest가 있으면 skip 가능 (재실행 안전)
        if manifest_path.exists():
            print(f"  [info] manifest already exists. skipping full unpack.")
            print(f"  [info] to re-unpack, delete {manifest_path}")
            continue

        arc = discover_set(toc_path)
        chain = open_virtual(arc)
        tree_dir.mkdir(parents=True, exist_ok=True)

        total = len(arc.entries)
        print(f"  entries: {total}, chunks: {[p.name for p in arc.chunk_paths]}")

        manifest_entries = []
        try:
            for i, ent in enumerate(arc.entries):
                out_path = tree_dir / ent.path
                # 이미 존재하는 파일은 건너뜀 (사용자가 미리 넣어둔 수정본 보호)
                if not out_path.exists():
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    chain.seek(ent.byte_offset)
                    remaining = ent.size
                    with open(out_path, "wb") as f:
                        while remaining > 0:
                            take = min(remaining, 4 * 1024 * 1024)
                            data = chain.read(take)
                            if not data:
                                break
                            f.write(data)
                            remaining -= len(data)

                manifest_entries.append({
                    "path": ent.path,
                    "lba": ent.lba,
                    "size": ent.size,
                    "layer2": ent.alt_lba is not None,
                    "alt_lba": ent.alt_lba,
                    "order": i,
                })

                if (i + 1) % 500 == 0 or i == total - 1:
                    print(f"  [{i + 1}/{total}] {ent.path}")
        finally:
            chain.close()

        manifest = {
            "toc_file": toc_path.name,
            "chunks": [p.name for p in arc.chunk_paths],
            "header_sectors": arc.header_sectors,
            "sector": SECTOR,
            "entries": manifest_entries,
        }
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        print(f"  [done] {len(manifest_entries)} entries, manifest written.")

    # 루트 파일 추출: hataraku/root/ 에 복사 (이미 있으면 skip)
    _unpack_root_files()

    print("\n=== unpack complete ===")


# ---------------------------------------------------------------------------
# 루트 파일 (SLPS_290.02, OV*.OVL 등) 언팩 / 리팩 헬퍼
# ---------------------------------------------------------------------------

def _unpack_root_files():
    """
    ISO 루트의 실행 파일/소형 파일들을 hataraku/root/ 에 복사.
    이미 존재하는 파일은 건드리지 않는다 (사용자 수정본 보호).
    """
    HATARAKU_ROOT_DIR.mkdir(parents=True, exist_ok=True)

    import shutil as _shutil

    found = []
    for src in sorted(XENOSAGA_DIR.iterdir()):
        if not src.is_file():
            continue
        if src.name.upper().startswith(ROOT_EXCLUDE_PREFIX):
            continue
        dst = HATARAKU_ROOT_DIR / src.name
        if dst.exists():
            found.append(f"  [skip] {src.name} (already in hataraku/root/)")
        else:
            _shutil.copy2(src, dst)
            found.append(f"  [copy] {src.name} ({src.stat().st_size:,} bytes) → hataraku/root/")

    if found:
        print("\n=== root files ===")
        for line in found:
            print(line)
    print(f"  [info] edit files in hataraku/root/ then repack to apply.")


def _collect_root_replacements(layout) -> dict[str, Path]:
    """
    hataraku/root/ 의 파일을 Original/iso/ 원본과 비교해
    변경된 파일만 replacements dict 에 추가한다.
    크기 불변 조건을 검사하고, 위반 시 경고 후 skip.

    반환: {\"SLPS_290.02;1\": Path(...), ...}
    """
    import hashlib

    result: dict[str, Path] = {}
    if not HATARAKU_ROOT_DIR.exists():
        return result

    # layout 에서 이름→엔트리 매핑 구성 (Layer 1 + Layer 2)
    all_entries = layout.layer1_files + layout.layer2_files
    entry_map = {e.name: e for e in all_entries}  # e.g. "SLPS_290.02;1"

    for mod_file in sorted(HATARAKU_ROOT_DIR.iterdir()):
        if not mod_file.is_file():
            continue
        if mod_file.name.upper().startswith(ROOT_EXCLUDE_PREFIX):
            continue  # 아카이브는 별도 경로에서 처리

        iso_name = mod_file.name.upper() + ";1"  # ISO9660 versioned name
        ent = entry_map.get(iso_name)
        if ent is None:
            # 버전 접미사 없이도 시도
            iso_name_nv = mod_file.name.upper()
            ent = entry_map.get(iso_name_nv)
            if ent is None:
                print(f"  [warn] {mod_file.name}: not found in ISO layout — skipping")
                continue
            iso_name = iso_name_nv

        new_size = mod_file.stat().st_size
        if new_size != ent.size:
            print(f"  [error] {mod_file.name}: size changed "
                  f"({ent.size:,} → {new_size:,}) — root files must stay same size. skipping.")
            continue

        result[iso_name] = mod_file
        print(f"  [root] {mod_file.name} → patch @ ISO LBA {ent.lba}")

    return result


# ---------------------------------------------------------------------------
# repack 명령
# ---------------------------------------------------------------------------

def cmd_repack():
    """
    hataraku/ 의 수정된 파일들로 각 그룹을 리팩하고,
    원본 레이아웃을 유지한 ISO를 kansei/ 에 만든다.
    아카이브 내부에서는 필요한 파일만 빈 섹터로 재배치할 수 있지만,
    ISO 루트 파일의 크기·LBA와 레이어 경계는 절대 변경하지 않는다.
    """
    import json
    from isobuild import parse_iso_layout, rebuild_iso

    if not ISO_PATH.exists():
        print(f"[error] original ISO not found: {ISO_PATH}")
        return 1

    KANSEI_DIR.mkdir(parents=True, exist_ok=True)

    # ISO 파일 교체 맵: {"XENOSAGA.02;1": Path("kansei/repack00/XENOSAGA.02")}
    replacements: dict[str, Path] = {}

    for grp in GROUPS:
        out_dir = HATARAKU_DIR / f"out{grp['suffix'][1:]}"
        manifest_path = out_dir / "manifest.json"
        tree_dir = out_dir / "tree"

        if not manifest_path.exists():
            print(f"[skip] {grp['label']}: no manifest (run 'unpack' first)")
            continue

        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        entries_meta = sorted(manifest["entries"], key=lambda e: e["order"])
        orig_toc = XENOSAGA_DIR / grp["toc"]

        modified = _detect_modifications(entries_meta, tree_dir, orig_toc)
        if not modified:
            print(f"[skip] {grp['label']}: no modifications detected")
            continue

        print(f"\n=== repack {grp['label']} ===")
        repack_dir = KANSEI_DIR / f"repack{grp['suffix'][1:]}"

        from repack import repack as tsuru_repack
        tsuru_repack(out_dir, orig_toc, repack_dir)

        # 리팩 결과물 수집
        group_digit = grp["suffix"][1]
        toc_name = grp["toc"]
        new_toc = repack_dir / toc_name
        replacements[f"{toc_name};1"] = new_toc

        for sub in range(1, 10):
            chunk_name = f"XENOSAGA.{group_digit}{sub}"
            chunk_path = repack_dir / chunk_name
            if not chunk_path.exists():
                break
            replacements[f"{chunk_name};1"] = chunk_path

    if not replacements:
        print("[info] checking root files only...")

    # 크기 변경 여부 확인
    layout = parse_iso_layout(ISO_PATH)

    # hataraku/root/ 의 수정된 루트 파일 수집 (크기 불변 전제)
    print("\n=== root file check ===")
    root_replacements = _collect_root_replacements(layout)
    if root_replacements:
        replacements.update(root_replacements)
    else:
        print("  [info] no root file changes detected")

    if not replacements:
        print("[info] no modifications to apply")
        return 0

    all_files = layout.layer1_files + layout.layer2_files
    size_changed = False
    for ent in all_files:
        if ent.name in replacements:
            new_sz = replacements[ent.name].stat().st_size
            if new_sz != ent.size:
                delta = new_sz - ent.size
                print(f"  [size] {ent.name}: {ent.size:,} -> {new_sz:,} ({delta:+,})")
                size_changed = True

    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_iso = KANSEI_DIR / f"{ISO_PATH.stem}_{ts}{ISO_PATH.suffix}"

    if size_changed:
        print("\n[error] repack output changed an ISO root-file size.")
        print("        Fixed-layout ISO build cannot continue.")
        return 1

    # 동일 크기여도 isobuild의 보호 검사를 반드시 거친다. 특히 XENOSAGA.13과
    # 레이어 2 시스템 영역의 원본 중첩 바이트가 유지되는지 여기서 확인한다.
    print(f"\n=== fixed-layout ISO patch ===")
    rebuild_iso(ISO_PATH, out_iso, replacements, layout)

    print(f"\n=== done: {out_iso} ===")
    print(f"  ISO size: {out_iso.stat().st_size:,} bytes")
    return 0


def _detect_modifications(entries_meta, tree_dir: Path, orig_toc: Path) -> bool:
    """hataraku tree 파일이 원본과 바이트 단위로 다른지 빠르게 체크."""
    import hashlib
    arc = discover_set(orig_toc)
    chain = open_virtual(arc)
    try:
        for m in entries_meta:
            src = tree_dir / m["path"]
            if not src.exists():
                continue
            if src.stat().st_size != m["size"]:
                return True
            # 크기 같으면 첫 8KB 해시 비교 (빠른 스팟 체크)
            chain.seek(m["lba"] * SECTOR)
            orig_head = chain.read(min(m["size"], 8192))
            with open(src, "rb") as f:
                new_head = f.read(min(m["size"], 8192))
            if orig_head != new_head:
                return True
    finally:
        chain.close()
    return False


def _get_iso_file_map() -> dict[str, tuple[int, int]]:
    """
    ISO 내 파일별 (absolute_LBA, size) 매핑을 구한다.
    Layer 1 (PVD@16)과 Layer 2 (PVD@layer2_base+16)를 모두 탐색.
    """
    result = {}
    iso_size = ISO_PATH.stat().st_size

    with open(ISO_PATH, "rb") as f:
        # Layer 1: PVD at sector 16
        _parse_pvd_dir(f, pvd_sector=16, lba_offset=0, out=result)

        # Layer 2: PVD를 찾는다.
        # Layer 1 마지막 파일 끝 이후에 Layer 2 PVD가 있다.
        layer2_base = _find_layer2_base(f, iso_size)
        if layer2_base is not None:
            _parse_pvd_dir(
                f, pvd_sector=layer2_base + 16,
                lba_offset=layer2_base, out=result,
            )

    return result


LAYER2_PVD_CACHE: int | None = None


def _find_layer2_base(f, iso_size: int) -> int | None:
    """Layer 2 PVD를 찾아 base sector를 반환. 없으면 None."""
    global LAYER2_PVD_CACHE
    if LAYER2_PVD_CACHE is not None:
        return LAYER2_PVD_CACHE

    # Layer 1 끝 지점 추정: sector 2084928 부근 (하드코딩 대신 탐색)
    # PVD magic = b'\x01CD001' at sector offset 0
    # Layer 1 파일 끝 이후 16 ~ 100 섹터 내에 PVD가 있다
    total_sectors = iso_size // SECTOR
    # Layer 2는 대략 중간쯤 시작
    search_start = total_sectors // 2 - 100
    search_end = total_sectors // 2 + 1000

    for sec in range(search_start, search_end):
        f.seek(sec * SECTOR)
        header = f.read(6)
        if header == b"\x01CD001":
            # 이것이 PVD. base = sec - 16
            LAYER2_PVD_CACHE = sec - 16
            return LAYER2_PVD_CACHE
    return None


def _parse_pvd_dir(f, pvd_sector: int, lba_offset: int,
                   out: dict[str, tuple[int, int]]) -> None:
    """PVD에서 root directory를 읽어 파일 (name → (abs_lba, size)) 매핑을 채운다."""
    f.seek(pvd_sector * SECTOR)
    pvd = f.read(SECTOR)
    if pvd[0:6] != b"\x01CD001":
        return

    # root directory record at PVD+156
    root_lba_rel = struct.unpack_from("<I", pvd, 156 + 2)[0]
    root_size = struct.unpack_from("<I", pvd, 156 + 10)[0]
    root_abs = lba_offset + root_lba_rel

    f.seek(root_abs * SECTOR)
    read_amt = ((root_size + SECTOR - 1) // SECTOR) * SECTOR
    data = f.read(read_amt)

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
            out[name] = (abs_lba, size)

        pos += rec_len


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        print("commands: unpack, repack")
        return 2

    cmd = argv[1].lower()
    if cmd == "unpack":
        cmd_unpack()
        return 0
    elif cmd == "repack":
        return cmd_repack()
    else:
        print(f"Unknown command: {cmd}")
        print("commands: unpack, repack")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
