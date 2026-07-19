#!/usr/bin/env python3
"""Build db_fileno.txt in Korean dictionary order from dbheader.tsv."""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path


CATEGORIES = ("가", "나", "다", "라", "마", "바", "사", "아", "자", "차", "카", "타", "파", "하")
CATEGORY_COMMENTS = CATEGORIES
CHOSEONG = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
CHOSEONG_TO_CATEGORY = {
    "ㄱ": "가", "ㄲ": "가", "ㄴ": "나", "ㄷ": "다", "ㄸ": "다",
    "ㄹ": "라", "ㅁ": "마", "ㅂ": "바", "ㅃ": "바", "ㅅ": "사",
    "ㅆ": "사", "ㅇ": "아", "ㅈ": "자", "ㅉ": "자", "ㅊ": "차",
    "ㅋ": "카", "ㅌ": "타", "ㅍ": "파", "ㅎ": "하",
}
CONTROL_RE = re.compile(r"<CTL:[^>]+>")

# Titles beginning with Latin letters or digits are sorted by their Korean
# spoken form. These overrides are intentionally explicit so that a later title
# edit cannot silently move an entry to a different category.
READING_OVERRIDES = {
    9011: "이씨엠",
    9012: "이피알 패러독스",
    9013: "이피알 레이더",
    9019: "브이포 영역",
    9029: "유도",
    9030: "에이그스",
    9031: "에이그스 이편",
    9033: "에스 엠 에스의 식별 시그널",
    9036: "엠 더블유 에스",
    9037: "엠티 영역",
    9039: "엘피에스",
    9074: "코스모스",
    9083: "지형 타깃 드론",
    9091: "딕 베타",
    9093: "지구라트 에이트",
    9096: "십이사도",
    9098: "주니어",
    9099: "주니어 이편",
    9100: "주니어와 알베도의 힘",
    9137: "디엠이 중독",
    9138: "디 트리플 에스",
    9149: "트리플 에이 클래스 공적 프로텍트",
    9155: "이국이 보낸 코스모스 장비 요항",
    9172: "피엠",
    9173: "피티 카트리지",
    9213: "유알티브이",
    9214: "유엔피",
    9215: "우누스 문두스 네트워크",
    9216: "유엠엔 관리 센터",
    9217: "유엠엔 전이 칼럼",
    9218: "유엠엔 펄스",
    9219: "유틱 기관",
    9223: "사백칠십사 특무 함대",
    9224: "사백번대 프로그램",
    9243: "와이 자료",
}


def category_for(reading: str) -> str:
    first = reading.strip()[:1]
    if not first or not ("가" <= first <= "힣"):
        raise ValueError(f"reading must begin with Hangul: {reading!r}")
    choseong = CHOSEONG[(ord(first) - 0xAC00) // 588]
    return CHOSEONG_TO_CATEGORY[choseong]


def load_rows(path: Path) -> list[tuple[int, str, str]]:
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if reader.fieldnames != ["index", "subject"]:
            raise ValueError(f"unexpected TSV columns: {reader.fieldnames!r}")
        for line_number, row in enumerate(reader, 2):
            index = int(row["index"])
            subject = row["subject"]
            visible = CONTROL_RE.sub("", subject).strip()
            reading = READING_OVERRIDES.get(index, visible)
            try:
                category_for(reading)
            except ValueError as exc:
                raise ValueError(f"line {line_number}, index {index}: {exc}") from exc
            rows.append((index, subject, reading))

    ids = [row[0] for row in rows]
    if len(ids) != 245 or len(set(ids)) != len(ids) or set(ids) != set(range(9000, 9245)):
        raise ValueError("dbheader.tsv must contain each index from 9000 through 9244 exactly once")
    return rows


def build_text(rows: list[tuple[int, str, str]]) -> tuple[str, dict[str, list[int]]]:
    grouped: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for index, _subject, reading in rows:
        grouped[category_for(reading)].append((index, reading))

    output = []
    ids_by_category = {}
    def sort_key(item: tuple[int, str]) -> tuple[str, int]:
        index, reading = item
        normalized = unicodedata.normalize("NFKC", reading)
        letters_and_numbers = "".join(character for character in normalized if character.isalnum())
        return letters_and_numbers, index

    for category, comment in zip(CATEGORIES, CATEGORY_COMMENTS):
        entries = sorted(
            grouped[category],
            key=sort_key,
        )
        ids = [index for index, _reading in entries]
        if not ids:
            raise ValueError(f"category {category!r} is empty")
        ids_by_category[category] = ids
        output.append(f"//\t{comment}")
        output.extend(f"{index}," for index in ids)
        output.extend(("-1,", ""))
    return "\r\n".join(output) + "\r\n", ids_by_category


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tsv", nargs="?", type=Path, default=Path(__file__).with_name("dbheader.tsv"))
    parser.add_argument("output", nargs="?", type=Path, default=Path(__file__).with_name("db_fileno.txt"))
    parser.add_argument("--check", action="store_true", help="verify output without writing it")
    args = parser.parse_args()

    rows = load_rows(args.tsv)
    text, grouped = build_text(rows)
    if args.check:
        actual = args.output.read_bytes().decode("utf-8")
        actual_values = [
            int(line.removesuffix(","))
            for line in actual.splitlines()
            if line and not line.startswith("//")
        ]
        expected_values = [
            int(line.removesuffix(","))
            for line in text.splitlines()
            if line and not line.startswith("//")
        ]
        if actual_values != expected_values:
            raise SystemExit(f"[ERROR] {args.output} is not up to date")
    else:
        temporary = args.output.with_name(args.output.name + ".tmp")
        temporary.write_text(text, encoding="utf-8", newline="")
        os.replace(temporary, args.output)

    all_ids = [index for category in CATEGORIES for index in grouped[category]]
    if len(all_ids) != 245 or len(set(all_ids)) != 245:
        raise AssertionError("generated db_fileno index coverage is not exactly 245 unique entries")
    counts = ", ".join(f"{category}={len(grouped[category])}" for category in CATEGORIES)
    print(f"[OK] db_fileno categories: {counts}")
    print(f"[OK] indices: {len(all_ids)} unique")
    print(f"[OK] {'verified' if args.check else 'output'}: {args.output}")


if __name__ == "__main__":
    main()
