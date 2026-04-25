#!/usr/bin/env python3
"""
Xenosaga Episode I – evtitem.dat 추출/임포트 도구

[레코드 구조] 0x80 bytes × 255 records
  +0x00~+0x10 (17B) : Field1 — 아이템명       (null-padded)
  +0x11~+0x7b (107B): Field2 — 아이템 설명    (null-padded)
  +0x7c~+0x7d (2B)  : Tail   — 히라가나 인덱스 (바이너리 보존)
  +0x7e~+0x7f (2B)  : 항상 0x0000            (바이너리 보존)

[Import 변환 파이프라인]
  한글 텍스트 → JSON 치환표(한글→한자) → EUC-JP 인코딩 → 파일 기록
"""

import json
import sys
import argparse
from pathlib import Path

RECORD_SIZE  = 0x80
RECORD_COUNT = 255

FIELD1_OFF  = 0x00
FIELD1_SIZE = 0x11   # 17B

FIELD2_OFF  = 0x11
FIELD2_SIZE = 0x6b   # 107B

TAIL_OFF    = 0x7c   # 2B 히라가나 인덱스 (건드리지 않음)


# ──────────────────────────────────────────────
# 공통 유틸
# ──────────────────────────────────────────────

def load_replace_table(json_path: str) -> dict:
    with open(json_path, encoding="utf-8-sig") as f:
        data = json.load(f)
    table = data.get("replace-table", {})
    if not table:
        raise ValueError("JSON에서 'replace-table' 키를 찾을 수 없습니다.")
    return table


def apply_replace_table(text: str, table: dict) -> str:
    return "".join(table.get(ch, ch) for ch in text)


def encode_euc_jp(text: str, context: str = "") -> bytes:
    try:
        return text.encode("euc-jp")
    except (UnicodeEncodeError, UnicodeDecodeError) as e:
        sys.exit(f"[오류] EUC-JP 인코딩 실패 ({context}): {e!r}\n  텍스트: {text!r}")


def read_field(rec: bytes, off: int, size: int) -> str:
    raw = rec[off:off + size]
    end = raw.find(b"\x00")
    chunk = raw[:end] if end >= 0 else raw
    if not chunk:
        return ""
    try:
        return chunk.decode("euc-jp")
    except Exception:
        return f"[ERR:{chunk.hex()}]"


def write_field(data: bytearray, base: int, off: int, size: int,
                text: str, replace_table: dict | None, context: str) -> None:
    # [ERR:hex] 마커 → 원본 바이트 그대로 보존
    if text.startswith("[ERR:") and text.endswith("]"):
        try:
            raw = bytes.fromhex(text[5:-1])
            data[base + off: base + off + size] = raw + b"\x00" * (size - len(raw))
            return
        except ValueError:
            pass

    converted = apply_replace_table(text, replace_table) if replace_table else text
    new_bytes = encode_euc_jp(converted, context=context)

    if len(new_bytes) > size:
        print(f"[경고] 공간 초과, 잘라냄 ({context}): {len(new_bytes)}B > {size}B  {text!r}")
        while len(new_bytes) > size:
            new_bytes = new_bytes[:-2]

    data[base + off: base + off + size] = new_bytes + b"\x00" * (size - len(new_bytes))


# ──────────────────────────────────────────────
# extract
# ──────────────────────────────────────────────

def do_extract(bin_path: str, out_json: str) -> None:
    data = Path(bin_path).read_bytes()
    if len(data) != RECORD_SIZE * RECORD_COUNT:
        print(f"[경고] 파일 크기 {len(data)}B ≠ 예상 {RECORD_SIZE * RECORD_COUNT}B")

    result = []
    for i in range(RECORD_COUNT):
        base = i * RECORD_SIZE
        rec  = data[base: base + RECORD_SIZE]

        f1 = read_field(rec, FIELD1_OFF, FIELD1_SIZE)
        f2 = read_field(rec, FIELD2_OFF, FIELD2_SIZE)

        result.append({
            "idx":                i,
            "name_original":      f1,
            "name_translation":   f1,
            "desc_original":      f2,
            "desc_translation":   f2,
        })

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    non_empty = sum(1 for e in result if e["name_original"])
    print(f"[evtitem] 추출 완료: {RECORD_COUNT}개 레코드 ({non_empty}개 유효) → {out_json}")


# ──────────────────────────────────────────────
# import
# ──────────────────────────────────────────────

def do_import(bin_path: str, in_json: str, out_bin: str,
              replace_table: dict | None = None) -> None:
    data = bytearray(Path(bin_path).read_bytes())

    with open(in_json, encoding="utf-8") as f:
        entries = json.load(f)

    for e in entries:
        i    = e["idx"]
        base = i * RECORD_SIZE

        f1 = e.get("name_translation", e.get("name_original", ""))
        f2 = e.get("desc_translation", e.get("desc_original", ""))

        write_field(data, base, FIELD1_OFF, FIELD1_SIZE, f1, replace_table,
                    context=f"idx={i} name")
        write_field(data, base, FIELD2_OFF, FIELD2_SIZE, f2, replace_table,
                    context=f"idx={i} desc")
        # tail(+0x7c), padding(+0x7e) 건드리지 않음

    Path(out_bin).write_bytes(data)
    print(f"[evtitem] 임포트 완료 → {out_bin}")


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Xenosaga Episode I – evtitem.dat 추출/임포트 도구",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python xeno1_evtitem.py extract evtitem.dat evtitem.json
  python xeno1_evtitem.py import  evtitem.dat evtitem.json evtitem_new.dat --table XENOSAGA_KOR-JPN.json
""",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ext = sub.add_parser("extract", help="텍스트 추출")
    p_ext.add_argument("bin",  help="입력 evtitem.dat")
    p_ext.add_argument("json", help="출력 JSON 파일")

    p_imp = sub.add_parser("import", help="텍스트 임포트")
    p_imp.add_argument("bin",     help="원본 evtitem.dat")
    p_imp.add_argument("json",    help="번역된 JSON 파일")
    p_imp.add_argument("out_bin", help="출력 파일")
    p_imp.add_argument("--table", metavar="JSON",
                       help="한글→한자 치환표 JSON (XENOSAGA_KOR-JPN.json)")

    args = parser.parse_args()

    replace_table = None
    if hasattr(args, "table") and args.table:
        replace_table = load_replace_table(args.table)
        print(f"[치환표] {len(replace_table)}개 항목 로드 완료")

    if args.cmd == "extract":
        do_extract(args.bin, args.json)
    elif args.cmd == "import":
        do_import(args.bin, args.json, args.out_bin, replace_table)


if __name__ == "__main__":
    main()
