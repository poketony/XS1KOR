#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image


TOOL_DIR = Path(__file__).resolve().parent
TOOL_VERSION = 4
NAMED_CONTAINER_MAGICS = (b"NLNK", b"NBXX", b"NBGL", b"PTCL")
GS_LAYOUT_FACTS = {
    "PSMT8_PAGE": [128, 64],
    "PSMT4_PAGE": [128, 128],
    "PSMT8_CLUT_ENTRIES": 256,
    "PSMT4_CLUT_ENTRIES": 16,
}


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def find_repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        if (parent / "xtx 개발소").is_dir():
            return parent
    raise RuntimeError("XS1KOR root was not found from this tool location")


def find_xtx_dev_dir() -> Path:
    matches = [p for p in ROOT.iterdir() if p.is_dir() and p.name.startswith("xtx")]
    if not matches:
        raise RuntimeError("xtx development folder was not found")
    return matches[0]


ROOT = find_repo_root()
XTX_DIR = find_xtx_dev_dir()
arx_tool = load_module("xs1kor_arx_tool", XTX_DIR / "arx_tool.py")
xtx_tool = load_module("xs1kor_xtx_tool", XTX_DIR / "xtx_tool_ver7_fixed_palette_formula.py")
psmt4_tool = load_module(
    "xs1kor_psmt4_codec",
    TOOL_DIR / "help_psmt4_codec.py",
)


def u32(data: bytes, off: int) -> int:
    return int.from_bytes(data[off : off + 4], "little")


def zstr(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    return sha256(path.read_bytes())


def safe_name(text: str) -> str:
    chars = []
    for ch in text:
        chars.append(ch if ch.isalnum() or ch in "._-" else "_")
    return "".join(chars).strip("._") or "unnamed"


def rel(path: Path, base: Path) -> str:
    return str(path.relative_to(base)).replace("\\", "/")


def read_index_png(path: Path, expected_size: tuple[int, int], max_value: int) -> np.ndarray:
    image = Image.open(path)
    if image.mode == "P":
        arr = np.array(image, dtype=np.uint8)
    elif image.mode in ("L", "I;16", "I"):
        arr = np.array(image.convert("L"), dtype=np.uint8)
    else:
        raise ValueError(
            f"{path} must be an indexed/grayscale PNG. "
            "RGBA/RGB images are rejected to avoid color quantization mistakes."
        )
    got = (int(arr.shape[1]), int(arr.shape[0]))
    if got != expected_size:
        raise ValueError(f"{path} size must be {expected_size}, got {got}")
    if int(arr.max(initial=0)) > max_value:
        raise ValueError(f"{path} contains values above {max_value}")
    return arr.astype(np.uint8)


def make_grayscale_palette(levels: int) -> list[int]:
    palette: list[int] = []
    for i in range(256):
        if levels == 16:
            v = 0 if i == 0 else 96 + round((i & 0x0F) * (159 / 15))
        else:
            v = i
        palette.extend([v, v, v])
    return palette


def write_index_png(arr: np.ndarray, path: Path, levels: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.fromarray(arr.astype(np.uint8), "P")
    image.putpalette(make_grayscale_palette(levels))
    image.save(path)


@dataclass
class NamedEntry:
    index: int
    name: str
    offset: int
    end: int
    size: int
    magic: str


def parse_named_container(data: bytes, allowed: tuple[bytes, ...] = NAMED_CONTAINER_MAGICS) -> tuple[dict, list[NamedEntry]]:
    magic = data[:4]
    if magic not in allowed:
        raise ValueError(f"unsupported named container magic: {magic!r}")
    name_size = u32(data, 4)
    offset_table = u32(data, 8)
    count = u32(data, 20)
    if not (0 < name_size <= 0x100):
        raise ValueError(f"bad name size: {name_size}")
    if count > 0x10000:
        raise ValueError(f"bad entry count: {count}")
    if 0x20 + count * name_size > len(data):
        raise ValueError("name table exceeds container")
    if offset_table + count * 4 > len(data):
        raise ValueError("offset table exceeds container")

    offsets = [u32(data, offset_table + i * 4) for i in range(count)]
    ends = offsets[1:] + [len(data)]
    entries: list[NamedEntry] = []
    for i, (off, end) in enumerate(zip(offsets, ends)):
        if off > end or end > len(data):
            raise ValueError(f"bad entry bounds at {i}: {off:#x}..{end:#x}")
        name = zstr(data[0x20 + i * name_size : 0x20 + (i + 1) * name_size])
        chunk = data[off:end]
        magic_text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk[:4])
        entries.append(NamedEntry(i, name, off, end, end - off, magic_text))

    meta = {
        "magic": magic.decode("ascii", errors="replace").rstrip("\x00"),
        "name_size": name_size,
        "offset_table": offset_table,
        "count": count,
    }
    return meta, entries


def diff_spans(a: bytes, b: bytes) -> list[dict]:
    spans: list[dict] = []
    start = None
    limit = min(len(a), len(b))
    for i in range(limit):
        if a[i] != b[i] and start is None:
            start = i
        elif a[i] == b[i] and start is not None:
            spans.append({"start": start, "end": i, "size": i - start})
            start = None
    if start is not None:
        spans.append({"start": start, "end": limit, "size": limit - start})
    if len(a) != len(b):
        spans.append({"start": limit, "end": max(len(a), len(b)), "size": abs(len(a) - len(b))})
    return spans


def set_psmt4_pixel(gsmem: np.ndarray, x: int, y: int, value: int, dbp: int, dbw4: int) -> None:
    pos, cb = psmt4_tool.psmt4_pos(x, y, dbp, dbw4)
    if not (0 <= pos < len(gsmem)):
        return
    word = int(gsmem[pos])
    shift = (cb >> 1) * 8
    byte = (word >> shift) & 0xFF
    if cb & 1:
        byte = (byte & 0x0F) | ((int(value) & 0x0F) << 4)
    else:
        byte = (byte & 0xF0) | (int(value) & 0x0F)
    mask = 0xFF << shift
    gsmem[pos] = (word & ~mask) | (byte << shift)


def gsmem_to_xtx_payloads(gsmem: np.ndarray, images: list[dict], dbw32: int) -> dict[int, bytes]:
    out: dict[int, bytes] = {}
    for img in images:
        if not img.get("valid"):
            continue
        w, h, x0, y0 = int(img["width"]), int(img["height"]), int(img["x0"]), int(img["y0"])
        words = np.zeros((h, w), dtype="<u4")
        for y in range(h):
            for x in range(w):
                pos = psmt4_tool.ct32_pos(x0 + x, y0 + y, dbw32)
                words[y, x] = int(gsmem[pos]) if 0 <= pos < len(gsmem) else 0
        out[int(img["index"])] = words.tobytes()
    return out


def psmt4_config(top_entry_name: str, psmt8_size: tuple[int, int]) -> dict:
    stem = Path(str(top_entry_name)).stem.lower()
    width = int(psmt8_size[0])
    height = max(1, int(psmt8_size[1]))
    profile = "full_atlas_psmt4"
    source = "default"
    if stem == "help_01_01":
        dbw4 = 1024
        profile = "help_01_01_psmt4_full_width"
    elif stem == "help_03_01":
        dbw4 = 1024
        profile = "help_03_01_full_atlas_tbw16"
        source = "forced full-atlas edit view; GS dump observed TBP0=0x3800, TBW=16, PSM=PSMT4"
    else:
        dbw4 = 64
    return {
        "profile": profile,
        "width": width,
        "height": height,
        "dbp": 0,
        "dbw4": dbw4,
        "dbw32": max(1, width // 2),
        "source": source,
        "gs_layout_facts": GS_LAYOUT_FACTS,
        "notes": [
            "PSMT4 is a logical GS interpretation of the same XTX upload bytes.",
            "PCSX2 GS layout basis: PSMT4 page=128x128, PSMT8 page=128x64, CLUT entries are 16/256.",
            "Edit exact index values 0..15 only.",
            "Known profiles are applied by the tool on extract and rebuild.",
            "If this geometry is wrong for a specific texture, set manual_config=true in this config before rebuilding.",
        ],
    }


def effective_psmt4_config(meta: dict, item: dict) -> dict:
    cfg = dict(item.get("config") or {})
    psmt8_item = meta.get("edit_files", {}).get("PSMT8", {})
    psmt8_size = tuple(int(x) for x in psmt8_item.get("size", item["size"]))
    known = psmt4_config(str(meta.get("top_entry_name", "")), psmt8_size)
    if not cfg.get("manual_config"):
        keys = ("width", "height", "dbp", "dbw4", "dbw32")
        if any(int(cfg.get(key, -1)) != int(known[key]) for key in keys):
            raise ValueError(
                f"stale PSMT4 geometry for {meta.get('top_entry_name')}: "
                f"extract again with tool version {TOOL_VERSION}, or set manual_config=true in xtx_meta.json"
            )
        return known
    return cfg


def decode_psmt4_png(xtx_data: bytes, images: list[dict], cfg: dict) -> np.ndarray:
    gsmem = psmt4_tool.build_gsmem_words(xtx_data, images, int(cfg["dbw32"]))
    return psmt4_tool.decode_psmt4(
        gsmem,
        int(cfg["width"]),
        int(cfg["height"]),
        int(cfg["dbp"]),
        int(cfg["dbw4"]),
    ).astype(np.uint8)


def rebuild_xtx_from_psmt4(original_xtx: bytes, images: list[dict], cfg: dict, index4: np.ndarray) -> bytes:
    gsmem = psmt4_tool.build_gsmem_words(original_xtx, images, int(cfg["dbw32"]))
    for y in range(index4.shape[0]):
        for x in range(index4.shape[1]):
            set_psmt4_pixel(gsmem, x, y, int(index4[y, x]), int(cfg["dbp"]), int(cfg["dbw4"]))
    payloads = gsmem_to_xtx_payloads(gsmem, images, int(cfg["dbw32"]))
    modified = bytearray(original_xtx)
    for img in images:
        idx = int(img["index"])
        if idx not in payloads:
            continue
        pstart, pend = int(img["pstart"]), int(img["pend"])
        pdata = payloads[idx]
        if len(pdata) != pend - pstart:
            raise ValueError("PSMT4 payload size mismatch")
        modified[pstart:pend] = pdata
    return bytes(modified)


def rebuild_xtx_from_psmt8(original_xtx: bytes, images: list[dict], index8: np.ndarray) -> bytes:
    modified = bytearray(original_xtx)
    for img in images:
        if not img.get("valid"):
            continue
        pstart, pend = int(img["pstart"]), int(img["pend"])
        pdata = xtx_tool.index_atlas_to_xtx_pdata(index8, img)
        if len(pdata) != pend - pstart:
            raise ValueError("PSMT8 payload size mismatch")
        modified[pstart:pend] = pdata
    return bytes(modified)


def extract_xtx(xtx_data: bytes, out_dir: Path, meta_base: dict, manifest_xtx: list[dict], base_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    original_xtx_path = out_dir / "original.xtx"
    original_xtx_path.write_bytes(xtx_data)

    images = xtx_tool.parse_xtx_headers(xtx_data)
    xtx_tool.configure_texture_dimensions(images)

    psmt8 = xtx_tool.build_index_atlas(xtx_data, images).astype(np.uint8)
    psmt8_path = out_dir / "PSMT8.png"
    write_index_png(psmt8, psmt8_path, 256)

    p4_cfg = psmt4_config(str(meta_base.get("top_entry_name", "")), (psmt8.shape[1], psmt8.shape[0]))
    psmt4 = decode_psmt4_png(xtx_data, images, p4_cfg)
    psmt4_path = out_dir / "PSMT4.png"
    write_index_png(psmt4, psmt4_path, 16)

    xtx_meta = {
        **meta_base,
        "tool_version": TOOL_VERSION,
        "xtx_size": len(xtx_data),
        "xtx_sha256": sha256(xtx_data),
        "image_count": len(images),
        "images": [dict(img) for img in images],
        "gs_layout_facts": GS_LAYOUT_FACTS,
        "metadata_runtime_evidence": [
            "OV12.OVL strings reference rg_help.euc.c, help.npr, help_*.bxx, RgBxxGetXtx, pXtxData, and pLexData.",
            "This supports the container -> XTX/LEX -> GS indexed texture pipeline.",
        ],
        "edit_files": {
            "PSMT4": {
                "path": "PSMT4.png",
                "sha256": file_sha256(psmt4_path),
                "size": [int(psmt4.shape[1]), int(psmt4.shape[0])],
                "max_index": 15,
                "config": p4_cfg,
            },
            "PSMT8": {
                "path": "PSMT8.png",
                "sha256": file_sha256(psmt8_path),
                "size": [int(psmt8.shape[1]), int(psmt8.shape[0])],
                "max_index": 255,
            },
        },
        "rebuild_rule": [
            "Only PSMT4.png and PSMT8.png are edit inputs.",
            "If neither PNG changed, this XTX is copied byte-for-byte.",
            "If exactly one PNG changed, that interpretation is rebuilt.",
            "If both PNGs changed, rebuild stops to avoid ambiguous overwrite.",
            "PNG pixels are index values. RGB/RGBA color quantization is not used.",
        ],
    }
    (out_dir / "xtx_meta.json").write_text(json.dumps(xtx_meta, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest_xtx.append({**meta_base, "folder": rel(out_dir, base_dir)})


def extract_payload_xtx(payload: bytes, out_dir: Path, manifest_entries: list[dict], manifest_xtx: list[dict]) -> None:
    top_meta, top_entries = parse_named_container(payload)
    entries_dir = out_dir / "entries"
    entries_dir.mkdir(exist_ok=True)

    for entry in top_entries:
        chunk = payload[entry.offset : entry.end]
        entry_dir = entries_dir / f"{entry.index:02d}_{safe_name(Path(entry.name).stem or entry.name)}"
        entry_dir.mkdir(parents=True, exist_ok=True)
        raw_path = entry_dir / "original.bin"
        raw_path.write_bytes(chunk)

        entry_meta = asdict(entry)
        entry_meta["original_bin"] = rel(raw_path, out_dir)
        if chunk[:4] in NAMED_CONTAINER_MAGICS:
            nested_meta, nested_entries = parse_named_container(chunk)
            entry_meta["nested_container"] = nested_meta
            entry_meta["nested_entries"] = [asdict(x) for x in nested_entries]
            for nested in nested_entries:
                nested_chunk = chunk[nested.offset : nested.end]
                if nested_chunk[:4] != b"XTX\x00":
                    continue
                xtx_dir = entry_dir / f"xtx_{nested.index:02d}_{safe_name(Path(nested.name).stem or nested.name)}"
                extract_xtx(
                    nested_chunk,
                    xtx_dir,
                    {
                        "top_entry_index": entry.index,
                        "top_entry_name": entry.name,
                        "top_entry_offset": entry.offset,
                        "nested_entry_index": nested.index,
                        "nested_entry_name": nested.name,
                        "nested_entry_offset": nested.offset,
                        "payload_xtx_offset": entry.offset + nested.offset,
                    },
                    manifest_xtx,
                    out_dir,
                )
        elif chunk[:4] == b"XTX\x00":
            xtx_dir = entry_dir / f"xtx_{entry.index:02d}_{safe_name(Path(entry.name).stem or entry.name)}"
            extract_xtx(
                chunk,
                xtx_dir,
                {
                    "top_entry_index": entry.index,
                    "top_entry_name": entry.name,
                    "top_entry_offset": entry.offset,
                    "nested_entry_index": None,
                    "nested_entry_name": None,
                    "nested_entry_offset": 0,
                    "payload_xtx_offset": entry.offset,
                },
                manifest_xtx,
                out_dir,
            )
        manifest_entries.append(entry_meta)
    return top_meta


def default_extract_dir(source: Path, suffix: str) -> Path:
    base = source.with_name(f"{source.stem}_{suffix}")
    if not base.exists():
        return base
    for i in range(2, 1000):
        candidate = source.with_name(f"{source.stem}_{suffix}_{i}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"too many existing extract folders for {source.name}")


def extract_npr(npr_path: Path, out_dir: Path | None = None) -> Path:
    if out_dir is None:
        out_dir = default_extract_dir(npr_path, "npr_extract")
    out_dir.mkdir(parents=True, exist_ok=True)

    npr_data = npr_path.read_bytes()
    if npr_data[:4] != b"ARX\x00":
        raise ValueError(f"not ARX/NPR: {npr_path}")
    payload = arx_tool.decompress_arx(npr_data)
    if payload[:4] not in NAMED_CONTAINER_MAGICS:
        raise ValueError(f"decompressed payload is not a known named container: {payload[:4]!r}")

    (out_dir / "original.npr").write_bytes(npr_data)
    (out_dir / "original_payload.bin").write_bytes(payload)

    manifest_entries: list[dict] = []
    manifest_xtx: list[dict] = []
    top_meta = extract_payload_xtx(payload, out_dir, manifest_entries, manifest_xtx)

    manifest = {
        "tool": "npr_roundtrip_tool",
        "version": TOOL_VERSION,
        "source_npr_name": npr_path.name,
        "source_npr_path": str(npr_path),
        "source_npr_size": len(npr_data),
        "source_npr_sha256": sha256(npr_data),
        "payload_size": len(payload),
        "payload_sha256": sha256(payload),
        "payload_container": top_meta,
        "entries": manifest_entries,
        "xtx_items": manifest_xtx,
        "rebuild_rules": [
            "original_payload.bin is the structural base.",
            "Only XTX folders with modified PSMT4.png or PSMT8.png are rewritten.",
            "No offsets, sizes, names, xtxinfo blocks, or entry tables are regenerated.",
            "No-edit rebuild copies original.npr byte-for-byte.",
        ],
    }
    (out_dir / "npr_meta.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Extracted: {npr_path}")
    print(f"Output folder: {out_dir}")
    print(f"XTX folders: {len(manifest_xtx)}")
    return out_dir


def extract_bxx(bxx_path: Path, out_dir: Path | None = None) -> Path:
    if out_dir is None:
        out_dir = default_extract_dir(bxx_path, "bxx_extract")
    out_dir.mkdir(parents=True, exist_ok=True)

    bxx_data = bxx_path.read_bytes()
    if bxx_data[:4] != b"NBXX":
        raise ValueError(f"not NBXX/BXX: {bxx_path}")
    (out_dir / "original.bxx").write_bytes(bxx_data)

    manifest_entries: list[dict] = []
    manifest_xtx: list[dict] = []
    top_meta = extract_payload_xtx(bxx_data, out_dir, manifest_entries, manifest_xtx)

    manifest = {
        "tool": "bxx_roundtrip_tool",
        "version": TOOL_VERSION,
        "source_bxx_name": bxx_path.name,
        "source_bxx_path": str(bxx_path),
        "source_bxx_size": len(bxx_data),
        "source_bxx_sha256": sha256(bxx_data),
        "payload_container": top_meta,
        "entries": manifest_entries,
        "xtx_items": manifest_xtx,
        "rebuild_rules": [
            "original.bxx is the structural base.",
            "Only XTX folders with modified PSMT4.png or PSMT8.png are rewritten.",
            "No offsets, sizes, names, xtxinfo blocks, or entry tables are regenerated.",
            "No-edit rebuild copies original.bxx byte-for-byte.",
        ],
    }
    (out_dir / "bxx_meta.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Extracted: {bxx_path}")
    print(f"Output folder: {out_dir}")
    print(f"XTX folders: {len(manifest_xtx)}")
    return out_dir


def choose_xtx_edit_mode(xtx_folder: Path, meta: dict) -> str | None:
    changed = []
    for mode in ("PSMT4", "PSMT8"):
        item = meta["edit_files"][mode]
        path = xtx_folder / item["path"]
        if not path.exists():
            raise ValueError(f"missing edit file: {path}")
        if file_sha256(path) != item["sha256"]:
            changed.append(mode)
    if not changed:
        return None
    if len(changed) > 1:
        raise ValueError(f"both PSMT4.png and PSMT8.png changed in {xtx_folder}; edit only one")
    return changed[0]


def rebuild_xtx_from_folder(xtx_folder: Path, payload: bytearray, report_items: list[dict]) -> None:
    meta_path = xtx_folder / "xtx_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    xtx_offset = int(meta["payload_xtx_offset"])
    xtx_size = int(meta["xtx_size"])
    original_xtx = (xtx_folder / "original.xtx").read_bytes()
    if len(original_xtx) != xtx_size:
        raise ValueError(f"original.xtx size mismatch in {xtx_folder}")
    if sha256(original_xtx) != meta["xtx_sha256"]:
        raise ValueError(f"original.xtx hash mismatch in {xtx_folder}")
    if bytes(payload[xtx_offset : xtx_offset + xtx_size]) != original_xtx:
        raise ValueError(f"base payload no longer matches original XTX at {xtx_folder}")

    mode = choose_xtx_edit_mode(xtx_folder, meta)
    if mode is None:
        report_items.append(
            {
                "folder": str(xtx_folder),
                "mode": "unchanged",
                "changed_bytes": 0,
                "changed_range_count": 0,
                "old_sha256": meta["xtx_sha256"],
                "new_sha256": meta["xtx_sha256"],
            }
        )
        return

    images = xtx_tool.parse_xtx_headers(original_xtx)
    xtx_tool.configure_texture_dimensions(images)
    if mode == "PSMT4":
        item = meta["edit_files"]["PSMT4"]
        size = tuple(int(x) for x in item["size"])
        index4 = read_index_png(xtx_folder / item["path"], size, 15)
        modified_xtx = rebuild_xtx_from_psmt4(original_xtx, images, effective_psmt4_config(meta, item), index4)
    else:
        item = meta["edit_files"]["PSMT8"]
        size = tuple(int(x) for x in item["size"])
        index8 = read_index_png(xtx_folder / item["path"], size, 255)
        modified_xtx = rebuild_xtx_from_psmt8(original_xtx, images, index8)

    if len(modified_xtx) != xtx_size:
        raise ValueError(f"rebuilt XTX size changed in {xtx_folder}")
    payload[xtx_offset : xtx_offset + xtx_size] = modified_xtx

    spans = diff_spans(original_xtx, modified_xtx)
    report_items.append(
        {
            "folder": str(xtx_folder),
            "mode": mode,
            "payload_xtx_offset": xtx_offset,
            "xtx_size": xtx_size,
            "changed_bytes": sum(x["size"] for x in spans),
            "changed_range_count": len(spans),
            "old_sha256": sha256(original_xtx),
            "new_sha256": sha256(modified_xtx),
        }
    )


def rebuild_npr_folder(folder: Path, out_path: Path | None = None) -> Path:
    meta_path = folder / "npr_meta.json"
    if not meta_path.exists():
        raise ValueError(f"npr_meta.json not found: {folder}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    payload_path = folder / "original_payload.bin"
    if not payload_path.exists():
        raise ValueError(f"original_payload.bin not found: {folder}")

    original_payload = payload_path.read_bytes()
    if sha256(original_payload) != meta["payload_sha256"]:
        raise ValueError("original_payload.bin hash does not match npr_meta.json")
    payload = bytearray(original_payload)

    report_items: list[dict] = []
    for xtx_meta in sorted(folder.rglob("xtx_meta.json")):
        rebuild_xtx_from_folder(xtx_meta.parent, payload, report_items)

    rebuilt_payload = bytes(payload)
    payload_spans = diff_spans(original_payload, rebuilt_payload)
    if out_path is None:
        out_path = folder / f"{Path(meta.get('source_npr_name') or 'rebuilt.npr').stem}_rebuilt.npr"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not payload_spans:
        shutil.copyfile(folder / "original.npr", out_path)
        arx_roundtrip = True
    else:
        rebuilt_npr = arx_tool.compress_arx(rebuilt_payload)
        arx_roundtrip = arx_tool.decompress_arx(rebuilt_npr) == rebuilt_payload
        if not arx_roundtrip:
            raise ValueError("ARX roundtrip verification failed")
        out_path.write_bytes(rebuilt_npr)

    report = {
        "tool": "npr_roundtrip_tool",
        "version": TOOL_VERSION,
        "source_extract_folder": str(folder),
        "output_npr": str(out_path),
        "output_npr_size": out_path.stat().st_size,
        "original_payload_size": len(original_payload),
        "rebuilt_payload_size": len(rebuilt_payload),
        "payload_size_equal": len(original_payload) == len(rebuilt_payload),
        "payload_changed_bytes": sum(x["size"] for x in payload_spans),
        "payload_changed_range_count": len(payload_spans),
        "arx_roundtrip_payload_equal": arx_roundtrip,
        "xtx_reports": report_items,
    }
    (folder / "rebuild_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Rebuilt: {out_path}")
    print(f"Changed XTX folders: {sum(1 for x in report_items if x['changed_bytes'])}/{len(report_items)}")
    print(f"Report: {folder / 'rebuild_report.json'}")
    return out_path


def rebuild_bxx_folder(folder: Path, out_path: Path | None = None) -> Path:
    meta_path = folder / "bxx_meta.json"
    if not meta_path.exists():
        raise ValueError(f"bxx_meta.json not found: {folder}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    original_path = folder / "original.bxx"
    if not original_path.exists():
        raise ValueError(f"original.bxx not found: {folder}")

    original_bxx = original_path.read_bytes()
    if sha256(original_bxx) != meta["source_bxx_sha256"]:
        raise ValueError("original.bxx hash does not match bxx_meta.json")
    payload = bytearray(original_bxx)

    report_items: list[dict] = []
    for xtx_meta in sorted(folder.rglob("xtx_meta.json")):
        rebuild_xtx_from_folder(xtx_meta.parent, payload, report_items)

    rebuilt_bxx = bytes(payload)
    spans = diff_spans(original_bxx, rebuilt_bxx)
    if out_path is None:
        out_path = folder / f"{Path(meta.get('source_bxx_name') or 'rebuilt.bxx').stem}_rebuilt.bxx"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not spans:
        shutil.copyfile(original_path, out_path)
    else:
        out_path.write_bytes(rebuilt_bxx)

    report = {
        "tool": "bxx_roundtrip_tool",
        "version": TOOL_VERSION,
        "source_extract_folder": str(folder),
        "output_bxx": str(out_path),
        "output_bxx_size": out_path.stat().st_size,
        "original_bxx_size": len(original_bxx),
        "rebuilt_bxx_size": len(rebuilt_bxx),
        "bxx_size_equal": len(original_bxx) == len(rebuilt_bxx),
        "bxx_changed_bytes": sum(x["size"] for x in spans),
        "bxx_changed_range_count": len(spans),
        "xtx_reports": report_items,
    }
    (folder / "rebuild_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Rebuilt: {out_path}")
    print(f"Changed XTX folders: {sum(1 for x in report_items if x['changed_bytes'])}/{len(report_items)}")
    print(f"Report: {folder / 'rebuild_report.json'}")
    return out_path


def dispatch(path: Path, out: Path | None = None) -> Path:
    if path.is_file() and path.suffix.lower() == ".npr":
        return extract_npr(path, out)
    if path.is_dir() and (path / "npr_meta.json").exists():
        return rebuild_npr_folder(path, out)
    raise ValueError("Drop either a .npr file or an extracted folder containing npr_meta.json")


def main() -> None:
    parser = argparse.ArgumentParser(description="NPR extract/rebuild tool for XTX PSMT4/PSMT8 index PNGs")
    parser.add_argument("path", type=Path, help=".npr file to extract, or extracted folder to rebuild")
    parser.add_argument("--out", type=Path, help="output folder for extract or output .npr for rebuild")
    args = parser.parse_args()
    dispatch(args.path, args.out)


if __name__ == "__main__":
    main()
