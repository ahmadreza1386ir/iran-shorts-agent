#!/usr/bin/env python3
"""
Verify rendered Shorts against the spec:
1080x1920, 30 fps, H.264 yuv420p, AAC 48 kHz, video/audio duration == narration duration.

Usage:  py verify_outputs.py [--tolerance 0.06]
Exit code 0 only if every checked file passes.
"""

from __future__ import annotations

import argparse
import sys
from fractions import Fraction

from common import AUDIO_DIR, FPS, HEIGHT, OUTPUT_DIR, WIDTH, ToolError, find_tool, load_scripts, probe_duration, probe_json, setup_logging

log = setup_logging("verify")


def check(ffprobe: str, sid: str, tol: float) -> list[str]:
    out = OUTPUT_DIR / f"short_{sid}.mp4"
    if not out.is_file():
        return [f"missing file {out.name}"]
    info = probe_json(ffprobe, out)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    errs: list[str] = []
    if not v or not a:
        return ["missing video or audio stream"]
    if (v.get("width"), v.get("height")) != (WIDTH, HEIGHT):
        errs.append(f"resolution {v.get('width')}x{v.get('height')} != {WIDTH}x{HEIGHT}")
    if v.get("codec_name") != "h264":
        errs.append(f"video codec {v.get('codec_name')} != h264")
    if v.get("pix_fmt") != "yuv420p":
        errs.append(f"pix_fmt {v.get('pix_fmt')} != yuv420p")
    if v.get("color_range") not in (None, "tv", "unknown"):
        errs.append(f"color_range {v.get('color_range')} (expected tv/limited)")
    try:
        fps = Fraction(v.get("avg_frame_rate", "0/1"))
        if abs(float(fps) - FPS) > 0.01:
            errs.append(f"fps {float(fps):.3f} != {FPS}")
    except (ZeroDivisionError, ValueError):
        errs.append("unreadable frame rate")
    if a.get("codec_name") != "aac":
        errs.append(f"audio codec {a.get('codec_name')} != aac")
    if str(a.get("sample_rate")) != "48000":
        errs.append(f"sample rate {a.get('sample_rate')} != 48000")
    src = next((AUDIO_DIR / f"short_{sid}{e}" for e in (".mp3", ".wav", ".m4a", ".aac", ".ogg") if (AUDIO_DIR / f"short_{sid}{e}").is_file()), None)
    vdur = float(v.get("duration") or info["format"].get("duration", 0))
    adur = float(a.get("duration") or info["format"].get("duration", 0))
    if src:
        ndur = probe_duration(ffprobe, src)
        for label, d in (("video", vdur), ("audio", adur)):
            if abs(d - ndur) > tol:
                errs.append(f"{label} duration {d:.3f}s differs from narration {ndur:.3f}s by {abs(d - ndur) * 1000:.0f} ms")
    else:
        errs.append("narration audio not found for duration comparison")
    log.info("short %-3s %dx%d %s %s | video %.3fs audio %.3fs | %.1f MB", sid, v.get("width"), v.get("height"), v.get("codec_name"),
             v.get("pix_fmt"), vdur, adur, out.stat().st_size / 1048576)
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tolerance", type=float, default=0.06, help="max allowed duration difference in seconds")
    args = ap.parse_args()
    try:
        ffprobe = find_tool("ffprobe")
        scripts = load_scripts()
    except (ToolError, OSError, ValueError) as exc:
        log.error("%s", exc)
        return 1
    bad = 0
    for sc in scripts:
        errs = check(ffprobe, sc["id"], args.tolerance)
        if errs:
            bad += 1
            for e in errs:
                log.error("short %s: %s", sc["id"], e)
        else:
            log.info("short %s: PASS", sc["id"])
    log.info("%d/%d passed", len(scripts) - bad, len(scripts))
    leftovers = list(OUTPUT_DIR.glob("*.tmp"))
    if leftovers:
        log.warning("leftover temp files in output: %s", [p.name for p in leftovers])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())