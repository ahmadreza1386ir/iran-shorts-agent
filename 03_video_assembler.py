#!/usr/bin/env python3
"""
Phase 3: Fast vertical Shorts assembler with mood-matched music, SFX & -14 LUFS voice.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from common import (
    AUDIO_DIR,
    FPS,
    HEIGHT,
    IMAGE_EXTS,
    MUSIC_DIR,
    OUTPUT_DIR,
    VISUALS_DIR,
    WIDTH,
    ToolError,
    find_tool,
    load_plan,
    load_scripts,
    parse_srt,
    probe_duration,
    run,
    setup_logging,
)

log = setup_logging("assembler")

SFX_DIR = VISUALS_DIR.parent / "sfx"
MIN_SCENE_FRAMES = 2 * FPS
SNAP_SECONDS = 1.6
TARGET_LUFS = -14.0
MOTION_CYCLE = ["push_in", "pan_right", "drift_up", "pull_out", "pan_left", "drift_down"]

# فرمت رنگ در ASS به صورت &H00BBGGRR& است
ACCENT_COLORS_ASS: list[str] = [
    "&H005CE6FF&",  # طلایی گرم
    "&H00F2D468&",  # فیروزه‌ای روشن
    "&H0080F5A8&",  # سبز ملایم
    "&H009EC5FF&",  # کهربایی/نارنجی ملایم
]

HIGHLIGHT_WORDS = {
    "mountain", "mountains", "path", "paths", "time", "truth", "wisdom",
    "secret", "never", "always", "stone", "water", "fire", "gold", "king",
    "trust", "fate", "choice", "patience", "silence", "story", "lesson",
}


def _atomic_move_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        shutil.copyfile(src, tmp_path)
        if not tmp_path.is_file() or tmp_path.stat().st_size == 0:
            raise RuntimeError(f"Generated output is empty: {src}")
        os.replace(tmp_path, dst)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


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
    return (
        text.replace("\\", "\\\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\n", r"\N")
    )


def ass_time(t: float) -> str:
    cs = max(0, int(round(t * 100)))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _stylize_line_ass(text: str, line_idx: int) -> str:
    base_accent = ACCENT_COLORS_ASS[line_idx % len(ACCENT_COLORS_ASS)]
    words = text.split()
    if not words:
        return ""

    anim_prefix = "{\\fad(75,55)\\fscx92\\fscy92\\t(0,130,\\fscx103\\fscy103)\\t(130,240,\\fscx100\\fscy100)}"
    styled_tokens: list[str] = []

    rng = random.Random(line_idx * 997 + len(text))
    emph_indices: set[int] = set()
    for idx, w in enumerate(words):
        clean_w = re.sub(r"[^A-Za-z0-9']+", "", w).lower()
        if clean_w in HIGHLIGHT_WORDS:
            emph_indices.add(idx)

    if not emph_indices and len(words) >= 3:
        emph_indices.add(rng.randrange(len(words)))

    for idx, w in enumerate(words):
        esc_w = ass_escape(w)
        if idx in emph_indices:
            styled_tokens.append(
                f"{{\\c{base_accent}\\fscx116\\fscy116\\b1}}{esc_w}{{\\c&H00FFFFFF&\\fscx100\\fscy100\\b-1}}"
            )
        elif len(words) >= 5 and idx == len(words) - 1 and rng.random() < 0.35:
            styled_tokens.append(f"{{\\c{base_accent}}}{esc_w}{{\\c&H00FFFFFF&}}")
        else:
            styled_tokens.append(esc_w)

    return anim_prefix + " ".join(styled_tokens)


def build_ass(
    cues: list[tuple[float, float, str]],
    total: float,
    family: str,
    margin_v: int = 510,
    font_size: int = 64,
) -> str:
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Main,%s,%d,&H00FFFFFF,&H000000FF,&H00121212,&HB8000000,-1,0,0,0,100,100,0,0,1,6,3,2,80,80,%d,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    ) % (WIDTH, HEIGHT, family, font_size, margin_v)

    lines: list[str] = []
    valid_idx = 0
    for i, (s, e, text) in enumerate(cues):
        s_clamped = max(0.0, min(float(s), total))
        e_clamped = min(float(e), total)
        if i + 1 < len(cues):
            next_start = max(0.0, min(float(cues[i + 1][0]), total))
            e_clamped = min(e_clamped, next_start)
        if e_clamped - s_clamped < 0.05 or s_clamped >= total:
            continue
        styled_text = _stylize_line_ass(text, valid_idx)
        lines.append(f"Dialogue: 0,{ass_time(s_clamped)},{ass_time(e_clamped)},Main,,0,0,0,,{styled_text}")
        valid_idx += 1

    return head + "\n".join(lines) + ("\n" if lines else "")


def cut_candidates(cues: list[tuple[float, float, str]]) -> list[float]:
    out: list[float] = []
    for a, b in zip(cues, cues[1:]):
        out.append((a[1] + b[0]) / 2 if b[0] - a[1] >= 0.12 else b[0])
    return out


def scene_boundaries(
    n: int,
    dur: float,
    texts: list[str],
    anchors: list[str | None],
    cues: list[tuple[float, float, str]],
) -> tuple[list[int], list[str]]:
    notes: list[str] = []
    total = max(n * MIN_SCENE_FRAMES, round(dur * FPS))
    if n <= 1:
        return [], notes
    cands = cut_candidates(cues)
    times: list[float] = []
    for i in range(1, n):
        anchor = anchors[i] if i < len(anchors) else None
        frac = None
        if anchor:
            for txt in texts:
                pos = txt.lower().find(anchor.lower())
                if pos >= 0:
                    frac = pos / max(1, len(txt))
                    break
            if frac is None:
                notes.append(f"scene {i + 1}: anchor not found -> equal share")
        if frac is None:
            frac = i / n
        t = frac * dur
        near = min(cands, key=lambda c: abs(c - t), default=None)
        if near is not None and abs(near - t) <= SNAP_SECONDS:
            notes.append(f"scene {i + 1}: cut {t:5.2f}s -> {near:5.2f}s (snapped to pause)")
            t = near
        else:
            notes.append(f"scene {i + 1}: cut {t:5.2f}s (no pause nearby)")
        times.append(t)
    frames: list[int] = []
    for i, t in enumerate(times):
        lo = (frames[-1] if frames else 0) + MIN_SCENE_FRAMES
        hi = total - MIN_SCENE_FRAMES * (n - 1 - i)
        frames.append(max(lo, min(hi, round(t * FPS))))
    return frames, notes


def _motion_expr(motion: str, frames_n: int) -> tuple[str, str, str]:
    denom = max(1, frames_n - 1)
    p = f"(on/{denom})"
    if motion == "pan_right":
        return "1.08", f"(iw-ow)*{p}", "(ih-oh)/2"
    if motion == "pan_left":
        return "1.08", f"(iw-ow)*(1-{p})", "(ih-oh)/2"
    if motion == "drift_up":
        return "1.08", "(iw-ow)/2", f"(ih-oh)*(1-{p})"
    if motion == "drift_down":
        return "1.08", "(iw-ow)/2", f"(ih-oh)*{p}"
    if motion == "pull_out":
        return f"1.09-0.09*{p}", "(iw-ow)/2", "(ih-oh)/2"
    return f"1.00+0.09*{p}", "(iw-ow)/2", "(ih-oh)/2"


def render_fast_video(
    ffmpeg: str,
    work: Path,
    sid: str,
    plan_scenes: list[dict[str, Any]],
    still_paths: list[Path],
    bounds: list[int],
    total_frames: int,
    with_subs: bool,
    preset: str,
) -> None:
    full_bounds = [0] + bounds + [total_frames]
    n_scenes = len(full_bounds) - 1
    seg_files: list[Path] = []

    for i in range(n_scenes):
        s_frames = max(1, full_bounds[i + 1] - full_bounds[i])
        s_dur = s_frames / FPS
        motion = (plan_scenes[i].get("motion") if i < len(plan_scenes) else None) or MOTION_CYCLE[i % len(MOTION_CYCLE)]
        seg_out = work / f"seg_{i:02d}.mp4"
        seg_files.append(seg_out)

        z_expr, x_expr, y_expr = _motion_expr(motion, s_frames)
        fade_out_st = max(0.0, s_dur - 0.18)
        run(
            [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-loop", "1", "-i", str(still_paths[i]),
                "-vf", (
                    f"scale={WIDTH * 2}:{HEIGHT * 2}:flags=lanczos,"
                    f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':d={s_frames}:s={WIDTH}x{HEIGHT}:fps={FPS},"
                    f"fade=t=in:st=0:d=0.18,fade=t=out:st={fade_out_st:.3f}:d=0.18,"
                    f"format=yuv420p,setsar=1"
                ),
                "-t", f"{s_dur:.4f}",
                "-c:v", "libx264", "-preset", preset, "-crf", "19", "-pix_fmt", "yuv420p",
                "-an", str(seg_out),
            ],
            cwd=work,
        )

    concat_list = work / "segs.txt"
    concat_list.write_text("\n".join(f"file '{p.name}'" for p in seg_files) + "\n", encoding="utf-8")

    vf_parts = ["eq=contrast=1.04:saturation=1.07"]
    if with_subs and (work / "subs.ass").is_file():
        vf_parts.append("subtitles=subs.ass:fontsdir=.")
    vf_parts.append("scale=in_range=full:out_range=tv:flags=bicubic,format=yuv420p,setsar=1")

    run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", concat_list.name,
            "-vf", ",".join(vf_parts),
            "-frames:v", str(total_frames), "-r", str(FPS),
            "-c:v", "libx264", "-preset", preset, "-crf", "18", "-profile:v", "high", "-level", "4.2",
            "-pix_fmt", "yuv420p", "-color_range", "tv", "-g", str(FPS * 2),
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-an", "video.mp4",
        ],
        cwd=work,
    )


VOICE_CHAIN = (
    "highpass=f=75,equalizer=f=250:t=q:w=1:g=-2,equalizer=f=3200:t=q:w=1:g=2,deesser=i=0.25,"
    "acompressor=threshold=0.125:ratio=3:attack=12:release=180:makeup=1.5"
)


def master_voice(ffmpeg: str, work: Path, src: str) -> str:
    ln = f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=9"
    p1 = run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", src, "-af", f"{VOICE_CHAIN},{ln}:print_format=json", "-f", "null", "-"],
        cwd=work,
    )
    blocks = re.findall(r"\{[^{}]*\}", p1.stderr)
    try:
        m = json.loads(blocks[-1])
        meas = ":".join(
            f"{k}={m[v]}"
            for k, v in (
                ("measured_I", "input_i"),
                ("measured_TP", "input_tp"),
                ("measured_LRA", "input_lra"),
                ("measured_thresh", "input_thresh"),
                ("offset", "target_offset"),
            )
        )
        f2 = f"{VOICE_CHAIN},{ln}:{meas}:linear=true"
        note = f"loudnorm 2-pass (in {m['input_i']} LUFS -> {TARGET_LUFS})"
    except (IndexError, KeyError, ValueError):
        f2, note = f"{VOICE_CHAIN},{ln}", "loudnorm 1-pass"
    run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", src,
            "-af", f"{f2},aresample=48000,aformat=channel_layouts=stereo",
            "-ar", "48000", "-ac", "2", "voice.wav",
        ],
        cwd=work,
    )
    return note


def _detect_mood(script: dict[str, Any], plan_scenes: list[dict[str, Any]]) -> str:
    blob = " ".join([
        str(script.get("proverb_en") or ""),
        str(script.get("narration") or ""),
        " ".join(str(s.get("desc") or "") for s in plan_scenes),
    ]).lower()
    if any(k in blob for k in ("water", "stream", "frog", "ocean", "drop", "ripples", "pond")):
        return "stream"
    if any(k in blob for k in ("night", "winter", "cold", "ice", "star", "moon")):
        return "night"
    if any(k in blob for k in ("bazaar", "scale", "merchant", "wheat", "sack", "gold")):
        return "bazaar"
    return "story"


def pick_music(sid: str, mood: str = "story") -> Path | None:
    if not MUSIC_DIR.is_dir():
        return None
    files = sorted(p for p in MUSIC_DIR.iterdir() if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".ogg", ".flac"})
    if not files:
        return None
    mood_keywords: dict[str, tuple[str, ...]] = {
        "stream": ("water", "stream", "river", "nature", "calm", "ambient", "lofi"),
        "night": ("night", "calm", "ambient", "soft", "pad", "moon"),
        "bazaar": ("warm", "folk", "acoustic", "story", "gold", "persian"),
        "story": ("story", "calm", "warm", "ambient"),
    }
    matched = [p for p in files if any(kw in p.stem.lower() for kw in mood_keywords.get(mood, ()))]
    pool = matched if matched else files
    return pool[int(hashlib.sha1(f"{sid}:{mood}".encode()).hexdigest(), 16) % len(pool)]


def pick_sfx_files(mood: str) -> list[Path]:
    if not SFX_DIR.is_dir():
        return []
    files = sorted(p for p in SFX_DIR.iterdir() if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".ogg", ".flac"})
    if not files:
        return []
    mood_hints: dict[str, tuple[str, ...]] = {
        "stream": ("water", "drop", "whoosh", "swish", "chime", "soft"),
        "night": ("whoosh", "chime", "wind", "soft", "bell"),
        "bazaar": ("chime", "whoosh", "wood", "soft", "tap"),
        "story": ("whoosh", "chime", "soft"),
    }
    preferred = [p for p in files if any(h in p.stem.lower() for h in mood_hints.get(mood, ()))]
    return preferred if preferred else files


def _build_sfx_bus(ffmpeg: str, work: Path, bounds_sec: list[float], dur: float, mood: str) -> tuple[str, str]:
    cut_times = [t for t in bounds_sec if 0.30 < t < dur - 0.30][:16]
    if not cut_times:
        return "", ""
    sfx_files = pick_sfx_files(mood)
    if sfx_files:
        copied: list[str] = []
        for idx, sf in enumerate(sfx_files[:3]):
            dest_name = f"sfx_src_{idx}{sf.suffix.lower()}"
            shutil.copyfile(sf, work / dest_name)
            copied.append(dest_name)
        inputs_cmd: list[str] = []
        chains: list[str] = []
        mix_tags: list[str] = []
        for c_i, t_cut in enumerate(cut_times):
            src_name = copied[c_i % len(copied)]
            ms = int(round(t_cut * 1000))
            tag = f"s{c_i}"
            inputs_cmd += ["-i", src_name]
            chains.append(
                f"[{c_i + 1}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                f"atrim=0:0.45,asetpts=PTS-STARTPTS,afade=t=in:d=0.04,afade=t=out:st=0.18:d=0.25,"
                f"volume=0.22,adelay={ms}|{ms}[{tag}]"
            )
            mix_tags.append(f"[{tag}]")
        chains.append(f"{''.join(mix_tags)}amix=inputs={len(mix_tags)}:normalize=0,lowpass=f=2400[sfx_out]")
        sfx_wav = work / "sfx_bus.wav"
        run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *inputs_cmd, "-filter_complex", ";".join(chains), "-map", "[sfx_out]", "-t", f"{dur:.4f}", str(sfx_wav)],
            cwd=work,
        )
        return str(sfx_wav), "sfx-files"

    parts: list[str] = []
    mix_tags = []
    for c_i, t_cut in enumerate(cut_times):
        ms = int(round(t_cut * 1000))
        tag = f"w{c_i}"
        parts.append(
            f"anoisesrc=color=pink:r=48000:a=0.08:d=0.28,aformat=channel_layouts=stereo,"
            f"bandpass=f=420:width_type=h:w=320,afade=t=in:d=0.06,afade=t=out:st=0.10:d=0.18,"
            f"volume=0.28,adelay={ms}|{ms}[{tag}]"
        )
        mix_tags.append(f"[{tag}]")
    parts.append(f"{''.join(mix_tags)}amix=inputs={len(mix_tags)}:normalize=0,lowpass=f=1400[sfx_out]")
    sfx_wav = work / "sfx_bus.wav"
    run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-filter_complex", ";".join(parts), "-map", "[sfx_out]", "-t", f"{dur:.4f}", str(sfx_wav)],
        cwd=work,
    )
    return str(sfx_wav), "soft-whoosh"


def _scene_modulated_bed(dur: float, bounds_sec: list[float], music_stream_tag: str | None, mood: str) -> tuple[str, str]:
    fades = f"afade=t=in:d=1.5,afade=t=out:st={max(0.0, dur - 2.0):.3f}:d=2.0"
    level = "loudnorm=I=-28:TP=-3:LRA=8,aresample=48000"
    d = f"{dur + 0.5:.3f}"
    if music_stream_tag:
        base_bed = f"{music_stream_tag}atrim=duration={d},aformat=sample_rates=48000:channel_layouts=stereo,{level}[raw_bed]"
    else:
        chord_map: dict[str, tuple[float, float, float]] = {
            "stream": (146.83, 220.00, 329.63),
            "night": (130.81, 196.00, 293.66),
            "bazaar": (146.83, 220.00, 277.18),
            "story": (146.83, 220.00, 329.63),
        }
        f1, f2, f3 = chord_map.get(mood, chord_map["story"])
        base_bed = (
            f"sine=f={f1}:r=48000:d={d}[s1];sine=f={f2}:r=48000:d={d}[s2];sine=f={f3}:r=48000:d={d}[s3];"
            f"anoisesrc=color=pink:r=48000:a=0.05:d={d}[nz];"
            f"[s1][s2][s3][nz]amix=inputs=4:normalize=0,aformat=channel_layouts=stereo,"
            f"lowpass=f=680,tremolo=f=0.12:d=0.22,{level}[raw_bed]"
        )
    mod_expr_terms: list[str] = ["0.92"]
    for i, t_cut in enumerate(bounds_sec[:16]):
        amp = 0.10 if (i % 2 == 0) else -0.08
        mod_expr_terms.append(f"{amp}*exp(-((t-{t_cut:.3f})*(t-{t_cut:.3f}))/0.50)")
    mod_bed = f"[raw_bed]volume='{'+'.join(mod_expr_terms)}':eval=frame,{fades}[bed]"
    return f"{base_bed};{mod_bed}", f"mood:{mood}"


def mux_final(
    ffmpeg: str,
    work: Path,
    dur: float,
    music: Path | None,
    use_bed: bool,
    bounds_sec: list[float],
    mood: str,
    out_name: str,
) -> str:
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", "video.mp4", "-i", "voice.wav"]
    next_idx = 2
    music_tag: str | None = None
    if use_bed and music:
        cmd += ["-stream_loop", "-1", "-i", f"music{music.suffix.lower()}"]
        music_tag = f"[{next_idx}:a]"
        next_idx += 1

    sfx_wav, sfx_tag = ("", "")
    sfx_tag_in: str | None = None
    if use_bed:
        sfx_wav, sfx_tag = _build_sfx_bus(ffmpeg, work, bounds_sec, dur, mood)
        if sfx_wav:
            cmd += ["-i", sfx_wav]
            sfx_tag_in = f"[{next_idx}:a]"
            next_idx += 1

    if not use_bed:
        g = f"[1:a]alimiter=limit=0.89:level=0,apad=whole_dur={dur:.4f},atrim=end={dur:.4f},asetpts=PTS-STARTPTS[aout]"
        cmd += [
            "-filter_complex", g, "-map", "0:v:0", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-ac", "2", "-t", f"{dur:.4f}", "-movflags", "+faststart", out_name,
        ]
        run(cmd, cwd=work)
        return "off"

    bed_graph_str, bed_label = _scene_modulated_bed(dur, bounds_sec, music_tag, mood)
    parts = [
        "[1:a]asplit=2[va][vb]",
        bed_graph_str,
        "[bed][vb]sidechaincompress=threshold=0.045:ratio=8:attack=20:release=450:makeup=1[bedd]",
    ]
    if sfx_tag_in:
        parts.append(f"{sfx_tag_in}aformat=sample_rates=48000:channel_layouts=stereo[sfx_clean]")
        parts.append("[va][bedd][sfx_clean]amix=inputs=3:duration=first:normalize=0[mix]")
        tag_desc = f"{bed_label}+{sfx_tag}"
    else:
        parts.append("[va][bedd]amix=inputs=2:duration=first:normalize=0[mix]")
        tag_desc = bed_label

    parts.append(f"[mix]alimiter=limit=0.89:level=0,apad=whole_dur={dur:.4f},atrim=end={dur:.4f},asetpts=PTS-STARTPTS[aout]")
    cmd += [
        "-filter_complex", ";".join(parts),
        "-map", "0:v:0", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-ac", "2", "-t", f"{dur:.4f}", "-movflags", "+faststart", t[dict[str, Any]]) -> str:
    blob = " ".join([
        str(script.get("proverb_en") or ""),
        str(script.get("narration") or ""),
        " ".join(str(s.get("desc") or "") for s in plan_scenes),
    ]).lower()
    if any(k in blob for k in ("water", "stream", "frog", "ocean", "drop", "ripples", "pond")):
        return "stream"
    if any(k in blob for k in ("night", "winter", "cold", "ice", "star", "moon")):
        return "night"
    if any(k in blob for k in ("bazaar", "scale", "merchant", "wheat", "sack", "gold")):
        return "bazaar"
    return "story"


def pick_music(sid: str, mood: str = "story") -> Path | None:
    if not MUSIC_DIR.is_dir():
        return None
    files = sorted(p for p in MUSIC_DIR.iterdir() if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".ogg", ".flac"})
    if not files:
        return None
    mood_keywords: dict[str, tuple[str, ...]] = {
        "stream": ("water", "stream", "river", "nature", "calm", "ambient", "lofi"),
        "night": ("night", "calm", "ambient", "soft", "pad", "moon"),
        "bazaar": ("warm", "folk", "acoustic", "story", "gold", "persian"),
        "story": ("story", "calm", "warm", "ambient"),
    }
    matched = [p for p in files if any(kw in p.stem.lower() for kw in mood_keywords.get(mood, ()))]
    pool = matched if matched else files
    return pool[int(hashlib.sha1(f"{sid}:{mood}".encode()).hexdigest(), 16) % len(pool)]


def pick_sfx_files(mood: str) -> list[Path]:
    if not SFX_DIR.is_dir():
        return []
    files = sorted(p for p in SFX_DIR.iterdir() if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".ogg", ".flac"})
    if not files:
        return []
    mood_hints: dict[str, tuple[str, ...]] = {
        "stream": ("water", "drop", "whoosh", "swish", "chime", "soft"),
        "night": ("whoosh", "chime", "wind", "soft", "bell"),
        "bazaar": ("chime", "whoosh", "wood", "soft", "tap"),
        "story": ("whoosh", "chime", "soft"),
    }
    preferred = [p for p in files if any(h in p.stem.lower() for h in mood_hints.get(mood, ()))]
    return preferred if preferred else files


def _build_sfx_bus(ffmpeg: str, work: Path, bounds_sec: list[float], dur: float, mood: str) -> tuple[str, str]:
    cut_times = [t for t in bounds_sec if 0.30 < t < dur - 0.30][:16]
    if not cut_times:
        return "", ""
    sfx_files = pick_sfx_files(mood)
    if sfx_files:
        copied: list[str] = []
        for idx, sf in enumerate(sfx_files[:3]):
            dest_name = f"sfx_src_{idx}{sf.suffix.lower()}"
            shutil.copyfile(sf, work / dest_name)
            copied.append(dest_name)
        inputs_cmd: list[str] = []
        chains: list[str] = []
        mix_tags: list[str] = []
        for c_i, t_cut in enumerate(cut_times):
            src_name = copied[c_i % len(copied)]
            ms = int(round(t_cut * 1000))
            tag = f"s{c_i}"
            inputs_cmd += ["-i", src_name]
            chains.append(
                f"[{c_i + 1}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                f"atrim=0:0.45,asetpts=PTS-STARTPTS,afade=t=in:d=0.04,afade=t=out:st=0.18:d=0.25,"
                f"volume=0.22,adelay={ms}|{ms}[{tag}]"
            )
            mix_tags.append(f"[{tag}]")
        chains.append(f"{''.join(mix_tags)}amix=inputs={len(mix_tags)}:normalize=0,lowpass=f=2400[sfx_out]")
        sfx_wav = work / "sfx_bus.wav"
        run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *inputs_cmd, "-filter_complex", ";".join(chains), "-map", "[sfx_out]", "-t", f"{dur:.4f}", str(sfx_wav)],
            cwd=work,
        )
        return str(sfx_wav), "sfx-files"

    parts: list[str] = []
    mix_tags = []
    for c_i, t_cut in enumerate(cut_times):
        ms = int(round(t_cut * 1000))
        tag = f"w{c_i}"
        parts.append(
            f"anoisesrc=color=pink:r=48000:a=0.08:d=0.28,aformat=channel_layouts=stereo,"
            f"bandpass=f=420:width_type=h:w=320,afade=t=in:d=0.06,afade=t=out:st=0.10:d=0.18,"
            f"volume=0.28,adelay={ms}|{ms}[{tag}]"
        )
        mix_tags.append(f"[{tag}]")
    parts.append(f"{''.join(mix_tags)}amix=inputs={len(mix_tags)}:normalize=0,lowpass=f=1400[sfx_out]")
    sfx_wav = work / "sfx_bus.wav"
    run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-filter_complex", ";".join(parts), "-map", "[sfx_out]", "-t", f"{dur:.4f}", str(sfx_wav)],
        cwd=work,
    )
    return str(sfx_wav), "soft-whoosh"


def _scene_modulated_bed(dur: float, bounds_sec: list[float], music_stream_tag: str | None, mood: str) -> tuple[str, str]:
    fades = f"afade=t=in:d=1.5,afade=t=out:st={max(0.0, dur - 2.0):.3f}:d=2.0"
    level = "loudnorm=I=-28:TP=-3:LRA=8,aresample=48000"
    d = f"{dur + 0.5:.3f}"
    if music_stream_tag:
        base_bed = f"{music_stream_tag}atrim=duration={d},aformat=sample_rates=48000:channel_layouts=stereo,{level}[raw_bed]"
    else:
        chord_map: dict[str, tuple[float, float, float]] = {
            "stream": (146.83, 220.00, 329.63),
            "night": (130.81, 196.00, 293.66),
            "bazaar": (146.83, 220.00, 277.18),
            "story": (146.83, 220.00, 329.63),
        }
        f1, f2, f3 = chord_map.get(mood, chord_map["story"])
        base_bed = (
            f"sine=f={f1}:r=48000:d={d}[s1];sine=f={f2}:r=48000:d={d}[s2];sine=f={f3}:r=48000:d={d}[s3];"
            f"anoisesrc=color=pink:r=48000:a=0.05:d={d}[nz];"
            f"[s1][s2][s3][nz]amix=inputs=4:normalize=0,aformat=channel_layouts=stereo,"
            f"lowpass=f=680,tremolo=f=0.12:d=0.22,{level}[raw_bed]"
        )
    mod_expr_terms: list[str] = ["0.92"]
    for i, t_cut in enumerate(bounds_sec[:16]):
        amp = 0.10 if (i % 2 == 0) else -0.08
        mod_expr_terms.append(f"{amp}*exp(-((t-{t_cut:.3f})*(t-{t_cut:.3f}))/0.50)")
    mod_bed = f"[raw_bed]volume='{'+'.join(mod_expr_terms)}':eval=frame,{fades}[bed]"
    return f"{base_bed};{mod_bed}", f"mood:{mood}"


def mux_final(
    ffmpeg: str,
    work: Path,
    dur: float,
    music: Path | None,
    use_bed: bool,
    bounds_sec: list[float],
    mood: str,
    out_name: str,
) -> str:
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", "video.mp4", "-i", "voice.wav"]
    next_idx = 2
    music_tag: str | None = None
    if use_bed and music:
        cmd += ["-stream_loop", "-1", "-i", f"music{music.suffix.lower()}"]
        music_tag = f"[{next_idx}:a]"
        next_idx += 1

    sfx_wav, sfx_tag = ("", "")
    sfx_tag_in: str | None = None
    if use_bed:
        sfx_wav, sfx_tag = _build_sfx_bus(ffmpeg, work, bounds_sec, dur, mood)
        if sfx_wav:
            cmd += ["-i", sfx_wav]
            sfx_tag_in = f"[{next_idx}:a]"
            next_idx += 1

    if not use_bed:
        g = f"[1:a]alimiter=limit=0.89:level=0,apad=whole_dur={dur:.4f},atrim=end={dur:.4f},asetpts=PTS-STARTPTS[aout]"
        cmd += [
            "-filter_complex", g, "-map", "0:v:0", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-ac", "2", "-t", f"{dur:.4f}", "-movflags", "+faststart", out_name,
        ]
        run(cmd, cwd=work)
        return "off"

    bed_graph_str, bed_label = _scene_modulated_bed(dur, bounds_sec, music_tag, mood)
    parts = [
        "[1:a]asplit=2[va][vb]",
        bed_graph_str,
        "[bed][vb]sidechaincompress=threshold=0.045:ratio=8:attack=20:release=450:makeup=1[bedd]",
    ]
    if sfx_tag_in:
        parts.append(f"{sfx_tag_in}aformat=sample_rates=48000:channel_layouts=stereo[sfx_clean]")
        parts.append("[va][bedd][sfx_clean]amix=inputs=3:duration=first:normalize=0[mix]")
        tag_desc = f"{bed_label}+{sfx_tag}"
    else:
        parts.append("[va][bedd]amix=inputs=2:duration=first:normalize=0[mix]")
        tag_desc = bed_label

    parts.append(f"[mix]alimiter=limit=0.89:level=0,apad=whole_dur={dur:.4f},atrim=end={dur:.4f},asetpts=PTS-STARTPTS[aout]")
    cmd += [
        "-filter_complex", ";".join(parts),
        "-map", "0:v:0", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-ac", "2", "-t", f"{dur:.4f}", "-movflags", "+faststart", out_name,
    ]
    run(cmd, cwd=work)
    return tag_desc


def measure_loudness(ffmpeg: str, path: Path) -> str:
    try:
        res = run([ffmpeg, "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=peak=true", "-vn", "-f", "null", "-"], check=False)
        i = re.findall(r"\n\s*I:\s+(-?[\d.]+) LUFS", res.stderr)
        lra = re.findall(r"\n\s*LRA:\s+([\d.]+) LU", res.stderr)
        tp = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", res.stderr)
        return f"{i[-1]} LUFS, LRA {lra[-1]}, TP {tp[-1]} dBFS" if i and lra and tp else "n/a"
    except Exception:
        return "n/a"


def find_audio(sid: str) -> Path | None:
    for ext in (".mp3", ".wav", ".m4a", ".aac", ".ogg"):
        p = AUDIO_DIR / f"short_{sid}{ext}"
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def collect_scene_images(sid: str, n: int) -> list[Path]:
    d = VISUALS_DIR / f"short_{sid}"
    imgs: list[Path] = []
    for i in range(1, n + 1):
        candidates: list[Path] = []
        if d.is_dir():
            candidates.extend(sorted(d.glob(f"scene_{i:02d}.*")))
        candidates.extend(sorted(VISUALS_DIR.glob(f"short_{sid}_scene_{i:02d}.*")))
        valid = [p for p in candidates if p.suffix.lower() in IMAGE_EXTS and p.stat().st_size > 1024]
        if not valid:
            raise FileNotFoundError(f"Scene {i} image missing for short {sid} - run 02b_fetch_visuals.py first.")
        imgs.append(valid[0])
    return imgs


def process_short(
    ffmpeg: str,
    ffprobe: str,
    script: dict[str, Any],
    plan_entry: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    sid = str(script["id"])
    audio = find_audio(sid)
    if not audio:
        raise FileNotFoundError(f"Audio not found for short {sid} in {AUDIO_DIR}")
    full_dur = probe_duration(ffprobe, a
