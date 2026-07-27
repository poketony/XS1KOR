# Tanaka casino graphics workflow

Run commands from `0.xenosaga0\tanaka`.

```bat
tanakagfx list
tanakagfx extract-all
tanakagfx extract help_all
tanakagfx rebuild help_all
```

Extracted images are stored under `graphics_extract` without changing source XTX files.
Rebuilt XTX files are written under `graphics_rebuilt` with the original relative path and filename.
After a clean extraction, each resource uses the direct path `graphics_extract\<resource name>`.

Keep extracted reference PNGs unchanged. Create translated images beside them with `_KOR` before `.png`:

```text
PSMT8_001.png -> PSMT8_001_KOR.png
PSMT4_001.png -> PSMT4_001_KOR.png
```

The PNGs are 24-bit RGB without an alpha channel. Their colors come from the GS CLUT addresses
hard-coded in read-only `metadata\OV11.OVL`, not from grayscale guesses. A file can use several
palettes in different UV regions; rebuild compares `_KOR` against the reference and quantizes each
changed pixel with the palette OV11 assigns to that region. Editing outside every OV11 texture
region is rejected instead of modifying unidentified data.

The palette bytes are normally embedded in the XTX upload itself. OV11 selects them through TEX0
CBP values relative to GS base `0x3800`; there is no separate casino `.lex` palette file.
`slot_1.xtx` is a cross-file exception: its `CBP 0x3FC` palette is supplied by `slot_2.xtx`.
`help_all.xtx` uses `CBP 0x1FC` for all three help regions.

`sam.xtx` is decoded as independent PSMT4 slots and split into 256x128 panels. OV11 selects
`CBP 0x7FE`, which is outside `sam.xtx` and belongs to shared runtime GS state. Until that global
CLUT upload is identified, these panels use an index-faithful high-contrast grayscale reference;
their indices still rebuild without palette overflow. The final blank `PSMT4_031.png` is padding.

`ov11_palette_profile.json` records the verified OV11 SHA-256 and per-resource UV/CBP mapping.
The workflow refuses to run if `metadata\OV11.OVL` does not match that profile.

When replacing an older extraction set, preserve any old `_KOR` files separately before running
the clean extraction because they may have been authored against the old grayscale presentation.

`CASINO.res` is a text/numeric resource and is intentionally excluded from this graphics workflow.

## CASINO.res text

Extract the fixed-width casino item text and its rebuild metadata:

```bat
casinotext extract
```

Extraction refuses to overwrite an existing translation. Use `casinotext extract --force` only
when the existing `CASINO.txt` and `CASINO.json` should intentionally be regenerated.

Edit only the third column of `text\CASINO.txt`. Keep `text\CASINO.json` unchanged, then rebuild:

```bat
casinotext rebuild
```

The rebuilt file is written to `text_rebuilt\CASINO.res`; the source file is never overwritten.
Korean text is converted with `..\carddata\XENOSAGA_KOR-JPN.json`. Encoded limits are 39 bytes
for item names, 79 bytes for item descriptions, and 27 bytes for bundle labels. Non-text values,
numeric tables, padding, and unedited fixed fields are preserved byte-for-byte.
