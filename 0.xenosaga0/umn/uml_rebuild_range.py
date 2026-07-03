#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent

UML_IDS = [
    "0000", "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010", "0011",
    "0012", "0013", "0014", "0015", "0016", "0017", "0018", "0019", "0020", "0021", "0022", "0023",
    "0024", "0025", "0026", "0027", "0028", "0029", "0030", "0031", "0032", "0033", "0034", "0035",
    "0036", "0037", "0038", "0039", "0040", "0041", "0042", "0043", "0044", "0045", "0046", "0047",
    "0048", "0049", "0050", "0051", "0052", "0053", "0054", "0055", "0056", "0057", "0058", "0059",
    "0060", "0061", "0062", "0063", "0064", "0065", "0066", "0067", "0068", "0069", "0070", "0071",
    "0072", "0073", "0074", "0075", "0076", "0077", "0078", "0079", "0080", "9000", "9001", "9002",
    "9003", "9004", "9005", "9006", "9007", "9008", "9009", "9010", "9011", "9012", "9013", "9014",
    "9015", "9016", "9017", "9018", "9019", "9020", "9021", "9022", "9023", "9024", "9025", "9026",
    "9027", "9028", "9029", "9030", "9031", "9032", "9033", "9034", "9035", "9036", "9037", "9038",
    "9039", "9040", "9041", "9042", "9043", "9044", "9045", "9046", "9047", "9048", "9049", "9050",
    "9051", "9052", "9053", "9054", "9055", "9056", "9057", "9058", "9059", "9060", "9061", "9062",
    "9063", "9064", "9065", "9066", "9067", "9068", "9069", "9070", "9071", "9072", "9073", "9074",
    "9075", "9076", "9077", "9078", "9079", "9080", "9081", "9082", "9083", "9084", "9085", "9086",
    "9087", "9088", "9089", "9090", "9091", "9092", "9093", "9094", "9095", "9096", "9097", "9098",
    "9099", "9100", "9101", "9102", "9103", "9104", "9105", "9106", "9107", "9108", "9109", "9110",
    "9111", "9112", "9113", "9114", "9115", "9116", "9117", "9118", "9119", "9120", "9121", "9122",
    "9123", "9124", "9125", "9126", "9127", "9128", "9129", "9130", "9131", "9132", "9133", "9134",
    "9135", "9136", "9137", "9138", "9139", "9140", "9141", "9142", "9143", "9144", "9145", "9146",
    "9147", "9148", "9149", "9150", "9151", "9152", "9153", "9154", "9155", "9156", "9157", "9158",
    "9159", "9160", "9161", "9162", "9163", "9164", "9165", "9166", "9167", "9168", "9169", "9170",
    "9171", "9172", "9173", "9174", "9175", "9176", "9177", "9178", "9179", "9180", "9181", "9182",
    "9183", "9184", "9185", "9186", "9187", "9188", "9189", "9190", "9191", "9192", "9193", "9194",
    "9195", "9196", "9197", "9198", "9199", "9200", "9201", "9202", "9203", "9204", "9205", "9206",
    "9207", "9208", "9209", "9210", "9211", "9212", "9213", "9214", "9215", "9216", "9217", "9218",
    "9219", "9220", "9221", "9222", "9223", "9224", "9225", "9226", "9227", "9228", "9229", "9230",
    "9231", "9232", "9233", "9234", "9235", "9236", "9237", "9238", "9239", "9240", "9241", "9242",
    "9243", "9244",
]


def normalize_id(value: str) -> str:
    value = value.strip()
    if value.lower() == "all":
        return "all"
    if not value.isdigit():
        raise ValueError(f"bad UML id: {value!r}")
    if len(value) > 4:
        raise ValueError(f"UML id must be 4 digits or fewer: {value!r}")
    return value.zfill(4)


def select_ids(start: str, end: str | None) -> list[str]:
    start_id = normalize_id(start)
    if start_id == "all":
        return [id_ for id_ in UML_IDS if (SCRIPT_DIR / f"{id_}.uml").is_file()]

    end_id = normalize_id(end or start)
    if start_id not in UML_IDS:
        raise ValueError(f"start id is not in UML_IDS: {start_id}")
    if end_id not in UML_IDS:
        raise ValueError(f"end id is not in UML_IDS: {end_id}")

    start_index = UML_IDS.index(start_id)
    end_index = UML_IDS.index(end_id)
    if start_index > end_index:
        raise ValueError(f"start id comes after end id in UML_IDS: {start_id} > {end_id}")
    return UML_IDS[start_index : end_index + 1]


def rebuild_one(id_: str) -> int:
    uml = SCRIPT_DIR / f"{id_}.uml"
    ext = SCRIPT_DIR / f"{id_}_ext"
    out = SCRIPT_DIR / f"{id_}.uml.new"

    if not uml.is_file():
        print(f"[SKIP] {id_}: missing {uml.name}")
        return 0
    if not ext.is_dir():
        print(f"[SKIP] {id_}: missing {ext.name}")
        return 0

    cmd = [sys.executable, "uml_tool.py", "rebuild", uml.name, ext.name, out.name]
    print("[RUN]", " ".join(cmd))
    result = subprocess.run(cmd, cwd=SCRIPT_DIR)
    return result.returncode


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: uml_rebuild_range.py <start-id|all> [end-id]")
        return 2

    try:
        targets = select_ids(argv[1], argv[2] if len(argv) >= 3 else None)
    except ValueError as exc:
        print(f"[ERROR] {exc}")
        return 2

    if not targets:
        print("[ERROR] no target UML files selected")
        return 2

    print(f"[INFO] selected {len(targets)} UML id(s): {targets[0]}..{targets[-1]}")
    failed: list[str] = []
    for id_ in targets:
        code = rebuild_one(id_)
        if code != 0:
            failed.append(id_)

    if failed:
        print(f"[FAIL] {len(failed)} rebuild(s) failed: {', '.join(failed)}")
        return 1
    print("[OK] rebuild complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
