#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = BASE_DIR.parents[1]
CORE_TOOL = WORKSPACE_DIR / "xtx 개발소" / "xtx_tool_ver7_fixed_palette_formula.py"
EXTRACT_ROOT = BASE_DIR / "graphics_extract"
REBUILT_ROOT = BASE_DIR / "graphics_rebuilt"
CATALOG_PATH = EXTRACT_ROOT / "catalog.json"


# These are resource ownership relationships, not texture-format guesses.
# Each group is supported by the names and shared use of one XTX payload.
LEX_GROUPS = {
    "cardbase.bin": ["card_kage.lex", "card_m_c.lex", "card_m_p.lex", "card_m_r.lex", "card_m_u.lex"],
    "newcardbase.bin": ["NewCard_B.lex", "NewCard_E.lex", "NewCard_S.lex"],
    "deckmake/deck_gra.bin": ["decklist.lex", "deckmake.lex"],
    "deckmake/pack.bin": ["pack1.lex", "pack2.lex", "pack3.lex", "pack4.lex"],
    "deckmake/type2_0.xtx": [f"type2_{i}.lex" for i in range(7)],
    "game/1p2p.xtx": ["1p.lex", "2p.lex"],
    "game/cur2d.xtx": [f"cur2D_{side}{i}.lex" for side in ("0", "1") for i in range(1, 7)],
    "game/cur_waku.xtx": [f"Cur_Waku{x}.lex" for x in ("G", "R", "W", "Y")],
    "game/newlost3.xtx": ["NewLost1.lex", "NewLost2.lex"],
    "game/newpop.xtx": ["NewPop1P.lex", "NewPop2P.lex"],
    "game/pop.xtx": ["pop1.lex", "pop2.lex"],
    "game/up_lr.bin": ["up_L.lex", "up_R.lex"],
    "game/victory.bin": [f"victory{i}.lex" for i in range(1, 8)],
    "tuto/tu_000.xtx": ["Tu_000.lex"],
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_core_tool():
    if not CORE_TOOL.is_file():
        raise FileNotFoundError(f"core XTX tool not found: {CORE_TOOL}")
    spec = importlib.util.spec_from_file_location("xs1_xtx_core", CORE_TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load core XTX tool: {CORE_TOOL}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def relative_key(path: Path) -> str:
    return path.relative_to(BASE_DIR).as_posix()


def is_source_texture(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        magic = path.read_bytes()[:4]
    except OSError:
        return False
    return magic in (b"XTX\x00", b"ARX\x00")


def discover_sources() -> list[Path]:
    sources = []
    for path in BASE_DIR.rglob("*"):
        try:
            rel_parts = path.relative_to(BASE_DIR).parts
        except ValueError:
            continue
        if not rel_parts or rel_parts[0].lower() in {"graphics_extract", "graphics_rebuilt", "__pycache__"}:
            continue
        if is_source_texture(path):
            sources.append(path)
    return sorted(sources, key=lambda path: relative_key(path).lower())


def case_insensitive_file(directory: Path, name: str) -> Path | None:
    wanted = name.lower()
    try:
        return next((path for path in directory.iterdir() if path.is_file() and path.name.lower() == wanted), None)
    except OSError:
        return None


def resolve_lex_paths(source: Path) -> list[Path]:
    rel = relative_key(source).lower()
    directory = source.parent
    selected = []

    explicit_names = LEX_GROUPS.get(rel)
    if explicit_names:
        for name in explicit_names:
            path = case_insensitive_file(directory, name)
            if path is not None:
                selected.append(path)

    exact = case_insensitive_file(directory, source.stem + ".lex")
    if exact is not None:
        selected.append(exact)

    source_stem = source.stem.lower()
    if exact is None and not explicit_names:
        for path in directory.glob("*.lex"):
            lex_stem = path.stem.lower()
            if re.fullmatch(re.escape(source_stem) + r"[_-]?\d+[a-z]?", lex_stem):
                selected.append(path)

    # Tu_001..Tu_046 use the same model/material definition as Tu_000.
    if rel.startswith("tuto/tu_") and rel != "tuto/tu_000.xtx":
        shared = case_insensitive_file(directory, "Tu_000.lex")
        if shared is not None:
            selected.append(shared)

    unique = []
    seen = set()
    for path in selected:
        key = str(path.resolve()).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path.resolve())
    return unique


def parse_lex_group(core, lex_paths: list[Path]) -> list[dict]:
    materials = []
    for lex_path in lex_paths:
        for material in core.parse_lex_materials(str(lex_path), False, False):
            item = dict(material)
            item["lex_path"] = str(lex_path)
            item["lex_name"] = lex_path.name
            item["source"] = f"{lex_path.name}:{item['source']}"
            materials.append(item)
    materials = core._dedupe_materials(materials)
    materials.sort(key=lambda item: (
        item["vmin"], item["umin"],
        (item["vmax"] - item["vmin"]) * (item["umax"] - item["umin"]),
    ))
    return materials


def extraction_dir(source: Path) -> Path:
    rel = source.relative_to(BASE_DIR)
    standard = EXTRACT_ROOT / rel.parent / source.stem
    candidates = [
        standard,
        standard.with_name(standard.name + '_indexed'),
        standard.with_name(standard.name + '_indexed_v14'),
    ]
    for candidate in candidates:
        if not candidate.is_dir() or not any(candidate.glob('*_KOR.png')):
            if candidate != standard:
                print(f"  Existing _KOR edit preserved; new indexed extraction: {candidate}")
            return candidate
    raise ValueError(
        'all protected extraction folders contain _KOR edits; preserve one before re-extracting: '
        + ', '.join(str(path) for path in candidates)
    )


def rebuilt_path(source: Path) -> Path:
    rel = source.relative_to(BASE_DIR)
    return REBUILT_ROOT / rel


def load_catalog() -> dict:
    if not CATALOG_PATH.is_file():
        return {"version": 1, "assets": []}
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def save_catalog(catalog: dict) -> None:
    EXTRACT_ROOT.mkdir(parents=True, exist_ok=True)
    CATALOG_PATH.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")


def catalog_item(source: Path, lex_paths: list[Path], out_dir: Path) -> dict:
    return {
        "name": source.stem,
        "source": relative_key(source),
        "source_size": source.stat().st_size,
        "source_sha256": sha256_file(source),
        "lex": [relative_key(path) for path in lex_paths],
        "extract_dir": out_dir.relative_to(BASE_DIR).as_posix(),
        "rebuilt": rebuilt_path(source).relative_to(BASE_DIR).as_posix(),
    }


def update_catalog_item(item: dict) -> None:
    catalog = load_catalog()
    assets = [entry for entry in catalog.get("assets", []) if entry["source"].lower() != item["source"].lower()]
    assets.append(item)
    assets.sort(key=lambda entry: entry["source"].lower())
    catalog["version"] = 1
    catalog["assets"] = assets
    save_catalog(catalog)


def extract_source(core, source: Path) -> dict:
    lex_paths = resolve_lex_paths(source)
    out_dir = extraction_dir(source)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n[EXTRACT] {relative_key(source)}")
    if lex_paths:
        print("  LEX: " + ", ".join(path.name for path in lex_paths))
        materials = parse_lex_group(core, lex_paths)
        core.cmd_extract(
            str(source), str(out_dir), False, str(lex_paths[0]), False,
            False, False, False, "auto", False, [], True,
            _preparsed_mats=materials,
        )
    else:
        print("  LEX: unresolved; extracting indexed PSMT4/PSMT8 views")
        core.cmd_extract(str(source), str(out_dir))
    item = catalog_item(source, lex_paths, out_dir)
    update_catalog_item(item)
    return item


def select_sources(query: str | None, allow_all: bool) -> list[Path]:
    sources = discover_sources()
    if allow_all and query is None:
        return sources
    if not query:
        raise ValueError("a texture name or relative path is required")
    needle = query.replace("\\", "/").lower()
    exact = [path for path in sources if relative_key(path).lower() == needle]
    if exact:
        return exact
    matches = [
        path for path in sources
        if path.stem.lower() == needle
        or relative_key(path.with_suffix("")).lower() == needle
    ]
    if not matches:
        raise ValueError(f"texture not found: {query}")
    if len(matches) > 1:
        choices = ", ".join(relative_key(path) for path in matches)
        raise ValueError(f"ambiguous texture name '{query}': {choices}")
    return matches


def select_catalog_item(query: str) -> dict:
    catalog = load_catalog()
    needle = query.replace("\\", "/").lower()
    matches = []
    for item in catalog.get("assets", []):
        source = Path(item["source"])
        source_no_ext = source.with_suffix("").as_posix().lower()
        if item["source"].lower() == needle or source_no_ext == needle or source.stem.lower() == needle:
            matches.append(item)
    if not matches:
        raise ValueError(f"no extraction catalog entry for '{query}'; run 'cardgfx extract {query}' first")
    if len(matches) > 1:
        choices = ", ".join(item["source"] for item in matches)
        raise ValueError(f"ambiguous texture name '{query}': {choices}")
    return matches[0]


def rebuild_source(core, item: dict) -> Path:
    source = BASE_DIR / item["source"]
    folder = BASE_DIR / item["extract_dir"]
    # Rebuilt files live under a separate root, so preserve the source name.
    # Do not trust older catalog entries that still contain a _rebuilt suffix.
    output = rebuilt_path(source)
    if not source.is_file() or not folder.is_dir():
        raise FileNotFoundError(f"missing source or extraction folder for {item['source']}")
    if sha256_file(source) != item["source_sha256"]:
        raise ValueError(f"source changed after extraction: {item['source']}; extract it again first")
    lex_paths = [BASE_DIR / path for path in item.get("lex", [])]
    lex_path = str(lex_paths[0]) if lex_paths else None
    output.parent.mkdir(parents=True, exist_ok=True)
    print(f"[REBUILD] {item['source']}")
    core.cmd_import(str(source), str(folder), str(output), lex_path=lex_path)
    return output


def print_list() -> None:
    catalog = load_catalog()
    known = {item["source"].lower(): item for item in catalog.get("assets", [])}
    for source in discover_sources():
        rel = relative_key(source)
        state = "extracted" if rel.lower() in known else "not extracted"
        lex = resolve_lex_paths(source)
        lex_note = ",".join(path.name for path in lex) if lex else "NO-LEX"
        print(f"{rel:<36} {state:<13} {lex_note}")


def main() -> None:
    parser = argparse.ArgumentParser(description="XS1 carddata XTX extract/rebuild workflow")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("extract-all", help="extract every XTX-magic resource under carddata")
    extract = sub.add_parser("extract", help="extract one resource by name or relative path")
    extract.add_argument("name")
    rebuild = sub.add_parser("rebuild", help="rebuild one previously extracted resource")
    rebuild.add_argument("name")
    sub.add_parser("list", help="list resources, extraction state, and selected LEX files")
    args = parser.parse_args()

    core = load_core_tool()
    if args.command == "extract-all":
        sources = select_sources(None, True)
        print(f"Found {len(sources)} XTX-magic resource(s).")
        failures = []
        for index, source in enumerate(sources, 1):
            try:
                extract_source(core, source)
            except Exception as exc:
                failures.append((relative_key(source), str(exc)))
                print(f"[ERROR {index}/{len(sources)}] {relative_key(source)}: {exc}", file=sys.stderr)
        print(f"\nExtracted: {len(sources) - len(failures)}/{len(sources)}")
        if failures:
            for name, message in failures:
                print(f"  FAILED {name}: {message}")
            raise SystemExit(1)
    elif args.command == "extract":
        extract_source(core, select_sources(args.name, False)[0])
    elif args.command == "rebuild":
        output = rebuild_source(core, select_catalog_item(args.name))
        print(f"Rebuilt file: {output}")
    else:
        print_list()


if __name__ == "__main__":
    main()
