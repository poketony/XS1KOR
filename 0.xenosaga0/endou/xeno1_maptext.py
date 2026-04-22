#!/usr/bin/env python3
"""
Xenosaga Episode I – Map Text Tool
대상 파일: mapex.bin, savemap.bin

[mapex.bin 구조]
  가변 길이 null-terminated EUC-JP 문자열들이 null 패딩과 함께 연속 배치.
  비어있지 않은 문자열만 추출/임포트 (offset, 원본 길이 보존).

[savemap.bin 구조]
  고정 레코드: 70 bytes × 38 records
    Field 1 (지역명): offset +0x00, 크기 0x21 (33 bytes), null-padded
    Field 2 (세부위치): offset +0x21, 크기 0x25 (37 bytes), null-padded

[Import 변환 파이프라인]
  한글 텍스트 → JSON 치환표(한글→한자) → EUC-JP 인코딩 → 파일 기록
"""

import json
import sys
import argparse
import copy
from pathlib import Path

# ──────────────────────────────────────────────
# 공통 유틸
# ──────────────────────────────────────────────

def load_replace_table(json_path: str) -> dict:
    """XENOSAGA_KOR-JPN.json 에서 replace-table 로드."""
    with open(json_path, encoding="utf-8-sig") as f:
        data = json.load(f)
    table = data.get("replace-table", {})
    if not table:
        raise ValueError("JSON에서 'replace-table' 키를 찾을 수 없습니다.")
    return table  # { '가': '亜', ... }


def apply_replace_table(text: str, table: dict) -> str:
    """한글 → 한자 치환 (한 글자씩)."""
    return "".join(table.get(ch, ch) for ch in text)


def encode_euc_jp(text: str, context: str = "") -> bytes:
    """EUC-JP 인코딩. 실패 시 오류 메시지와 함께 종료."""
    try:
        return text.encode("euc-jp")
    except (UnicodeEncodeError, UnicodeDecodeError) as e:
        sys.exit(f"[오류] EUC-JP 인코딩 실패 ({context}): {e!r}\n  텍스트: {text!r}")


# ──────────────────────────────────────────────
# mapex.bin
# ──────────────────────────────────────────────

def mapex_scan(data: bytes) -> list[dict]:
    """
    비어있지 않은 EUC-JP 문자열 블록을 스캔.
    usable = 텍스트 + 뒤따르는 null 패딩 (다음 블록 직전까지) → import 시 실제 가용 공간.
    반환: [{"idx": N, "offset": int, "end": int, "usable": int, "text": str}, ...]
    """
    # 1패스: non-empty 블록 위치 수집
    raw = []
    i = 0
    while i < len(data):
        j = data.find(b"\x00", i)
        if j == -1:
            j = len(data)
        chunk = data[i:j]
        if chunk:
            raw.append((i, j, chunk))
        i = j + 1

    # 2패스: EUC-JP 디코딩 + usable 계산
    entries = []
    for k, (off, end, chunk) in enumerate(raw):
        try:
            text = chunk.decode("euc-jp")
        except Exception:
            continue  # 바이너리 데이터 무시
        next_off = raw[k + 1][0] if k + 1 < len(raw) else len(data)
        usable = next_off - off  # 텍스트 + null 패딩 전체
        entries.append({"idx": len(entries), "offset": off, "end": end,
                        "usable": usable, "text": text})
    return entries


def mapex_extract(bin_path: str, out_json: str) -> None:
    data = Path(bin_path).read_bytes()
    entries = mapex_scan(data)

    result = []
    for e in entries:
        result.append({
            "idx":         e["idx"],
            "offset":      e["offset"],
            "end":         e["end"],
            "usable":      e["usable"],   # import 때 실제 쓸 수 있는 바이트 수
            "original":    e["text"],
            "translation": e["text"],     # 번역 작업용 필드
        })

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"[mapex] 추출 완료: {len(result)}개 문자열 → {out_json}")


def mapex_import(bin_path: str, in_json: str, out_bin: str,
                 replace_table: dict | None = None) -> None:
    data = bytearray(Path(bin_path).read_bytes())

    with open(in_json, encoding="utf-8") as f:
        entries = json.load(f)

    # usable을 bin에서 직접 재계산 (JSON에 usable 필드가 없는 구버전도 정확히 동작)
    bin_scan = {e["offset"]: e["usable"] for e in mapex_scan(bytes(data))}

    errors = []
    for e in entries:
        kor_text = e.get("translation", e.get("text", ""))

        # 한글 → 한자 치환
        if replace_table:
            converted = apply_replace_table(kor_text, replace_table)
        else:
            converted = kor_text

        new_bytes = encode_euc_jp(converted, context=f"idx={e['idx']}")

        offset = e["offset"]
        # bin에서 재계산한 usable 우선, 없으면 JSON 값, 최후엔 end-offset
        usable = bin_scan.get(offset, e.get("usable", e["end"] - offset))

        if len(new_bytes) > usable:
            errors.append(
                f"  [idx={e['idx']}] 새 텍스트({len(new_bytes)}B) > "
                f"가용 공간({usable}B): {kor_text!r}"
            )
            continue

        # in-place 덮어쓰기 + 남은 공간 전체 null 패딩
        data[offset:offset + usable] = new_bytes + b"\x00" * (usable - len(new_bytes))

    if errors:
        print(f"[mapex][경고] 공간 초과로 {len(errors)}개 항목 스킵:")
        print("\n".join(errors))

    Path(out_bin).write_bytes(data)
    print(f"[mapex] 임포트 완료 → {out_bin}")


# ──────────────────────────────────────────────
# savemap.bin
# ──────────────────────────────────────────────

SAVEMAP_RECORD_SIZE = 0x46   # 70 bytes
SAVEMAP_FIELD1_OFF  = 0x00
SAVEMAP_FIELD1_SIZE = 0x21   # 33 bytes (지역명)
SAVEMAP_FIELD2_OFF  = 0x21
SAVEMAP_FIELD2_SIZE = 0x21   # 33 bytes (세부위치, null-padded 텍스트)
SAVEMAP_TAIL_OFF    = 0x42   # +4 bytes 바이너리 (맵 ID 등, 건드리지 않음)


def savemap_read_field(data: bytes, base: int, off: int, size: int) -> str:
    """필드 텍스트 읽기. EUC-JP 디코딩 불가 시 '[ERR:hex]' 마커 반환."""
    raw = data[base + off: base + off + size]
    end = raw.find(b"\x00")
    chunk = raw[:end] if end >= 0 else raw
    if not chunk:
        return ""
    try:
        return chunk.decode("euc-jp")
    except Exception:
        return f"[ERR:{chunk.hex()}]"


def savemap_extract(bin_path: str, out_json: str) -> None:
    data = Path(bin_path).read_bytes()
    n_records = len(data) // SAVEMAP_RECORD_SIZE

    result = []
    for i in range(n_records):
        base = i * SAVEMAP_RECORD_SIZE
        f1 = savemap_read_field(data, base, SAVEMAP_FIELD1_OFF, SAVEMAP_FIELD1_SIZE)
        f2 = savemap_read_field(data, base, SAVEMAP_FIELD2_OFF, SAVEMAP_FIELD2_SIZE)
        result.append({
            "idx": i,
            "field1_original": f1,   # 지역명
            "field1_translation": f1,
            "field2_original": f2,   # 세부위치
            "field2_translation": f2,
        })

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"[savemap] 추출 완료: {n_records}개 레코드 → {out_json}")


def savemap_write_field(data: bytearray, base: int, off: int, size: int,
                        text: str, replace_table: dict | None,
                        context: str) -> None:
    # [ERR:hex] 마커 → 원본 바이트 그대로 복원 (건드리지 않음)
    if text.startswith("[ERR:") and text.endswith("]"):
        raw_hex = text[5:-1]
        try:
            raw_bytes = bytes.fromhex(raw_hex)
            start = base + off
            data[start: start + size] = raw_bytes + b"\x00" * (size - len(raw_bytes))
            return
        except ValueError:
            pass  # hex 파싱 실패 시 그냥 인코딩 시도

    if replace_table:
        converted = apply_replace_table(text, replace_table)
    else:
        converted = text

    new_bytes = encode_euc_jp(converted, context=context)

    if len(new_bytes) > size:
        print(f"[savemap][경고] 공간 초과, 잘라냄 ({context}): "
              f"{len(new_bytes)}B > {size}B  {text!r}")
        while len(new_bytes) > size:
            new_bytes = new_bytes[:-2]

    start = base + off
    data[start: start + size] = new_bytes + b"\x00" * (size - len(new_bytes))


def savemap_import(bin_path: str, in_json: str, out_bin: str,
                   replace_table: dict | None = None) -> None:
    data = bytearray(Path(bin_path).read_bytes())

    with open(in_json, encoding="utf-8") as f:
        entries = json.load(f)

    for e in entries:
        i    = e["idx"]
        base = i * SAVEMAP_RECORD_SIZE

        f1 = e.get("field1_translation", e.get("field1_original", ""))
        f2 = e.get("field2_translation", e.get("field2_original", ""))

        savemap_write_field(data, base, SAVEMAP_FIELD1_OFF, SAVEMAP_FIELD1_SIZE,
                            f1, replace_table, context=f"idx={i} field1")
        savemap_write_field(data, base, SAVEMAP_FIELD2_OFF, SAVEMAP_FIELD2_SIZE,
                            f2, replace_table, context=f"idx={i} field2")

    Path(out_bin).write_bytes(data)
    print(f"[savemap] 임포트 완료 → {out_bin}")


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Xenosaga Episode I – mapex.bin / savemap.bin 추출/임포트 도구",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  # 추출
  python xeno1_maptext.py extract mapex   mapex.bin   mapex_text.json
  python xeno1_maptext.py extract savemap savemap.bin savemap_text.json

  # 임포트 (치환표 적용)
  python xeno1_maptext.py import mapex   mapex.bin   mapex_text.json   mapex_new.bin   --table XENOSAGA_KOR-JPN.json
  python xeno1_maptext.py import savemap savemap.bin savemap_text.json savemap_new.bin --table XENOSAGA_KOR-JPN.json
""",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # extract
    p_ext = sub.add_parser("extract", help="텍스트 추출")
    p_ext.add_argument("filetype", choices=["mapex", "savemap"])
    p_ext.add_argument("bin",  help="입력 .bin 파일")
    p_ext.add_argument("json", help="출력 JSON 파일")

    # import
    p_imp = sub.add_parser("import", help="텍스트 임포트")
    p_imp.add_argument("filetype", choices=["mapex", "savemap"])
    p_imp.add_argument("bin",     help="원본 .bin 파일 (읽기 전용)")
    p_imp.add_argument("json",    help="번역된 JSON 파일")
    p_imp.add_argument("out_bin", help="출력 .bin 파일")
    p_imp.add_argument("--table", metavar="JSON",
                       help="한글→한자 치환표 JSON (XENOSAGA_KOR-JPN.json)")

    args = parser.parse_args()

    replace_table = None
    if hasattr(args, "table") and args.table:
        replace_table = load_replace_table(args.table)
        print(f"[치환표] {len(replace_table)}개 항목 로드 완료")

    if args.cmd == "extract":
        if args.filetype == "mapex":
            mapex_extract(args.bin, args.json)
        else:
            savemap_extract(args.bin, args.json)

    elif args.cmd == "import":
        if args.filetype == "mapex":
            mapex_import(args.bin, args.json, args.out_bin, replace_table)
        else:
            savemap_import(args.bin, args.json, args.out_bin, replace_table)


if __name__ == "__main__":
    main()
