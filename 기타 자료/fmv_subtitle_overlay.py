#!/usr/bin/env python3
"""
Create Xenosaga-style FMV subtitle overlays from common subtitle files.

The tool converts SRT/WebVTT/ASS cues into a styled ASS overlay and can
optionally burn that overlay into a video with ffmpeg.  It also supports a
bottom drawbox mask to cover the original hardcoded Japanese subtitles.
"""

from __future__ import annotations

import argparse
import dataclasses
import pathlib
import re
import subprocess
import sys
from typing import Iterable


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent


def find_resource_root() -> pathlib.Path:
    candidates = [
        SCRIPT_DIR,
        SCRIPT_DIR.parent,
        SCRIPT_DIR.parent.parent,
    ]
    for candidate in candidates:
        if (candidate / "기타 자료" / "SUB" / "a예서체.ttf").exists():
            return candidate
    return SCRIPT_DIR.parent


ROOT = find_resource_root()
DEFAULT_FONT = ROOT / "기타 자료" / "SUB" / "a예서체.ttf"
DEFAULT_PUNCT_FONT = ROOT / "기타 자료" / "SUB" / "BGREIRR.TTF"
DEFAULT_PUNCT_FONT_NAME = "Reishoreiryu"
GAME_SPACING_SCALE = 100.0
DEFAULT_PLAYRES_X = 512
DEFAULT_PLAYRES_Y = 448
DEFAULT_LINE1_Y = 380
DEFAULT_LINE2_Y = 415
VIDEO_EXTENSIONS = {".m2v"}
SUBTITLE_EXTENSIONS = {".srt", ".vtt", ".ass", ".ssa"}


@dataclasses.dataclass
class Cue:
    start: str
    end: str
    text: str


def parse_time(value: str) -> str:
    value = value.strip().replace(",", ".")
    match = re.match(r"^(?:(\d+):)?(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?$", value)
    if not match:
        raise ValueError(f"Unsupported timestamp: {value!r}")

    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    centis = int((match.group(4) or "0").ljust(3, "0")[:2])
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centis:02d}"


def parse_srt_or_vtt(text: str) -> list[Cue]:
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"^WEBVTT[^\n]*(?:\n\n|\n)", "", text, flags=re.IGNORECASE)
    blocks = re.split(r"\n{2,}", text.strip())
    cues: list[Cue] = []

    for block in blocks:
        lines = [line.strip("\n") for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        if "-->" not in lines[0] and len(lines) > 1:
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            continue

        start_raw, end_raw = lines[0].split("-->", 1)
        end_raw = end_raw.split()[0]
        body = "\n".join(strip_basic_tags(line) for line in lines[1:]).strip()
        if body:
            cues.append(Cue(parse_time(start_raw), parse_time(end_raw), body))

    return cues


def parse_ass(text: str) -> list[Cue]:
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    in_events = False
    fields: list[str] = []
    cues: list[Cue] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.lower() == "[events]":
            in_events = True
            continue
        if line.startswith("[") and line.endswith("]"):
            in_events = False
            continue
        if not in_events:
            continue
        if line.lower().startswith("format:"):
            fields = [field.strip().lower() for field in line.split(":", 1)[1].split(",")]
            continue
        if not line.lower().startswith("dialogue:") or not fields:
            continue

        payload = line.split(":", 1)[1].lstrip()
        parts = payload.split(",", maxsplit=len(fields) - 1)
        if len(parts) != len(fields):
            continue
        row = dict(zip(fields, parts))
        body = row.get("text", "")
        body = re.sub(r"\{[^}]*\}", "", body).replace("\\N", "\n").replace("\\n", "\n")
        body = strip_basic_tags(body).strip()
        if body:
            cues.append(Cue(parse_time(row["start"]), parse_time(row["end"]), body))

    return cues


def strip_basic_tags(text: str) -> str:
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    return re.sub(r"</?[^>]+>", "", text)


def read_cues(path: pathlib.Path) -> list[Cue]:
    raw = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() in {".ass", ".ssa"}:
        cues = parse_ass(raw)
    else:
        cues = parse_srt_or_vtt(raw)
    if not cues:
        raise ValueError(f"No subtitle cues found in {path}")
    return cues


def is_generated_ass(path: pathlib.Path) -> bool:
    name = path.name.lower()
    return name.endswith(".xeno_fmv.ass") or name.endswith("_kor.ass")


def list_folder_pairs(folder: pathlib.Path) -> list[tuple[pathlib.Path, pathlib.Path]]:
    videos = sorted(
        path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    )
    subtitles = sorted(
        path
        for path in folder.iterdir()
        if path.is_file()
        and path.suffix.lower() in SUBTITLE_EXTENSIONS
        and not is_generated_ass(path)
    )

    if not videos:
        raise ValueError(f"No .m2v files found in folder: {folder}")
    if not subtitles:
        raise ValueError(f"No subtitle files found in folder: {folder}")

    if len(videos) == 1 and len(subtitles) == 1:
        return [(videos[0], subtitles[0])]

    remaining_subtitles = subtitles[:]
    pairs: list[tuple[pathlib.Path, pathlib.Path]] = []
    for video in videos:
        exact = next((sub for sub in remaining_subtitles if sub.stem == video.stem), None)
        if exact is None:
            exact = next((sub for sub in remaining_subtitles if sub.stem.startswith(video.stem)), None)
        if exact is not None:
            pairs.append((video, exact))
            remaining_subtitles.remove(exact)

    if not pairs:
        raise ValueError(
            "No matching video/subtitle pairs found. Use the same base filename, "
            "or put only one .m2v and one subtitle file in the folder."
        )

    return pairs


def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    value = hex_rgb.strip().lstrip("#")
    if len(value) != 6:
        raise ValueError(f"Expected RRGGBB color, got {hex_rgb!r}")
    r, g, b = value[0:2], value[2:4], value[4:6]
    return f"&H{alpha:02X}{b}{g}{r}"


def ass_alpha(value: int) -> int:
    return max(0, min(255, value))


def escape_ass_text(text: str) -> str:
    text = text.replace("\\", "\\\\")
    text = text.replace("{", r"\{").replace("}", r"\}")
    return text.replace("\n", r"\N")


def uses_main_font(char: str) -> bool:
    codepoint = ord(char)
    is_hangul = (
        0xAC00 <= codepoint <= 0xD7A3
        or 0x1100 <= codepoint <= 0x11FF
        or 0x3130 <= codepoint <= 0x318F
        or 0xA960 <= codepoint <= 0xA97F
        or 0xD7B0 <= codepoint <= 0xD7FF
    )
    return is_hangul or char == ","


def escape_ass_char(char: str) -> str:
    if char == "\n":
        return r"\N"
    if char == "\\":
        return r"\\"
    if char == "{":
        return r"\{"
    if char == "}":
        return r"\}"
    return char


def ass_font_tag(font_name: str) -> str:
    return r"{\fn" + font_name.replace("}", "") + "}"


def ass_override_tag(
    *,
    blur: float,
    pos_x: float | None,
    pos_y: float | None,
) -> str:
    tags = f"\\blur{blur:g}"
    if pos_x is not None and pos_y is not None:
        tags += f"\\pos({pos_x:g},{pos_y:g})"
    return "{" + tags + "}" if tags else ""


def format_mixed_font_text(text: str, main_font_name: str, punct_font_name: str) -> str:
    parts: list[str] = []
    active_font: str | None = None

    for char in text:
        desired_font = main_font_name if uses_main_font(char) else punct_font_name
        if desired_font != active_font:
            parts.append(ass_font_tag(desired_font))
            active_font = desired_font
        parts.append(escape_ass_char(char))

    return "".join(parts)


def line_position(index: int, line1_y: float, line2_y: float, line_gap: float | None) -> float:
    if index == 0:
        return line1_y
    if index == 1:
        return line2_y
    gap = line_gap if line_gap is not None else line2_y - line1_y
    return line2_y + gap * (index - 1)


def write_ass(
    cues: Iterable[Cue],
    path: pathlib.Path,
    *,
    playres_x: int,
    playres_y: int,
    font_name: str,
    punct_font_name: str,
    font_size: int,
    spacing: float,
    margin_v: int,
    pos_x: float | None,
    pos_y: float | None,
    line1_y: float,
    line2_y: float,
    line_gap: float | None,
    split_lines: bool,
    outline: float,
    shadow: float,
    blur: float,
    spread: bool,
    spread_outline: float,
    spread_blur: float,
    spread_alpha: int,
    primary_color: str,
    outline_color: str,
    shadow_color: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {playres_x}",
        f"PlayResY: {playres_y}",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        (
            "Style: XenoSpread,"
            f"{font_name},{font_size},{ass_color('000000', ass_alpha(spread_alpha))},"
            f"{ass_color('000000', ass_alpha(spread_alpha))},"
            f"{ass_color('000000', ass_alpha(spread_alpha))},"
            f"{ass_color('000000', ass_alpha(spread_alpha))},0,0,0,0,100,100,{spacing},0,"
            f"3,{spread_outline},0,2,24,24,{margin_v},1"
        ),
        (
            "Style: XenoFMV,"
            f"{font_name},{font_size},{ass_color(primary_color)},"
            f"{ass_color(primary_color)},{ass_color(outline_color)},"
            f"{ass_color(shadow_color)},0,0,0,0,100,100,{spacing},0,"
            f"1,{outline},{shadow},2,24,24,{margin_v},1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for cue in cues:
        cue_lines = cue.text.splitlines() if split_lines else [cue.text]
        for index, cue_line in enumerate(cue_lines):
            if not cue_line.strip():
                continue
            line_pos_y = line_position(index, line1_y, line2_y, line_gap) if split_lines else pos_y
            formatted_text = format_mixed_font_text(cue_line, font_name, punct_font_name)
            if spread:
                lines.append(
                    f"Dialogue: 0,{cue.start},{cue.end},XenoSpread,,0000,0000,0000,,"
                    f"{ass_override_tag(blur=spread_blur, pos_x=pos_x, pos_y=line_pos_y)}"
                    f"{formatted_text}"
                )
            lines.append(
                f"Dialogue: 1,{cue.start},{cue.end},XenoFMV,,0000,0000,0000,,"
                f"{ass_override_tag(blur=blur, pos_x=pos_x, pos_y=line_pos_y)}"
                f"{formatted_text}"
            )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def ffmpeg_filter_path(path: pathlib.Path) -> str:
    normalized = path.resolve().as_posix()
    normalized = normalized.replace("\\", "/").replace(":", r"\:")
    normalized = normalized.replace("'", r"\'")
    return normalized


def burn_video(
    video: pathlib.Path,
    ass_path: pathlib.Path,
    output: pathlib.Path,
    *,
    fonts_dir: pathlib.Path,
    mask_y: int,
    mask_h: int,
    mask_alpha: float,
    video_codec: str,
    crf: int,
    preset: str,
    prores_profile: int,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    filters: list[str] = []
    if mask_alpha > 0 and mask_h > 0:
        filters.append(f"drawbox=x=0:y={mask_y}:w=iw:h={mask_h}:color=black@{mask_alpha}:t=fill")
    filters.append(
        "subtitles="
        f"filename='{ffmpeg_filter_path(ass_path)}':"
        f"fontsdir='{ffmpeg_filter_path(fonts_dir)}'"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video),
        "-map",
        "0",
        "-vf",
        ",".join(filters),
        "-c:v",
        video_codec,
    ]
    if video_codec == "prores_ks":
        cmd += [
            "-profile:v",
            str(prores_profile),
            "-pix_fmt",
            "yuv422p10le",
            "-vendor",
            "apl0",
        ]
    else:
        cmd += [
            "-crf",
            str(crf),
            "-preset",
            preset,
        ]
    cmd += [
        "-c:a",
        "copy",
        str(output),
    ]
    subprocess.run(cmd, check=True)


def output_paths_for_pair(
    video: pathlib.Path,
    subtitle: pathlib.Path,
    *,
    output_dir: pathlib.Path | None,
) -> tuple[pathlib.Path, pathlib.Path]:
    base_dir = output_dir or video.parent
    ass_out = base_dir / f"{video.stem}_KOR.ass"
    video_out = base_dir / f"{video.stem}_KOR.mov"
    return ass_out, video_out


def render_subtitle(
    subtitle: pathlib.Path,
    ass_out: pathlib.Path,
    args: argparse.Namespace,
    *,
    font: pathlib.Path,
    punct_font: pathlib.Path,
    video: pathlib.Path | None = None,
    video_out: pathlib.Path | None = None,
) -> None:
    cues = read_cues(subtitle)
    ass_spacing = args.ass_spacing
    if ass_spacing is None:
        ass_spacing = args.spacing / GAME_SPACING_SCALE
    pos_x = None if args.no_fixed_pos else args.pos_x
    pos_y = None if args.no_fixed_pos else args.pos_y
    if pos_x is None and not args.no_fixed_pos:
        pos_x = args.playres_x / 2
    if pos_y is None and not args.no_fixed_pos:
        pos_y = args.line2_y

    write_ass(
        cues,
        ass_out,
        playres_x=args.playres_x,
        playres_y=args.playres_y,
        font_name=args.font_name or font.stem,
        punct_font_name=args.punct_font_name,
        font_size=args.font_size,
        spacing=ass_spacing,
        margin_v=args.margin_v,
        pos_x=pos_x,
        pos_y=pos_y,
        line1_y=args.line1_y,
        line2_y=args.line2_y,
        line_gap=args.line_gap,
        split_lines=not args.no_split_lines,
        outline=args.outline,
        shadow=args.shadow,
        blur=args.blur,
        spread=not args.no_spread,
        spread_outline=args.spread_outline,
        spread_blur=args.spread_blur,
        spread_alpha=args.spread_alpha,
        primary_color=args.primary_color,
        outline_color=args.outline_color,
        shadow_color=args.shadow_color,
    )
    print(f"Wrote ASS overlay: {ass_out}")

    if video is not None:
        if video_out is None:
            video_out = video.with_name(f"{video.stem}.subbed.mp4")
        burn_video(
            video,
            ass_out,
            video_out,
            fonts_dir=font.parent,
            mask_y=args.mask_y,
            mask_h=args.mask_h,
            mask_alpha=args.mask_alpha,
            video_codec=args.video_codec,
            crf=args.crf,
            preset=args.preset,
            prores_profile=args.prores_profile,
        )
        print(f"Wrote burned video: {video_out}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate Xenosaga-style FMV subtitle ASS overlays and optional burned videos."
    )
    parser.add_argument(
        "input",
        type=pathlib.Path,
        help="Input subtitle file, or a folder containing .m2v and subtitle pairs",
    )
    parser.add_argument("--video", type=pathlib.Path, help="Optional input video to burn subtitles into")
    parser.add_argument("--out", type=pathlib.Path, help="Output video path when --video is used")
    parser.add_argument("--ass-out", type=pathlib.Path, help="Output ASS path")
    parser.add_argument("--output-dir", type=pathlib.Path, help="Output folder for folder mode")
    parser.add_argument("--font", type=pathlib.Path, default=DEFAULT_FONT, help="Font file used by ffmpeg/libass")
    parser.add_argument("--font-name", default=None, help="ASS font family name. Defaults to font file stem.")
    parser.add_argument(
        "--punct-font",
        type=pathlib.Path,
        default=DEFAULT_PUNCT_FONT,
        help="Font file used for non-Hangul characters and punctuation",
    )
    parser.add_argument(
        "--punct-font-name",
        default=DEFAULT_PUNCT_FONT_NAME,
        help="ASS font family name for punctuation",
    )
    parser.add_argument("--playres-x", type=int, default=DEFAULT_PLAYRES_X)
    parser.add_argument("--playres-y", type=int, default=DEFAULT_PLAYRES_Y)
    parser.add_argument(
        "--pos-x",
        type=float,
        default=None,
        help="Fixed subtitle anchor X. Defaults to the horizontal center.",
    )
    parser.add_argument(
        "--pos-y",
        type=float,
        default=None,
        help="Fixed subtitle anchor Y for unsplit block mode.",
    )
    parser.add_argument("--line1-y", type=float, default=DEFAULT_LINE1_Y, help="Fixed Y for subtitle row 1")
    parser.add_argument("--line2-y", type=float, default=DEFAULT_LINE2_Y, help="Fixed Y for subtitle row 2")
    parser.add_argument("--line-gap", type=float, default=None, help="Y gap for row 3 and later")
    parser.add_argument(
        "--no-split-lines",
        action="store_true",
        help="Keep multiline cues as one ASS dialogue block.",
    )
    parser.add_argument(
        "--no-fixed-pos",
        action="store_true",
        help="Use ASS MarginV positioning instead of a fixed \\pos anchor.",
    )
    parser.add_argument("--font-size", type=int, default=24)
    parser.add_argument(
        "--spacing",
        type=float,
        default=200.0,
        help="Game-style character spacing. 150 is converted to 1.5 ASS pixels.",
    )
    parser.add_argument(
        "--ass-spacing",
        type=float,
        default=None,
        help="Direct ASS character spacing override. Usually leave this unset.",
    )
    parser.add_argument("--margin-v", type=int, default=28, help="Distance from bottom in script pixels")
    parser.add_argument("--outline", type=float, default=2.6)
    parser.add_argument("--shadow", type=float, default=5.0)
    parser.add_argument("--blur", type=float, default=2.5)
    parser.add_argument(
        "--no-spread",
        action="store_true",
        help="Disable the extra black spread layer behind subtitles.",
    )
    parser.add_argument(
        "--spread-outline",
        type=float,
        default=5.0,
        help="Padding size for the black spread layer. Larger values fill wider gaps.",
    )
    parser.add_argument("--spread-blur", type=float, default=7.0)
    parser.add_argument(
        "--spread-alpha",
        type=int,
        default=16,
        help="ASS alpha for black spread. 0 is opaque, 255 is transparent.",
    )
    parser.add_argument("--primary-color", default="FFFFFF")
    parser.add_argument("--outline-color", default="000000")
    parser.add_argument("--shadow-color", default="000000")
    parser.add_argument("--mask-y", type=int, default=360, help="Top of original subtitle cover box")
    parser.add_argument("--mask-h", type=int, default=88, help="Height of original subtitle cover box")
    parser.add_argument("--mask-alpha", type=float, default=0.0, help="0 disables the cover box")
    parser.add_argument("--video-codec", default="prores_ks")
    parser.add_argument("--prores-profile", type=int, default=3, help="3 = ProRes 422 HQ")
    parser.add_argument("--crf", type=int, default=16)
    parser.add_argument("--preset", default="slow")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    input_path = args.input.resolve()
    if not input_path.exists():
        parser.error(f"Input path not found: {input_path}")

    font = args.font.resolve()
    if not font.exists():
        parser.error(f"Font file not found: {font}")
    punct_font = args.punct_font.resolve()
    if not punct_font.exists():
        parser.error(f"Punctuation font file not found: {punct_font}")

    output_dir = args.output_dir.resolve() if args.output_dir else None
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)

    if input_path.is_dir():
        if args.video or args.out or args.ass_out:
            parser.error("--video, --out, and --ass-out are file-mode options. Use --output-dir for folder mode.")
        pairs = list_folder_pairs(input_path)
        print(f"Found {len(pairs)} pair(s) in folder: {input_path}")
        for video, subtitle in pairs:
            ass_out, video_out = output_paths_for_pair(video, subtitle, output_dir=output_dir)
            print(f"Processing pair: {video.name} + {subtitle.name}")
            render_subtitle(
                subtitle,
                ass_out.resolve(),
                args,
                font=font,
                punct_font=punct_font,
                video=video.resolve(),
                video_out=video_out.resolve(),
            )
        return 0

    subtitle = input_path
    if subtitle.suffix.lower() not in SUBTITLE_EXTENSIONS:
        parser.error(f"Input file is not a supported subtitle: {subtitle}")

    ass_out = args.ass_out
    if ass_out is None:
        ass_out = subtitle.with_suffix(".xeno_fmv.ass")
    ass_out = ass_out.resolve()

    video = None
    video_out = None
    if args.video:
        video = args.video.resolve()
        if not video.exists():
            parser.error(f"Video file not found: {video}")
        video_out = args.out.resolve() if args.out else video.with_name(f"{video.stem}.subbed.mov")

    render_subtitle(
        subtitle,
        ass_out,
        args,
        font=font,
        punct_font=punct_font,
        video=video,
        video_out=video_out,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
