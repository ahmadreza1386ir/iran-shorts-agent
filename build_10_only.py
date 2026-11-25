#!/usr/bin/env python3
"""
Rebuild short_4 using ONLY the first 10 generated paintings (scene_01.jpg .. scene_10.jpg)
stretched evenly across the exact audio duration, burning synchronized subtitles from
output/audio/short_4.srt with compliant yuv420p & tv color range.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"H:\AHMADREZA\پروژه ها\iran-shorts-agent")
VISUALS_DIR = ROOT / "assets" / "visuals" / "short_4"
MANIFEST_PATH = VISUALS_DIR / "manifest.json"
OUT_VIDEO = ROOT / "output" / "pending_approval" / "short_4.mp4"
AUDIO_DIR = ROOT / "output" / "audio"
SRT_PATH = AUDIO_DIR / "short_4.srt"


def find_narration_audio() -> Path | None:
    for ext in (".mp3", ".wav", ".m4a", ".aac", ".ogg"):
        p = AUDIO_DIR / f"short_4{ext}"
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def get_duration(media_path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(media_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(res.stdout.strip())


_TIME_RE = re.compile(
    r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{1,3})"
)


def parse_srt(path: Path) -> list[tuple[float, float, str]]:
    if not path.is_file():
        return []
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        text = None
        for enc in ("utf-8-sig", "cp1256", "utf-8"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            text = raw.decode("utf-8", errors="replace")

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cues: list[tuple[float, float, str]] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.split("\n")
        for i, line in enumerate(lines):
            m = _TIME_RE.search(line)
            if not m:
                continue
            g = m.groups()
            start = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(f"{g[3]:0<3}") / 1000.0
            end = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(f"{g[7]:0<3}") / 1000.0
            body = "\n".join(x.strip() for x in lines[i + 1 :] if x.strip())
            if body and end > start:
                cues.append((start, end, body))
            break
    cues.sort(key=lambda c: c[0])
    return cues


def find_caption_font() -> tuple[str, list[Path]]:
    dirs = [
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
        Path.home() / "AppData/Local/Microsoft/Windows/Fonts",
        Path("/usr/share/fonts/truetype/dejavu"),
    ]
    options = [
        ("Vazirmatn", ["Vazirmatn-Bold.ttf", "Vazirmatn-Regular.ttf", "Vazirmatn[wght].ttf"]),
        ("Tahoma", ["tahomabd.ttf", "tahoma.ttf"]),
        ("Segoe UI", ["segoeuib.ttf", "segoeui.ttf"]),
        ("DejaVu Sans", ["DejaVuSans-Bold.ttf", "DejaVuSans.ttf"]),
    ]
    for family, names in options:
        files = [d / n for d in dirs for n in names if (d / n).is_file()]
        if files:
            return family, files[:2]
    return "Tahoma", []


def ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")


def ass_time(t: float) -> str:
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def build_ass(cues: list[tuple[float, float, str]], total: float, family: str) -> str:
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Main,%s,60,&H00FFFFFF,&H000000FF,&H00101010,&HC8000000,-1,0,0,0,100,100,0,0,1,5,3,2,85,85,375,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    ) % family
    lines = []
    for i, (s, e, text) in enumerate(cues):
        if i + 1 < len(cues):
            e = min(e, cues[i + 1][0])
        e = min(e, total)
        if e - s < 0.05 or s >= total:
            continue
        lines.append(f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Main,,0,0,0,,{{\\fad(80,60)}}{ass_escape(text)}")
    return head + "\n".join(lines) + "\n"


def _escape_filter_path(p: Path) -> str:
    s = p.resolve().as_posix()
    return s.replace(":", r"\:").replace("'", r"\'")


def main() -> int:
    images: list[Path] = []
    for i in range(1, 11):
        img = VISUALS_DIR / f"scene_{i:02d}.jpg"
        if not img.is_file():
            print(f"[ERROR] تصویر پیدا نشد: {img}", file=sys.stderr)
            return 1
        images.append(img)

    if MANIFEST_PATH.is_file():
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        scenes = data.get("scenes", {})
        data["scenes"] = {
            str(k): v for k, v in scenes.items()
            if str(k).isdigit() and 1 <= int(k) <= 10
        }
        MANIFEST_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    if not OUT_VIDEO.is_file():
        print("[1/3] Running standard assembler for audio/subtitles...")
        res_asm = subprocess.run(
            [sys.executable, str(ROOT / "03_video_assembler.py"), "--only", "4"],
            cwd=str(ROOT),
        )
        if res_asm.returncode != 0:
            return res_asm.returncode
    else:
        print("[1/3] Using existing short_4.mp4 audio track (0 token cost)...")

    narration_src = find_narration_audio()
    total_dur = get_duration(narration_src) if narration_src else get_duration(OUT_VIDEO)
    fps = 30
    target_frames = round(total_dur * fps)
    exact_video_dur = target_frames / fps

    base_frames = target_frames // 10
    rem_frames = target_frames % 10
    scene_frames_list = [
        base_frames + (1 if idx < rem_frames else 0)
        for idx in range(10)
    ]

    work_dir = VISUALS_DIR / "_parts_10"
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    temp_audio = work_dir / "mixed_audio.aac"
    concat_raw = work_dir / "concat_raw.mp4"
    temp_final = work_dir / "final_10scenes_subtitled.mp4"

    print(
        f"[2/3] Rendering 10 distinct painting segments ({target_frames} frames total, "
        f"{exact_video_dur:.3f}s)..."
    )

    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(OUT_VIDEO), "-vn", "-c:a", "copy", str(temp_audio),
        ],
        cwd=str(work_dir),
        check=True,
    )

    part_paths: list[Path] = []
    for idx, (img, s_frames) in enumerate(zip(images, scene_frames_list), start=1):
        s_frames = max(1, s_frames)
        s_dur = s_frames / fps
        part_out = work_dir / f"part_{idx:02d}.mp4"
        part_paths.append(part_out)

        if idx % 2 == 1:
            z_expr = "1.0+0.0005*on"
        else:
            z_expr = f"{1.0 + 0.0005 * s_frames:.4f}-0.0005*on"

        vf = (
            f"scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,"
            f"zoompan=z='{z_expr}':x='(iw-ow)/2':y='(ih-oh)/2':d={s_frames}:s=1080x1920:fps={fps},"
            f"scale=in_range=full:out_range=tv,format=yuv420p,setsar=1"
        )
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-loop", "1", "-i", str(img),
                "-vf", vf,
                "-frames:v", str(s_frames),
                "-t", f"{s_dur:.4f}",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-profile:v", "high", "-level", "4.2",
                "-pix_fmt", "yuv420p", "-color_range", "tv",
                "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                "-an", str(part_out),
            ],
            cwd=str(work_dir),
            check=True,
        )

    concat_list = work_dir / "list.txt"
    concat_list.write_text(
        "\n".join(f"file '{p.name}'" for p in part_paths) + "\n",
        encoding="utf-8",
    )

    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c:v", "copy",
            "-frames:v", str(target_frames),
            "-t", f"{exact_video_dur:.4f}",
            str(concat_raw),
        ],
        cwd=str(work_dir),
        check=True,
    )

    cues = [(s, min(e, exact_video_dur), t) for (s, e, t) in parse_srt(SRT_PATH) if s < exact_video_dur]
    vf_final = ["eq=contrast=1.04:saturation=1.07"]
    if cues:
        family, fonts = find_caption_font()
        for f in fonts:
            shutil.copyfile(f, work_dir / f.name)
        ass_file = work_dir / "subs.ass"
        ass_file.write_text(build_ass(cues, exact_video_dur, family), encoding="utf-8-sig")
        ass_escaped = _escape_filter_path(ass_file)
        fontsdir_escaped = _escape_filter_path(work_dir)
        vf_final.append(f"subtitles=filename='{ass_escaped}':fontsdir='{fontsdir_escaped}'")
        print(f"   burning {len(cues)} subtitle cue(s) using font '{family}'...")
    else:
        print("   [WARN] short_4.srt not found or empty; building without burned subtitles.")

    vf_final.append("scale=in_range=full:out_range=tv,format=yuv420p,setsar=1")

    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(concat_raw),
            "-i", str(temp_audio),
            "-vf", ",".join(vf_final),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "medium", "-crf", "19",
            "-profile:v", "high", "-level", "4.2",
            "-pix_fmt", "yuv420p", "-color_range", "tv",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-c:a", "copy",
            "-frames:v", str(target_frames),
            "-t", f"{exact_video_dur:.4f}",
            "-movflags", "+faststart",
            str(temp_final),
        ],
        cwd=str(work_dir),
        check=True,
    )

    temp_final.replace(OUT_VIDEO)
    shutil.rmtree(work_dir, ignore_errors=True)

    print("[3/3] Verifying all outputs...")
    res_ver = subprocess.run([sys.executable, str(ROOT / "verify_outputs.py")], cwd=str(ROOT))
    return res_ver.returncode


if __name__ == "__main__":
    raise SystemExit(main())