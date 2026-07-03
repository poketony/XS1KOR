#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path


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


def find_npr_tool() -> Path:
    here = Path(__file__).resolve()
    sibling = here.parent.parent / "npr_roundtrip_tool" / "npr_roundtrip_tool.py"
    if sibling.is_file():
        return sibling
    fallback = find_repo_root() / "codex-lab" / "npr_roundtrip_tool" / "npr_roundtrip_tool.py"
    if fallback.is_file():
        return fallback
    raise RuntimeError("npr_roundtrip_tool.py was not found")


npr_tool = load_module(
    "xs1kor_shared_npr_roundtrip_tool",
    find_npr_tool(),
)


def dispatch(path: Path, out: Path | None = None) -> Path:
    if path.is_file() and path.suffix.lower() == ".bxx":
        return npr_tool.extract_bxx(path, out)
    if path.is_dir() and (path / "bxx_meta.json").exists():
        return npr_tool.rebuild_bxx_folder(path, out)
    raise ValueError("Drop either a .bxx file or an extracted folder containing bxx_meta.json")


def main() -> None:
    parser = argparse.ArgumentParser(description="BXX extract/rebuild tool for XTX PSMT4/PSMT8 index PNGs")
    parser.add_argument("path", type=Path, help=".bxx file to extract, or extracted folder to rebuild")
    parser.add_argument("--out", type=Path, help="output folder for extract or output .bxx for rebuild")
    args = parser.parse_args()
    dispatch(args.path, args.out)


if __name__ == "__main__":
    main()
