#!/usr/bin/env python3
"""Extract and rebuild Xenosaga 1 casino XTX graphics."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image


BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parents[1]
CORE_TOOL = REPO_ROOT / "xtx 개발소" / "xtx_tool_ver7_fixed_palette_formula.py"
EXTRACT_ROOT = BASE_DIR / "graphics_extract"
REBUILT_ROOT = BASE_DIR / "graphics_rebuilt"
CATALOG_PATH = EXTRACT_ROOT / "catalog.json"
META_NAME = "tanaka_xtx_meta.json"
PALETTE_PROFILE_PATH = BASE_DIR / "ov11_palette_profile.json"
OV11_PATH = REPO_ROOT / "metadata" / "OV11.OVL"
SAM_NAME = "sam.xtx"
SAM_PANEL_WIDTH = 256
SAM_PANEL_HEIGHT = 128


def load_core():
    spec = importlib.util.spec_from_file_location("xs1_xtx_core", CORE_TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load XTX core: {CORE_TOOL}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def relative_source(path: Path) -> str:
    return path.relative_to(BASE_DIR).as_posix()


def is_generated(path: Path) -> bool:
    rel = path.relative_to(BASE_DIR)
    return bool(rel.parts and rel.parts[0].lower() in {"graphics_extract", "graphics_rebuilt"})


def is_xtx(path: Path) -> bool:
    if not path.is_file() or is_generated(path):
        return False
    try:
        return path.read_bytes()[:4] == b"XTX\0"
    except OSError:
        return False


def discover_sources() -> list[Path]:
    return sorted(
        (path for path in BASE_DIR.rglob("*") if is_xtx(path)),
        key=lambda path: relative_source(path).lower(),
    )


def load_catalog() -> dict:
    if not CATALOG_PATH.is_file():
        return {"version": 1, "assets": []}
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def save_catalog(catalog: dict) -> None:
    EXTRACT_ROOT.mkdir(parents=True, exist_ok=True)
    CATALOG_PATH.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")


def extraction_dir(source: Path) -> Path:
    rel = source.relative_to(BASE_DIR)
    standard = EXTRACT_ROOT / rel.parent / source.stem
    candidates = [standard, standard.with_name(standard.name + "_v2")]
    for candidate in candidates:
        if not candidate.is_dir() or not any(candidate.rglob("*_KOR.png")):
            if candidate != standard:
                print(f"  Existing _KOR edit preserved; new extraction: {candidate}")
            return candidate
    raise ValueError(
        "all extraction folders contain _KOR edits; preserve one before re-extracting: "
        + ", ".join(str(path) for path in candidates)
    )


def rebuilt_path(source: Path) -> Path:
    return REBUILT_ROOT / source.relative_to(BASE_DIR)


def update_catalog(source: Path, out_dir: Path, profile: str) -> None:
    item = {
        "source": relative_source(source),
        "source_size": source.stat().st_size,
        "source_sha256": sha256_file(source),
        "extract_dir": out_dir.relative_to(BASE_DIR).as_posix(),
        "rebuilt": rebuilt_path(source).relative_to(BASE_DIR).as_posix(),
        "profile": profile,
    }
    catalog = load_catalog()
    assets = [entry for entry in catalog.get("assets", []) if entry["source"].lower() != item["source"].lower()]
    assets.append(item)
    assets.sort(key=lambda entry: entry["source"].lower())
    catalog["assets"] = assets
    save_catalog(catalog)


def select_source(query: str) -> Path:
    needle = query.replace("\\", "/").lower()
    matches = []
    for source in discover_sources():
        rel = relative_source(source).lower()
        if rel == needle or source.stem.lower() == needle or str(Path(rel).with_suffix("")) == needle:
            matches.append(source)
    if not matches:
        raise ValueError(f"XTX resource not found: {query}")
    if len(matches) > 1:
        raise ValueError(f"ambiguous resource '{query}': " + ", ".join(relative_source(path) for path in matches))
    return matches[0]


def select_catalog_item(query: str) -> dict:
    needle = query.replace("\\", "/").lower()
    matches = []
    for item in load_catalog().get("assets", []):
        source = Path(item["source"])
        if item["source"].lower() == needle or source.stem.lower() == needle or source.with_suffix("").as_posix().lower() == needle:
            matches.append(item)
    if not matches:
        raise ValueError(f"no extraction catalog entry for '{query}'; run 'tanakagfx extract {query}' first")
    if len(matches) > 1:
        raise ValueError(f"ambiguous resource '{query}'")
    return matches[0]


def kor_path(path: Path) -> Path:
    return path.with_name(path.stem + "_KOR" + path.suffix)


def write_index_png(core, indices: np.ndarray, path: Path, levels: int) -> None:
    core.write_index_png(indices.astype(np.uint8), str(path), levels)


def load_palette_profile() -> dict:
    if not PALETTE_PROFILE_PATH.is_file():
        raise FileNotFoundError(f"missing OV11 palette profile: {PALETTE_PROFILE_PATH}")
    profile = json.loads(PALETTE_PROFILE_PATH.read_text(encoding="utf-8"))
    ovl = OV11_PATH.read_bytes()
    if len(ovl) != int(profile["ovl_size"]) or sha256_bytes(ovl) != profile["ovl_sha256"]:
        raise ValueError("metadata/OV11.OVL does not match the palette profile")
    return profile


def ps2_alpha_to_png(value: np.ndarray) -> np.ndarray:
    return np.clip(value.astype(np.uint16) * 255 // 128, 0, 255).astype(np.uint8)


def build_gsmem(core, data: bytes, images: list[dict]) -> np.ndarray:
    width32 = max(int(image["bw_eff"]) * 64 for image in images)
    return core.psmt4_tool.build_gsmem_words(data, images, width32)


def read_csm1_palette(core, gsmem: np.ndarray, cbp: int, levels: int) -> np.ndarray:
    palette = np.zeros((levels, 4), dtype=np.uint8)
    start = cbp * 64
    for index in range(levels):
        x = index & 15
        y = index >> 4
        word = int(gsmem[start + core.psmt4_tool.ct32_pos(x, y, 64)])
        palette[index] = (
            word & 0xFF,
            (word >> 8) & 0xFF,
            (word >> 16) & 0xFF,
            (word >> 24) & 0xFF,
        )
    if levels == 256:
        for group in range(8):
            left = group * 32 + 8
            right = group * 32 + 16
            temporary = palette[left:left + 8].copy()
            palette[left:left + 8] = palette[right:right + 8]
            palette[right:right + 8] = temporary
    palette[:, 3] = ps2_alpha_to_png(palette[:, 3])
    return palette


def display_palette_rgb(palette: np.ndarray) -> np.ndarray:
    rgb = palette[:, :3].copy()
    rgb[palette[:, 3] == 0] = 0
    return rgb


def grayscale_palette(levels: int) -> np.ndarray:
    values = np.rint(np.linspace(0, 255, levels)).astype(np.uint8)
    palette = np.zeros((levels, 4), dtype=np.uint8)
    palette[:, :3] = values[:, None]
    palette[:, 3] = 255
    return palette


def resource_profile(source: Path, palette_profile: dict) -> dict:
    try:
        return palette_profile["resources"][source.name]
    except KeyError as exc:
        raise ValueError(f"OV11 has no palette mapping for {source.name}") from exc


def build_region_palette_map(shape: tuple[int, int], regions: list[dict], psm: str) -> np.ndarray:
    height, width = shape
    result = np.full((height, width), -1, dtype=np.int32)
    for region in sorted(regions, key=lambda item: -(int(item["width"]) * int(item["height"]))):
        if region["psm"] != psm:
            continue
        x0, y0 = max(0, int(region["u"])), max(0, int(region["v"]))
        x1 = min(width, x0 + int(region["width"]))
        y1 = min(height, y0 + int(region["height"]))
        if x0 < x1 and y0 < y1:
            result[y0:y1, x0:x1] = int(region["cbp"])
    return result


def render_region_rgb(
    indices: np.ndarray,
    palette_map: np.ndarray,
    palettes: dict[int, np.ndarray],
) -> np.ndarray:
    rgb = np.zeros((*indices.shape, 3), dtype=np.uint8)
    for cbp, palette in palettes.items():
        mask = palette_map == cbp
        if np.any(mask):
            rgb[mask] = display_palette_rgb(palette)[indices[mask]]
    return rgb


def read_edit_rgb(path: Path, reference_path: Path, expected: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    image = Image.open(path)
    if image.size != expected:
        raise ValueError(f"{path} size must be {expected}, got {image.size}")
    edit_rgb = np.array(image.convert("RGB"), dtype=np.uint8)
    reference_rgb = np.array(Image.open(reference_path).convert("RGB"), dtype=np.uint8)
    changed = np.any(edit_rgb != reference_rgb, axis=2)
    return edit_rgb, changed


def nearest_palette_indices(colors: np.ndarray, palette: np.ndarray) -> tuple[np.ndarray, int]:
    palette_rgb = display_palette_rgb(palette).astype(np.int32)
    result = np.empty(len(colors), dtype=np.uint8)
    max_error = 0
    for start in range(0, len(colors), 4096):
        chunk = colors[start:start + 4096].astype(np.int32)
        distance = np.sum((chunk[:, None, :] - palette_rgb[None, :, :]) ** 2, axis=2)
        selected = np.argmin(distance, axis=1)
        result[start:start + len(chunk)] = selected.astype(np.uint8)
        max_error = max(max_error, int(np.max(distance[np.arange(len(chunk)), selected], initial=0)))
    return result, max_error


def quantize_changed_rgb(
    original: np.ndarray,
    edit_rgb: np.ndarray,
    changed: np.ndarray,
    palette_map: np.ndarray,
    palettes: dict[int, np.ndarray],
    path: Path,
) -> tuple[np.ndarray, dict]:
    unresolved = changed & (palette_map < 0)
    if np.any(unresolved):
        y, x = np.argwhere(unresolved)[0]
        raise ValueError(f"{path} changes ({x},{y}), outside every OV11 texture/CLUT region")
    output = original.copy()
    max_error = 0
    for cbp, palette in palettes.items():
        mask = changed & (palette_map == cbp)
        if not np.any(mask):
            continue
        selected, error = nearest_palette_indices(edit_rgb[mask], palette)
        output[mask] = selected
        max_error = max(max_error, error)
    if int(output.max(initial=0)) >= max(len(palette) for palette in palettes.values()):
        raise ValueError(f"internal palette overflow while reading {path}")
    report = {"changed_pixels": int(np.count_nonzero(changed)), "max_squared_rgb_error": max_error}
    print(f"  [QUANTIZE] {path.name}: {report['changed_pixels']} pixels; max RGB error^2={max_error}")
    return output, report


def parse_images(core, data: bytes) -> list[dict]:
    images = core.parse_xtx_headers(data)
    core.configure_texture_dimensions(images)
    return [image for image in images if image.get("valid")]


def decode_sam_slot(core, data: bytes, image: dict) -> tuple[np.ndarray, dict]:
    width = int(image["width"])
    height = int(image["height"])
    payload = data[int(image["pstart"]): int(image["pend"])]
    words = np.frombuffer(payload, dtype="<u4").reshape(height, width)
    gsmem = np.zeros(1024 * 1024, dtype=np.uint32)
    for y in range(height):
        for x in range(width):
            gsmem[core.psmt4_tool.ct32_pos(x, y, width)] = words[y, x]
    logical_height = SAM_PANEL_HEIGHT
    logical_width = len(payload) * 2 // logical_height
    dbw4 = math.ceil(logical_width / 16)
    indices = core.psmt4_tool.decode_psmt4(gsmem, logical_width, logical_height, 0, dbw4).astype(np.uint8)
    return indices, {
        "logical_width": logical_width,
        "logical_height": logical_height,
        "dbw4": dbw4,
        "dbw32": width,
    }


def encode_sam_slot(core, data: bytes, image: dict, original: np.ndarray, edited: np.ndarray, layout: dict) -> bytes:
    width = int(image["width"])
    height = int(image["height"])
    payload = data[int(image["pstart"]): int(image["pend"])]
    words = np.frombuffer(payload, dtype="<u4").reshape(height, width)
    gsmem = np.zeros(1024 * 1024, dtype=np.uint32)
    for y in range(height):
        for x in range(width):
            gsmem[core.psmt4_tool.ct32_pos(x, y, width)] = words[y, x]
    changed_y, changed_x = np.where(edited != original)
    for y, x in zip(changed_y.tolist(), changed_x.tolist()):
        core.set_psmt4_pixel(gsmem, x, y, int(edited[y, x]), 0, int(layout["dbw4"]))
    rebuilt = np.zeros((height, width), dtype="<u4")
    for y in range(height):
        for x in range(width):
            rebuilt[y, x] = gsmem[core.psmt4_tool.ct32_pos(x, y, width)]
    return rebuilt.tobytes()


def extract_regular(
    core,
    source: Path,
    data: bytes,
    images: list[dict],
    out_dir: Path,
    ov11_resource: dict,
) -> dict:
    items = []
    palette_resource = ov11_resource.get("palette_resource", source.name)
    if palette_resource == source.name:
        palette_data, palette_images = data, images
    else:
        palette_path = BASE_DIR / palette_resource
        palette_data = palette_path.read_bytes()
        palette_images = parse_images(core, palette_data)
    gsmem = build_gsmem(core, palette_data, palette_images)
    cbps = sorted({int(region["cbp"]) for region in ov11_resource["regions"] if region["psm"] == "PSMT8"})
    palettes = {cbp: read_csm1_palette(core, gsmem, cbp, 256) for cbp in cbps}
    for ordinal, image in enumerate(images, 1):
        indices = core.img_to_unsw(data, image).astype(np.uint8)
        path = out_dir / f"PSMT8_{ordinal:03d}.png"
        palette_map = build_region_palette_map(indices.shape, ov11_resource["regions"], "PSMT8")
        Image.fromarray(render_region_rgb(indices, palette_map, palettes), "RGB").save(path)
        covered = int(np.count_nonzero(palette_map >= 0))
        items.append({
            "path": path.name,
            "sha256": sha256_file(path),
            "size": [int(indices.shape[1]), int(indices.shape[0])],
            "image_index": int(image["index"]),
            "pstart": int(image["pstart"]),
            "pend": int(image["pend"]),
            "max_index": 255,
            "palette_cbps": cbps,
            "regions": ov11_resource["regions"],
            "covered_pixels": covered,
            "total_pixels": int(indices.size),
        })
    return {
        "profile": "OV11_PSMT8_regions",
        "images": items,
        "palette_resource": palette_resource,
    }


def extract_sam(
    core,
    source: Path,
    data: bytes,
    images: list[dict],
    out_dir: Path,
    ov11_resource: dict,
) -> dict:
    items = []
    ordinal = 1
    slots = []
    gsmem = build_gsmem(core, data, images)
    cbp = int(ov11_resource["default_cbp"])
    palette = read_csm1_palette(core, gsmem, cbp, 16)
    external_palette = not np.any(palette)
    if external_palette:
        # OV11 points at GS CBP 0x7FE, beyond sam.xtx's uploaded bytes. The
        # CLUT is global runtime state, so sam remains an index-faithful
        # grayscale reference until that shared upload is identified.
        palette = grayscale_palette(16)
    for image in images:
        strip, layout = decode_sam_slot(core, data, image)
        slots.append({"image_index": int(image["index"]), **layout})
        for x0 in range(0, strip.shape[1], SAM_PANEL_WIDTH):
            width = min(SAM_PANEL_WIDTH, strip.shape[1] - x0)
            panel = strip[:, x0:x0 + width]
            path = out_dir / f"PSMT4_{ordinal:03d}.png"
            Image.fromarray(display_palette_rgb(palette)[panel], "RGB").save(path)
            items.append({
                "path": path.name,
                "sha256": sha256_file(path),
                "size": [width, SAM_PANEL_HEIGHT],
                "image_index": int(image["index"]),
                "x": x0,
                "max_index": 15,
                "palette_cbp": cbp,
            })
            ordinal += 1
    return {
        "profile": "OV11_SAM_PSMT4",
        "images": items,
        "slots": slots,
        "palette_source": "external GS CLUT at CBP 0x7FE" if external_palette else "sam.xtx",
    }


def extract_source(core, source: Path) -> dict:
    data = source.read_bytes()
    images = parse_images(core, data)
    palette_profile = load_palette_profile()
    ov11_resource = resource_profile(source, palette_profile)
    out_dir = extraction_dir(source)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[EXTRACT] {relative_source(source)}")
    if source.name.lower() == SAM_NAME:
        profile = extract_sam(core, source, data, images, out_dir, ov11_resource)
    else:
        profile = extract_regular(core, source, data, images, out_dir, ov11_resource)
    meta = {
        "tool": "tanaka_graphics_workflow",
        "version": 2,
        "source": relative_source(source),
        "source_size": len(data),
        "source_sha256": sha256_bytes(data),
        "ov11_sha256": palette_profile["ovl_sha256"],
        "palette_storage": palette_profile["palette_storage"],
        **profile,
        "edit_rule": "RGB/no-alpha OV11 palette reference. Keep originals unchanged; add *_KOR.png to replace changed pixels.",
    }
    (out_dir / META_NAME).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    update_catalog(source, out_dir, profile["profile"])
    print(f"  {len(profile['images'])} editable image(s) -> {out_dir}")
    return meta


def byte_diff_summary(original: bytes, rebuilt: bytes) -> dict:
    positions = [index for index, (left, right) in enumerate(zip(original, rebuilt)) if left != right]
    return {
        "original_size": len(original),
        "rebuilt_size": len(rebuilt),
        "changed_bytes": len(positions),
        "changed_span": None if not positions else [positions[0], positions[-1]],
    }


def rebuild_regular(core, data: bytes, meta: dict, folder: Path) -> tuple[bytes, list[str]]:
    parsed_images = parse_images(core, data)
    images = {int(image["index"]): image for image in parsed_images}
    palette_resource = meta.get("palette_resource", meta["source"])
    if palette_resource == meta["source"] or palette_resource == Path(meta["source"]).name:
        palette_data, palette_images = data, parsed_images
    else:
        palette_path = BASE_DIR / palette_resource
        palette_data = palette_path.read_bytes()
        palette_images = parse_images(core, palette_data)
    gsmem = build_gsmem(core, palette_data, palette_images)
    modified = bytearray(data)
    changed = []
    for item in meta["images"]:
        reference = folder / item["path"]
        edit = kor_path(reference)
        if not edit.is_file():
            continue
        expected = tuple(int(value) for value in item["size"])
        image = images[int(item["image_index"])]
        original = core.img_to_unsw(data, image).astype(np.uint8)
        edit_rgb, changed_pixels = read_edit_rgb(edit, reference, expected)
        palettes = {
            int(cbp): read_csm1_palette(core, gsmem, int(cbp), 256)
            for cbp in item["palette_cbps"]
        }
        palette_map = build_region_palette_map(original.shape, item["regions"], "PSMT8")
        indices, _quantize_report = quantize_changed_rgb(
            original, edit_rgb, changed_pixels, palette_map, palettes, edit
        )
        payload = core.unsw_to_pdata(indices, image)
        pstart, pend = int(image["pstart"]), int(image["pend"])
        if len(payload) != pend - pstart:
            raise ValueError(f"payload size mismatch for {edit}")
        modified[pstart:pend] = payload
        changed.append(edit.name)
    return bytes(modified), changed


def rebuild_sam(core, data: bytes, meta: dict, folder: Path) -> tuple[bytes, list[str]]:
    parsed_images = parse_images(core, data)
    images = {int(image["index"]): image for image in parsed_images}
    gsmem = build_gsmem(core, data, parsed_images)
    items_by_slot: dict[int, list[dict]] = {}
    for item in meta["images"]:
        items_by_slot.setdefault(int(item["image_index"]), []).append(item)
    modified = bytearray(data)
    changed = []
    for slot in meta["slots"]:
        image_index = int(slot["image_index"])
        image = images[image_index]
        original, layout = decode_sam_slot(core, data, image)
        edited = original.copy()
        slot_changed = False
        for item in items_by_slot.get(image_index, []):
            reference = folder / item["path"]
            edit = kor_path(reference)
            if not edit.is_file():
                continue
            expected = tuple(int(value) for value in item["size"])
            x0 = int(item["x"])
            original_panel = original[:, x0:x0 + expected[0]]
            edit_rgb, changed_pixels = read_edit_rgb(edit, reference, expected)
            cbp = int(item["palette_cbp"])
            palette = read_csm1_palette(core, gsmem, cbp, 16)
            if not np.any(palette):
                palette = grayscale_palette(16)
            palette_map = np.full(original_panel.shape, cbp, dtype=np.int32)
            panel, _quantize_report = quantize_changed_rgb(
                original_panel,
                edit_rgb,
                changed_pixels,
                palette_map,
                {cbp: palette},
                edit,
            )
            edited[:, x0:x0 + panel.shape[1]] = panel
            slot_changed = True
            changed.append(edit.name)
        if slot_changed:
            payload = encode_sam_slot(core, data, image, original, edited, layout)
            pstart, pend = int(image["pstart"]), int(image["pend"])
            if len(payload) != pend - pstart:
                raise ValueError(f"sam payload size mismatch for slot {image_index}")
            modified[pstart:pend] = payload
    return bytes(modified), changed


def rebuild_source(core, item: dict) -> Path:
    source = BASE_DIR / item["source"]
    folder = BASE_DIR / item["extract_dir"]
    meta_path = folder / META_NAME
    if not source.is_file() or not meta_path.is_file():
        raise FileNotFoundError(f"missing source or extraction metadata for {item['source']}")
    data = source.read_bytes()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if sha256_bytes(data) != meta["source_sha256"]:
        raise ValueError(f"source changed after extraction: {item['source']}")
    print(f"[REBUILD] {item['source']}")
    if meta["profile"] == "OV11_SAM_PSMT4":
        rebuilt, edits = rebuild_sam(core, data, meta, folder)
    elif meta["profile"] == "OV11_PSMT8_regions":
        rebuilt, edits = rebuild_regular(core, data, meta, folder)
    else:
        raise ValueError(f"unsupported profile: {meta['profile']}")
    if len(rebuilt) != len(data) or rebuilt[:4] != b"XTX\0":
        raise ValueError("rebuilt XTX failed size or magic validation")
    output = rebuilt_path(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(rebuilt)
    report = {
        "source": item["source"],
        "output": output.relative_to(BASE_DIR).as_posix(),
        "edited_images": edits,
        **byte_diff_summary(data, rebuilt),
        "source_sha256": sha256_bytes(data),
        "output_sha256": sha256_bytes(rebuilt),
    }
    (folder / "last_rebuild_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"  edited images: {len(edits)}")
    print(f"  changed bytes: {report['changed_bytes']}; span={report['changed_span']}")
    print(f"  output: {output}")
    return output


def print_list() -> None:
    catalog = {item["source"].lower(): item for item in load_catalog().get("assets", [])}
    for source in discover_sources():
        rel = relative_source(source)
        item = catalog.get(rel.lower())
        state = item["profile"] if item else "not extracted"
        print(f"{rel:<24} {state}")
    print("CASINO.res               text resource (excluded)")


def main() -> None:
    parser = argparse.ArgumentParser(description="XS1 tanaka casino XTX graphics workflow")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    sub.add_parser("extract-all")
    extract = sub.add_parser("extract")
    extract.add_argument("name")
    rebuild = sub.add_parser("rebuild")
    rebuild.add_argument("name")
    args = parser.parse_args()
    core = load_core()
    if args.command == "list":
        print_list()
    elif args.command == "extract-all":
        sources = discover_sources()
        print(f"Found {len(sources)} XTX resource(s).")
        for index, source in enumerate(sources, 1):
            print(f"\n[{index}/{len(sources)}]")
            extract_source(core, source)
    elif args.command == "extract":
        extract_source(core, select_source(args.name))
    elif args.command == "rebuild":
        rebuild_source(core, select_catalog_item(args.name))


if __name__ == "__main__":
    main()
