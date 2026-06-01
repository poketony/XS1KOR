#!/usr/bin/env python3
"""
thinktool.py - Xenosaga think/scene 자동 패치 툴

사용법:
  python thinktool.py scan              # think→scene 매핑 스캔 후 think_map.json 저장
  python thinktool.py patch <thinkNNN.bin>  # 수정된 think 파일을 대응하는 .a 파일들에 패치
  python thinktool.py info <thinkNNN.bin>   # 특정 think가 어느 .a 파일에 있는지 조회
"""

import os
import sys
import json

THINK_DIR = "think"
SCENE_DIR = "scene"
MAP_FILE  = "think_map.json"


# ── 유틸 ──────────────────────────────────────────────────────────────────────

def find_all_occurrences(haystack: bytes, needle: bytes) -> list[int]:
    """needle이 haystack에서 등장하는 모든 오프셋 반환"""
    offsets = []
    start = 0
    nlen = len(needle)
    while True:
        pos = haystack.find(needle, start)
        if pos == -1:
            break
        offsets.append(pos)
        start = pos + nlen
    return offsets


def load_map() -> dict:
    if not os.path.exists(MAP_FILE):
        print(f"[!] {MAP_FILE} 없음. 먼저 scan을 실행해.")
        sys.exit(1)
    with open(MAP_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# ── 서브커맨드 ────────────────────────────────────────────────────────────────

def cmd_scan():
    """think 폴더와 scene 폴더를 스캔해 매핑 JSON 생성"""

    # think 파일 목록
    think_files = sorted(
        f for f in os.listdir(THINK_DIR)
        if f.lower().startswith("think") and f.lower().endswith(".bin")
    )
    if not think_files:
        print(f"[!] {THINK_DIR}/ 안에 think*.bin 파일이 없어.")
        sys.exit(1)

    # scene .a 파일 목록
    scene_files = sorted(
        f for f in os.listdir(SCENE_DIR)
        if f.lower().endswith(".a")
    )
    if not scene_files:
        print(f"[!] {SCENE_DIR}/ 안에 .a 파일이 없어.")
        sys.exit(1)

    print(f"[*] think 파일 {len(think_files)}개 / scene 파일 {len(scene_files)}개 스캔 시작")

    # scene 파일 전부 미리 읽기 (반복 I/O 방지)
    scene_data: dict[str, bytes] = {}
    for sf in scene_files:
        path = os.path.join(SCENE_DIR, sf)
        with open(path, "rb") as f:
            scene_data[sf] = f.read()
        print(f"  로드: {sf} ({len(scene_data[sf])} bytes)")

    mapping: dict[str, list] = {}  # {think파일명: [{scene, offset}, ...]}

    for tf in think_files:
        tpath = os.path.join(THINK_DIR, tf)
        with open(tpath, "rb") as f:
            tbytes = f.read()

        hits = []
        for sf, sbytes in scene_data.items():
            offsets = find_all_occurrences(sbytes, tbytes)
            for off in offsets:
                hits.append({"scene": sf, "offset": off})
                print(f"  [HIT] {tf} → {sf} @ 0x{off:08X}")

        mapping[tf] = hits
        if not hits:
            print(f"  [--] {tf} → 매칭 없음")

    with open(MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)

    total_hits = sum(len(v) for v in mapping.values())
    print(f"\n[완료] {MAP_FILE} 저장. 총 {total_hits}건 매핑.")


def cmd_patch(think_name: str):
    """수정된 think 파일을 대응하는 .a 파일들에 인플레이스 패치"""

    mapping = load_map()

    # 파일명 정규화
    base = os.path.basename(think_name)
    if base not in mapping:
        print(f"[!] '{base}' 가 {MAP_FILE}에 없어. scan을 다시 실행했는지 확인해.")
        sys.exit(1)

    tpath = os.path.join(THINK_DIR, base)
    if not os.path.exists(tpath):
        print(f"[!] {tpath} 파일이 없어.")
        sys.exit(1)

    with open(tpath, "rb") as f:
        new_bytes = f.read()

    hits = mapping[base]
    if not hits:
        print(f"[!] {base}에 매핑된 .a 파일이 없어.")
        return

    for entry in hits:
        sf   = entry["scene"]
        off  = entry["offset"]
        spath = os.path.join(SCENE_DIR, sf)

        with open(spath, "r+b") as f:
            # 현재 데이터와 크기 검증
            f.seek(0, 2)
            file_size = f.tell()

            if off + len(new_bytes) > file_size:
                print(f"  [ERR] {sf} @ 0x{off:08X} : 범위 초과, 건너뜀")
                continue

            # 덮어쓰기
            f.seek(off)
            f.write(new_bytes)
            print(f"  [OK]  {sf} @ 0x{off:08X} 패치 완료 ({len(new_bytes)} bytes)")

    print(f"\n[완료] {base} 패치 끝.")


def cmd_info(think_name: str):
    """특정 think 파일의 매핑 정보 출력"""

    mapping = load_map()
    base = os.path.basename(think_name)

    if base not in mapping:
        print(f"[!] '{base}' 가 {MAP_FILE}에 없어.")
        sys.exit(1)

    hits = mapping[base]
    if not hits:
        print(f"{base} → 매핑된 .a 파일 없음")
    else:
        print(f"{base} → {len(hits)}건")
        for h in hits:
            print(f"  {h['scene']}  @ 0x{h['offset']:08X}")


# ── 진입점 ────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd == "scan":
        cmd_scan()

    elif cmd == "patch":
        if len(sys.argv) < 3:
            print("사용법: python thinktool.py patch <thinkNNN.bin>")
            sys.exit(1)
        cmd_patch(sys.argv[2])

    elif cmd == "info":
        if len(sys.argv) < 3:
            print("사용법: python thinktool.py info <thinkNNN.bin>")
            sys.exit(1)
        cmd_info(sys.argv[2])

    else:
        print(f"[!] 알 수 없는 커맨드: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
