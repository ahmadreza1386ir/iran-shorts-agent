#!/usr/bin/env python3
"""
Shared helpers for the iran-shorts-agent visual + assembly stages.

Stdlib only. Windows-safe: pathlib everywhere, UTF-8 console, argument-list
subprocess calls (never shell strings), explicit tool discovery.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
INPUT_JSON = BASE_DIR / "data" / "scripts_batch_1.json"
PLAN_JSON = BASE_DIR / "data" / "visual_plan.json"
AUDIO_DIR = BASE_DIR / "output" / "audio"
VISUALS_DIR = BASE_DIR / "assets" / "visuals"
MUSIC_DIR = BASE_DIR / "assets" / "music"
OUTPUT_DIR = BASE_DIR / "output" / "pending_approval"

WIDTH, HEIGHT, FPS = 1080, 1920, 30
# Stills are stored at 2x so zoom/pan has headroom and stays smooth.
CANVAS_W, CANVAS_H = WIDTH * 2, HEIGHT * 2

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def setup_logging(name: str) -> logging.Logger:
    # Persian text in log lines must never crash a cp1252 / cp437 console.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    return logging.getLogger(name)


# --------------------------------------------------------------------------
# Tools / subprocess
# --------------------------------------------------------------------------
class ToolError(RuntimeError):
    pass


def find_tool(name: str) -> str:
    """Locate ffmpeg/ffprobe. Honors FFMPEG_BIN / FFPROBE_BIN overrides."""
    override = os.environ.get(f"{name.upper()}_BIN")
    if override and Path(override).is_file():
        return override
    found = shutil.which(name)
    if not found:
        raise ToolError(
            f"'{name}' not found on PATH. Install FFmpeg (e.g. `winget install Gyan.FFmpeg`) "
            f"or set the {name.upper()}_BIN environment variable to the full path."
        )
    return found


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command with a list of args (no shell), decode output as UTF-8 safely."""
    res = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and res.returncode != 0:
        tail = "\n".join((res.stderr or "").strip().splitlines()[-14:])
        raise ToolError(f"{Path(str(cmd[0])).name} failed (exit {res.returncode}):\n{tail}")
    return res


def probe_json(ffprobe: str, path: Path) -> dict[str, Any]:
    res = run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)])
    return json.loads(res.stdout or "{}")


def probe_duration(ffprobe: str, path: Path) -> float:
    info = probe_json(ffprobe, path)
    dur = float(info.get("format", {}).get("duration", 0) or 0)
    if dur <= 0.1:
        raise ToolError(f"Invalid duration ({dur}) for {path.name}")
    return dur


# --------------------------------------------------------------------------
# SRT
# --------------------------------------------------------------------------
_TIME_RE = re.compile(
    r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{1,3})"
)


def _decode_text(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "cp1256"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_srt(path: Path) -> list[tuple[float, float, str]]:
    text = _decode_text(path.read_bytes()).replace("\r\n", "\n").replace("\r", "\n")
    cues: list[tuple[float, float, str]] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.split("\n")
        for i, line in enumerate(lines):
            m = _TIME_RE.search(line)
            if not m:
                continue
            g = m.groups()
            start = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(f"{g[3]:0<3}") / 1000
            end = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(f"{g[7]:0<3}") / 1000
            body = "\n".join(x.strip() for x in lines[i + 1 :] if x.strip())
            if body and end > start:
                cues.append((start, end, body))
            break
    cues.sort(key=lambda c: c[0])
    return cues


# --------------------------------------------------------------------------
# Scripts + visual plan
# --------------------------------------------------------------------------
def safe_script_id(value: Any) -> str | None:
    if value is None or isinstance(value, (bool, dict, list)):
        return None
    s = str(value).strip()
    return s if s and re.fullmatch(r"[A-Za-z0-9_-]+", s) else None


def load_scripts(path: Path = INPUT_JSON) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"Input JSON not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, list):
        raise ValueError("scripts JSON must be a JSON array.")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        sid = safe_script_id(item.get("id"))
        if not sid or sid in seen:
            continue
        raw_scenes = item.get("visual_scenes")
        if isinstance(raw_scenes, str):
            raw_scenes = [raw_scenes]
        scenes: list[str] = []
        for s in raw_scenes if isinstance(raw_scenes, list) else []:
            if isinstance(s, str):
                scenes.append(s.strip())
            elif isinstance(s, dict):
                scenes.append(
                    next(
                        (str(s[k]).strip() for k in ("description", "visual", "prompt", "scene", "text") if s.get(k)),
                        "",
                    )
                )
            else:
                scenes.append("")
        out.append(
            {
                "id": sid,
                "category": str(item.get("category") or "").strip(),
                "scenes": scenes,
                "narration": str(item.get("narration_tts") or item.get("narration_display") or ""),
                "narration_alt": str(item.get("narration_display") or ""),
            }
        )
        seen.add(sid)
    return out


def load_plan(path: Path = PLAN_JSON) -> dict[str, dict[str, Any]]:
    """visual_plan.json -> {short_id: {"style": str, "scenes": [scene_plan, ...]}}"""
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    shorts = data.get("shorts", data) if isinstance(data, dict) else data
    plan: dict[str, dict[str, Any]] = {}
    for entry in shorts if isinstance(shorts, list) else []:
        sid = safe_script_id(entry.get("id"))
        if sid:
            plan[sid] = entry
    return plan


def clean_stale_part_files(root: Path) -> int:
    """Remove leftover *.part temp files from older fetcher versions."""
    n = 0
    if root.is_dir():
        for p in root.rglob("*.part"):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    return n