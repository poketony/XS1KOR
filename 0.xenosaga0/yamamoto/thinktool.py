#!/usr/bin/env python3
"""
thinktool.py - Xenosaga think/scene 자동 패치 툴

사용법:
  python thinktool.py scan              # think→scene 매핑 스캔 후 think_map.json 저장
  python thinktool.py patch <thinkNNN.bin|all> [--dry-run]  # 엔트리 재구성
  python thinktool.py info <thinkNNN.bin>   # 특정 think가 어느 .a 파일에 있는지 조회
"""

import os
import sys
import json
import hashlib
import struct
from pathlib import Path

HERE = Path(__file__).resolve().parent
THINK_DIR = HERE / "think"
SCENE_DIR = HERE / "scene"
MAP_FILE = HERE / "think_map.json"


# ── 유틸 ──────────────────────────────────────────────────────────────────────

def archive_bounds(data):
    """SLPS next_arc_size: cumulative ends relative to the 64-byte-aligned base."""
    if len(data) < 4:
        raise ValueError("Truncated archive")
    table, = struct.unpack_from("<I", data)
    if not table or table % 4 or table + 4 > len(data) or table // 4 >= 256:
        raise ValueError("Invalid archive table")
    base = (table + 64) & ~63
    points = [base] + [base + v for v in struct.unpack_from("<" + "I"*(table//4), data, 4)]
    if points[-1] != len(data) or any(a > b or a % 64 or b % 64 for a,b in zip(points,points[1:])):
        raise ValueError("Invalid archive entry bounds")
    return points


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save_map(mapping):
    temporary = MAP_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(mapping, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    os.replace(temporary, MAP_FILE)


def rebuild_archive(data, replacements):
    points = archive_bounds(data)
    if any(i < 0 or i >= len(points)-1 for i in replacements):
        raise ValueError("Invalid entry index")
    out = bytearray(data[:points[0]])
    for i,(a,b) in enumerate(zip(points,points[1:])):
        payload = replacements.get(i, data[a:b])
        if i in replacements and (not payload or len(payload) > 0x4000):
            raise ValueError("Think must be 1..16384 bytes")
        out.extend(payload)
        out.extend(bytes(-len(payload) % 64))
        struct.pack_into("<I", out, 4+i*4, len(out)-points[0])
    rebuilt = bytes(out)
    newpoints = archive_bounds(rebuilt)
    for i,(a,b) in enumerate(zip(points,points[1:])):
        expected = replacements.get(i, data[a:b])
        expected += bytes(-len(expected) % 64)
        if rebuilt[newpoints[i]:newpoints[i+1]] != expected:
            raise ValueError("Rebuild verification failed")
    return rebuilt


def validate_hit(hit, data):
    points = archive_bounds(data)
    if "entry" not in hit or "sha256" not in hit:
        raise ValueError("Legacy offset-only map: validated entry mapping is required")
    i = hit["entry"]
    if not isinstance(i, int) or not 0 <= i < len(points)-1:
        raise ValueError("Invalid mapped entry")
    if digest(data[points[i]:points[i+1]]) not in hit["sha256"]:
        raise ValueError(f"{hit['scene']}: entry {i} differs from the recorded content")
    return i


def load_map() -> dict:
    if not os.path.exists(MAP_FILE):
        print(f"[!] {MAP_FILE} 없음. 먼저 scan을 실행해.")
        sys.exit(1)
    with open(MAP_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# ── 서브커맨드 ────────────────────────────────────────────────────────────────

def cmd_scan():
    """Match whole allocated entries, retaining verified previous identities."""
    previous = load_map() if MAP_FILE.exists() else {}
    mapping = {p.name: [] for p in sorted(THINK_DIR.glob("think*.bin"))}
    candidates = {}
    for name in mapping:
        for p in (THINK_DIR/name, THINK_DIR/(name+".ori")):
            if p.exists():
                raw = p.read_bytes()
                if raw:
                    candidates.setdefault(raw+bytes(-len(raw)%64), set()).add(name)
    for path in sorted(SCENE_DIR.glob("*.a")):
        data = path.read_bytes()
        points = archive_bounds(data)
        known = {}
        for name,hits in previous.items():
            for hit in hits:
                if hit["scene"] == path.name:
                    i = validate_hit(hit, data)
                    known.setdefault(i, set()).add(name)
        for i,(a,b) in enumerate(zip(points,points[1:])):
            payload = data[a:b]
            for name in candidates.get(payload, set()) | known.get(i, set()):
                mapping.setdefault(name, []).append({"scene":path.name, "offset":a,
                    "entry":i, "sha256":[digest(payload)]})
    save_map(mapping)
    print(f"[완료] {sum(map(len,mapping.values()))}건 엔트리 매핑 저장")


def cmd_patch(think_name, dry_run=False):
    """Preflight all targets, then rebuild and atomically replace each archive."""
    mapping = load_map()
    base = os.path.basename(think_name)
    names = list(mapping) if base == "all" else [base]
    plans = {}
    unmapped = []
    for name in names:
        if name not in mapping:
            raise ValueError(f"Unknown think: {name}")
        raw = (THINK_DIR/name).read_bytes()
        if not raw or len(raw) > 0x4000:
            raise ValueError(f"{name}: Think must be 1..16384 bytes")
        if not mapping[name]:
            unmapped.append(name)
        for hit in mapping[name]:
            plans.setdefault(hit["scene"], []).append((name, hit, raw))
    if unmapped:
        print("[매핑 없음 / 적용 제외] " + ", ".join(unmapped))
    verified = {}
    for scene, rows in plans.items():
        if Path(scene).name != scene:
            raise ValueError("Invalid scene filename")
        data = (SCENE_DIR/scene).read_bytes()
        replacements = {}
        for name,hit,raw in rows:
            index = validate_hit(hit,data)
            # A formerly identical think can have several logical names.
            for alias,hits in mapping.items():
                if any(h["scene"] == scene and h.get("entry") == index for h in hits):
                    if (THINK_DIR/alias).read_bytes() != raw:
                        raise ValueError(f"{scene} entry {index}: conflicting aliases {name}, {alias}")
            replacements[index] = raw
        result = rebuild_archive(data,replacements)
        verified[scene] = (digest(data), replacements)
        print(f"  {scene}: {len(data)} -> {len(result)} bytes")
    if dry_run:
        print(f"[검증 완료] {len(plans)}개 아카이브, 파일 변경 없음")
        return
    for scene,(before,replacements) in verified.items():
        path = SCENE_DIR/scene
        data = path.read_bytes()
        if digest(data) != before:
            raise ValueError(f"{scene}: changed during patch")
        result = rebuild_archive(data,replacements)
        if result == data:
            continue
        points = archive_bounds(result)
        # Save both accepted versions first: interruption before/after replace
        # leaves a map that can still validate the archive and retry safely.
        for hits in mapping.values():
            for hit in hits:
                if hit["scene"] == scene:
                    i = hit["entry"]
                    oldpoints = archive_bounds(data)
                    hit["sha256"] = sorted({digest(data[oldpoints[i]:oldpoints[i+1]]),
                                            digest(result[points[i]:points[i+1]])})
                    hit["offset"] = points[i]
        temporary = path.with_suffix(".a.tmp")
        with temporary.open("wb") as f:
            f.write(result)
            f.flush()
            os.fsync(f.fileno())
        if temporary.read_bytes() != result:
            raise ValueError(f"{scene}: temporary file verification failed")
        save_map(mapping)
        os.replace(temporary,path)
    print(f"[완료] {len(plans)}개 아카이브 처리")


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
        cmd_patch(sys.argv[2], "--dry-run" in sys.argv[3:])

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
    try:
        main()
    except (ValueError, OSError) as error:
        print(f"[ERR] {error}", file=sys.stderr)
        sys.exit(1)
