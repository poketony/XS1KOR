# BXX Roundtrip Tool

Drag a `.bxx` file onto `BXX_Drop_Here.bat` to extract it.
Drag the extracted folder back onto the BAT to rebuild it.

Each embedded XTX folder contains only two edit images:

- `PSMT4.png`: indexed palette PNG. Pixel values are exact `0..15`; the palette is brightened only so it is editable by eye.
- `PSMT8.png`: indexed palette PNG. Pixel values are exact `0..255`; the palette is grayscale for display.

Rebuild behavior:

- If neither PNG changed, that XTX is copied byte-for-byte.
- If exactly one of `PSMT4.png` or `PSMT8.png` changed, that file is used.
- If both changed, rebuild stops with an error.
- RGB/RGBA PNGs are rejected. The tool does not quantize colors.
- Changing the visible palette colors alone does not change the underlying indices; edit pixels as index values.
- No offsets, sizes, names, `xtxinfo` blocks, or entry tables are regenerated.
- No-edit BXX rebuild copies `original.bxx` byte-for-byte.

Files in an extract folder:

- `bxx_meta.json`: rebuild metadata
- `original.bxx`: untouched input copy
- `entries\NN_name\original.bin`: raw BXX entry bytes
- `entries\NN_name\xtx_MM_name\original.xtx`: untouched embedded XTX
- `entries\NN_name\xtx_MM_name\PSMT4.png`: edit image
- `entries\NN_name\xtx_MM_name\PSMT8.png`: edit image
- `entries\NN_name\xtx_MM_name\xtx_meta.json`: XTX geometry and PNG hashes

Manual CLI:

```powershell
python C:\Users\JO\XS1KOR\codex-lab\bxx_roundtrip_tool\bxx_roundtrip_tool.py C:\path\to\file.bxx
python C:\Users\JO\XS1KOR\codex-lab\bxx_roundtrip_tool\bxx_roundtrip_tool.py C:\path\to\file_bxx_extract
```
