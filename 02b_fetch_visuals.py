#!/usr/bin/env python3
"""
Phase 2B: Scene visuals via Hormouz API with multi-model fallback chain
(black-forest-labs/flux-schnell -> prunaai/z-image-turbo -> black-forest-labs/flux-2-klein-4b),
automatic visual guard for corner/text/artifact defects, duplicate-aware retry,
and Wikimedia Commons, Openverse & cards fallback.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import io
import json
import os
import re
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common import (
    CANVAS_H,
    CANVAS_W,
    VISUALS_DIR,
    clean_stale_part_files,
    load_plan,
    load_scripts,
    safe_script_id,
    setup_logging,
)
from fallback_art import render_card

try:
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageStat
except ImportError:
    print("Pillow is required:  py -m pip install pillow", file=sys.stderr)
    sys.exit(2)

log = setup_logging("visuals")

CONTACT = os.environ.get("SHORTS_CONTACT", "").strip()
USER_AGENT = (
    f"IranShortsAgent/6.14 ({CONTACT or 'https://github.com/openverse-client'}) "
    f"python-urllib/{sys.version_info.major}.{sys.version_info.minor}"
)


def _env_first(names: tuple[str, ...], default: str = "") -> str:
    for name in names:
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    return default


def _normalize_hormouz_root(raw_base: str) -> str:
    root = (raw_base or "https://api.hormouz.net/v1").strip().rstrip("/")
    if not (root.startswith("https://") or root.startswith("http://")):
        raise ValueError("Invalid Hormouz base URL scheme.")
    for suffix in ("/chat/completions", "/images/generations", "/predictions"):
        if root.endswith(suffix):
            root = root[: -len(suffix)]
    if not root.endswith("/v1"):
        root = f"{root}/v1"
    return root


HORMOUZ_ROOT = _normalize_hormouz_root(
    _env_first(
        ("HORMOUZ_IMAGE_BASE_URL", "HORMOUZ_BASE_URL", "GAPGPT_BASE_URL"),
        "https://api.hormouz.net/v1",
    )
)

DEFAULT_IMAGE_CHAIN = [
    "black-forest-labs/flux-schnell",
    "prunaai/z-image-turbo",
    "black-forest-labs/flux-2-klein-4b",
]


def get_hormouz_image_models() -> list[str]:
    raw = _env_first(("HORMOUZ_IMAGE_MODELS", "HORMOUZ_IMAGE_MODEL", "GAPGPT_IMAGE_MODEL"), "")
    if not raw:
        return list(DEFAULT_IMAGE_CHAIN)
    models = [m.strip() for m in raw.split(",") if m.strip()]
    for fb in DEFAULT_IMAGE_CHAIN:
        if fb not in models:
            models.append(fb)
    return models


HORMOUZ_IMAGE_MODELS = get_hormouz_image_models()
HORMOUZ_IMAGE_SIZE = _env_first(("HORMOUZ_IMAGE_SIZE", "GAPGPT_IMAGE_SIZE"), "9:16")
HORMOUZ_MAX_DUP_RETRIES = 2
HORMOUZ_GUARD_RETRIES = int(_env_first(("HORMOUZ_GUARD_RETRIES",), "1") or "1")
VISUAL_GUARD_ENABLED = _env_first(("VISUAL_GUARD_ENABLED",), "1").lower() not in ("0", "false", "no")

STATE_FILE = VISUALS_DIR / "provider_state.json"
USED_HASHES_FILE = VISUALS_DIR / "used_hashes.json"
STATE_TTL = 12 * 3600
MAX_BYTES = 30 * 1024 * 1024
MIN_LONG_SIDE, MIN_SHORT_SIDE = 960, 640
MIN_SHARPNESS, MIN_STDDEV = 6.0, 12.0
DHASH_DUP_DISTANCE = 10
MAX_CANDIDATES_PER_SCENE = 8

SOURCE_INFO = {
    "hormouz": ("generated-unverified", 0.0, 0.0),
    "commons": ("verified-text", 0.0, 0.0),
    "openverse": ("verified-text", 0.0, 0.0),
    "card": ("procedural", 0.0, 0.0),
}


class FetchError(Exception):
    def __init__(self, msg: str, status: int | None = None, fatal: bool = False):
        super().__init__(msg)
        self.status, self.fatal = status, fatal


@dataclass
class Candidate:
    source: str
    url: str
    title: str
    blob: str
    license: str = ""
    author: str = ""
    page: str = ""
    score: float = 0.0
    size: tuple[int, int] = (0, 0)
    extra: dict[str, Any] = field(default_factory=dict)


def get_hormouz_key() -> str:
    return _env_first(("HORMOUZ_API_KEY", "GAPGPT_API_KEY"), "")


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def load_global_hashes() -> list[int]:
    if not USED_HASHES_FILE.is_file():
        return []
    try:
        raw = json.loads(USED_HASHES_FILE.read_text(encoding="utf-8-sig"))
        if isinstance(raw, list):
            return [int(x) for x in raw]
    except (OSError, ValueError):
        pass
    return []


def save_global_hashes(hashes: list[int]) -> None:
    try:
        unique_ordered = list(dict.fromkeys(int(h) for h in hashes))
        _atomic_write_bytes(
            USED_HASHES_FILE,
            (json.dumps(unique_ordered, indent=2) + "\n").encode("utf-8"),
        )
    except OSError as exc:
        log.warning("Could not save used_hashes.json: %s", exc)


class Net:
    def __init__(self, offline: bool = False, reset: bool = False):
        self.offline = offline
        self.disabled: dict[str, str] = {}
        self._announced: set[str] = set()
        self._last_openverse_ts: float = 0.0
        if reset:
            STATE_FILE.unlink(missing_ok=True)
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8-sig")) if STATE_FILE.is_file() else {}
            if isinstance(state, dict):
                for prov, v in state.items():
                    if isinstance(v, dict) and time.time() - float(v.get("ts", 0)) < STATE_TTL:
                        self.disabled[prov] = str(v.get("reason", "disabled"))
        except (OSError, ValueError):
            pass

    def is_disabled(self, provider: str) -> bool:
        if provider in self.disabled and provider not in self._announced:
            self._announced.add(provider)
            log.warning("Provider '%s' is OFF: %s", provider, self.disabled[provider])
        return provider in self.disabled

    def request(
        self,
        provider: str,
        url: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 60,
        retries: int = 1,
        auth_fatal: bool = True,
    ) -> bytes:
        url = (url or "").strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise FetchError("disallowed URL scheme", fatal=True)
        if self.offline:
            raise FetchError("offline mode", fatal=True)
        if self.is_disabled(provider):
            raise FetchError(f"{provider} disabled: {self.disabled[provider]}", fatal=True)

        if provider == "openverse" and "api.openverse.org" in url:
            elapsed = time.time() - self._last_openverse_ts
            if elapsed < 3.2:
                time.sleep(3.2 - elapsed)
            self._last_openverse_ts = time.time()

        body_bytes: bytes | None = None
        req_headers = {
            "User-Agent": USER_AGENT,
            "Api-User-Agent": USER_AGENT,
            "Accept": "*/*",
            **(headers or {}),
        }
        if payload is not None:
            body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(url, data=body_bytes, headers=req_headers, method=method)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = resp.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise FetchError("response too large")
                return data
            except urllib.error.HTTPError as exc:
                try:
                    err_txt = exc.read(300).decode("utf-8", "replace").replace("\n", " ").strip()
                except Exception:
                    err_txt = ""
                host = parsed.netloc
                detail = f"HTTP {exc.code} from {host}" + (f" - {err_txt}" if err_txt else "")
                last = FetchError(detail, status=exc.code)
                if exc.code in (401, 402, 403) and auth_fatal:
                    if provider != "hormouz":
                        self.disabled[provider] = detail
                    raise FetchError(detail, exc.code, fatal=False) from exc
                if exc.code in (429, 503) and attempt < retries:
                    time.sleep(6 * (attempt + 1))
                    continue
                if exc.code < 500:
                    raise last from exc
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
                last = FetchError(f"network error: {getattr(exc, 'reason', exc)}")
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
        assert last is not None
        raise last


def has_term(blob: str, term: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(term.lower()), blob) is not None


def strip_html(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s or ""))


def has_word(blob: str, term: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])", blob) is not None


# Kept balanced so cultural/story motifs (jars, rooms, buildings) are not over-rejected
GLOBAL_REJECT = [
    "seal", "seals", "coat of arms", "emblem", "insignia", "logo", "flag", "banner",
    "stamp", "coin", "banknote", "currency", "icon", "pictogram", "diagram", "map",
    "chart", "poster", "certificate", "label", "packaging", "vector", "svg", "clipart",
    "screenshot", "scan", "infographic", "official",
    "telephone", "landline", "phone", "selfhelp", "promotion", "furniture",
    "appliance", "widget", "merchandise", "solar", "parade", "promenade",
    "port", "harbor", "pier", "potable", "jet d'eau", "fountain",
]

PREFERRED_BOOST = [
    "water", "river", "stream", "pond", "lake", "nature", "landscape", "sunset",
    "dusk", "dawn", "stone", "rock", "ripples", "reflection", "lantern", "light",
]


def relevance(blob: str, rules: dict[str, Any], boost_blob: str | None = None) -> tuple[bool, float, str]:
    full = boost_blob or blob
    for group in rules.get("require", []):
        if not any(has_term(blob, t) for t in group):
            return False, 0.0, f"missing required term from {group}"
    allowed = {t.lower() for t in rules.get("allow", [])}
    for t in [*rules.get("reject", []), *(g for g in GLOBAL_REJECT if g not in allowed)]:
        if has_word(full, t):
            return False, 0.0, f"contains rejected term '{t}'"
    score = float(sum(1 for t in rules.get("boost", []) if has_term(full, t)))
    score += 0.5 * sum(1 for t in PREFERRED_BOOST if has_term(full, t))
    return True, score, "ok"


_LICENSE_OK = re.compile(
    r"^(cc0|cc[ -]by(?![ -]n[cd])|public domain|pd[ -]|attribution|pdm|by(?![ -]n[cd]))",
    re.I,
)


def license_ok(name: str) -> bool:
    n = (name or "").strip()
    if not n or re.search(r"\bn[cd]\b|-n[cd]", n, re.I):
        return False
    return bool(_LICENSE_OK.match(n))


def aspect_ok(w: int, h: int) -> bool:
    if w <= 0 or h <= 0:
        return True
    return max(w / h, h / w) <= 2.2


def commons_candidates(net: Net, query: str, rules: dict[str, Any]) -> list[Candidate]:
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrnamespace": "6",
        "gsrsearch": f"{query} filetype:bitmap",
        "gsrlimit": "20",
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": "1920",
        "iiextmetadatafilter": "ImageDescription|ObjectName|Categories|LicenseShortName|Artist",
    }
    raw = net.request(
        "commons",
        "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params),
        timeout=25,
    )
    return parse_commons(json.loads(raw.decode("utf-8", "replace")), rules)


def parse_commons(data: dict[str, Any], rules: dict[str, Any]) -> list[Candidate]:
    out: list[Candidate] = []
    for page in (data.get("query", {}).get("pages", {}) or {}).values():
        info = (page.get("imageinfo") or [{}])[0]
        if info.get("mime") != "image/jpeg":
            continue
        meta = {k: strip_html(v.get("value", "")) for k, v in (info.get("extmetadata") or {}).items()}
        lic = meta.get("LicenseShortName", "")
        title = re.sub(r"^File:", "", page.get("title", ""))
        primary = " ".join([title, meta.get("ObjectName", ""), meta.get("ImageDescription", "")]).lower()
        blob = (primary + " " + meta.get("Categories", "").replace("|", " ")).lower()
        ok, score, _ = relevance(primary, rules, blob)
        if not ok or not license_ok(lic):
            continue
        w, h = (
            int(info.get("thumbwidth") or info.get("width") or 0),
            int(info.get("thumbheight") or info.get("height") or 0),
        )
        if not aspect_ok(w, h):
            continue
        score += 1.3 if h >= w else 0.0
        score += 0.5 if min(w, h) >= 1000 else 0.0
        out.append(
            Candidate(
                "commons",
                info.get("thumburl") or info.get("url", ""),
                title,
                blob,
                lic,
                meta.get("Artist", ""),
                info.get("descriptionurl", ""),
                score,
                (w, h),
            )
        )
    return out


def openverse_candidates(net: Net, query: str, rules: dict[str, Any]) -> list[Candidate]:
    params = {
        "q": query,
        "license_type": "commercial",
        "page_size": "20",
        "extension": "jpg",
    }
    url = "https://api.openverse.org/v1/images/?" + urllib.parse.urlencode(params)
    raw = net.request("openverse", url, headers={"Accept": "application/json"}, timeout=25, auth_fatal=False)
    data = json.loads(raw.decode("utf-8", "replace"))
    out: list[Candidate] = []
    for item in data.get("results") or []:
        img_url = (item.get("url") or "").strip()
        if not img_url:
            continue
        lic = (item.get("license") or "").strip()
        if not license_ok(lic):
            continue
        title = (item.get("title") or "openverse image").strip()
        tags = " ".join(t.get("name", "") for t in (item.get("tags") or []) if isinstance(t, dict))
        primary = f"{title} {tags}".lower()
        ok, score, _ = relevance(primary, rules, primary)
        if not ok:
            continue
        w, h = int(item.get("width") or 0), int(item.get("height") or 0)
        if w and h and (max(w, h) < MIN_LONG_SIDE or min(w, h) < MIN_SHORT_SIDE or not aspect_ok(w, h)):
            continue
        score += 1.3 if h >= w else 0.0
        score += 0.5 if min(w, h) >= 1000 else 0.0
        out.append(
            Candidate(
                "openverse",
                img_url,
                title,
                primary,
                lic.upper(),
                (item.get("creator") or "").strip(),
                (item.get("foreign_landing_url") or "").strip(),
                score,
                (w, h),
            )
        )
    return out


def dhash(img: Image.Image) -> int:
    g = img.convert("L").resize((9, 8), Image.LANCZOS)
    px = list(g.get_flattened_data()) if hasattr(g, "get_flattened_data") else list(g.getdata())
    bits = 0
    for r in range(8):
        for c in range(8):
            bits = (bits << 1) | (px[r * 9 + c] > px[r * 9 + c + 1])
    return bits


def sharpness(img: Image.Image) -> float:
    g = img.convert("L")
    g.thumbnail((640, 640))
    lap = g.filter(ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128))
    return float(ImageStat.Stat(lap).var[0])


def visual_guard_check(img: Image.Image) -> tuple[bool, str]:
    """
    Lightweight visual guard to flag likely corner watermarks/text signatures
    or isolated sharp symbol clusters in generated storybook frames.
    """
    if not VISUAL_GUARD_ENABLED:
        return True, "guard_disabled"

    w, h = img.size
    gray = img.convert("L")
    edges = gray.filter(ImageFilter.FIND_EDGES)

    br_box = (int(w * 0.72), int(h * 0.84), w, h)
    br_edges = edges.crop(br_box)
    br_px = list(br_edges.getdata())
    if br_px:
        strong_br = sum(1 for v in br_px if v > 68) / len(br_px)
        br_std = ImageStat.Stat(gray.crop(br_box)).stddev[0]
        if strong_br > 0.14 and br_std > 26.0:
            return False, f"corner watermark/text artifact (edge_ratio={strong_br:.2f}, std={br_std:.1f})"

    bl_box = (0, int(h * 0.84), int(w * 0.28), h)
    bl_edges = edges.crop(bl_box)
    bl_px = list(bl_edges.getdata())
    if bl_px:
        strong_bl = sum(1 for v in bl_px if v > 68) / len(bl_px)
        bl_std = ImageStat.Stat(gray.crop(bl_box)).stddev[0]
        if strong_bl > 0.14 and bl_std > 26.0:
            return False, f"bottom-left artifact (edge_ratio={strong_bl:.2f}, std={bl_std:.1f})"

    low_mid_box = (int(w * 0.35), int(h * 0.68), int(w * 0.65), int(h * 0.88))
    lm_patch = gray.crop(low_mid_box).resize((96, 96), Image.BILINEAR)
    lm_edges = lm_patch.filter(ImageFilter.FIND_EDGES)
    lm_px = list(lm_edges.getdata())
    if lm_px:
        strong_lm = sum(1 for v in lm_px if v > 85) / len(lm_px)
        if 0.045 < strong_lm < 0.18:
            small_grid = lm_edges.resize((6, 6), Image.BOX)
            grid_vals = list(small_grid.getdata())
            max_cell = max(grid_vals) if grid_vals else 0
            avg_cell = sum(grid_vals) / max(1, len(grid_vals))
            if avg_cell > 0 and (max_cell / avg_cell) > 3.6 and max_cell > 55:
                return False, f"isolated symbol/cross artifact on floor (spike={max_cell/avg_cell:.1f}x)"

    return True, "ok"


MIN_GREY_LEVELS = 150
MIN_COLORS = 1200


def assess(img: Image.Image, forbidden_hashes: list[int], photo: bool = False) -> tuple[bool, str, dict[str, Any]]:
    w, h = img.size
    if not aspect_ok(w, h):
        return False, f"extreme aspect ratio ({w}x{h})", {"w": w, "h": h}
    g = img.convert("L")
    stat = ImageStat.Stat(g)
    m = {
        "w": w,
        "h": h,
        "sharp": round(sharpness(img), 1),
        "std": round(stat.stddev[0], 1),
        "mean": round(stat.mean[0], 1),
    }
    if max(w, h) < MIN_LONG_SIDE or min(w, h) < MIN_SHORT_SIDE:
        return False, f"too small ({w}x{h})", m
    if m["std"] < MIN_STDDEV or m["mean"] < 15 or m["mean"] > 242:
        return False, f"flat/blank (std={m['std']}, mean={m['mean']})", m
    if m["sharp"] < MIN_SHARPNESS:
        return False, f"blurry/low detail (sharp={m['sharp']})", m
    if photo:
        small = img.convert("RGB").resize((128, 128), Image.BILINEAR)
        colors = small.getcolors(maxcolors=16384) or []
        m["colors"] = len(colors)
        if len(colors) < MIN_COLORS:
            hist = small.convert("L").histogram()
            levels = sum(1 for c in hist if c >= 2)
            flat_share = sum(sorted(hist)[-4:]) / (128 * 128)
            chroma = ImageStat.Stat(small).stddev
            is_grey = max(abs(chroma[0] - chroma[1]), abs(chroma[1] - chroma[2])) < 2.0
            m["grey_levels"] = levels
            if not (is_grey and levels >= MIN_GREY_LEVELS and flat_share < 0.7):
                return False, f"not photographic (only {len(colors)} distinct colours)", m
    else:
        guard_ok, guard_reason = visual_guard_check(img)
        if not guard_ok:
            return  for t in [*rules.get("reject", []), *(g for g in GLOBAL_REJECT if g not in allowed)]:
        if has_word(full, t):
            return False, 0.0, f"contains rejected term '{t}'"
    score = float(sum(1 for t in rules.get("boost", []) if has_term(full, t)))
    score += 0.5 * sum(1 for t in PREFERRED_BOOST if has_term(full, t))
    return True, score, "ok"


_LICENSE_OK = re.compile(
    r"^(cc0|cc[ -]by(?![ -]n[cd])|public domain|pd[ -]|attribution|pdm|by(?![ -]n[cd]))",
    re.I,
)


def license_ok(name: str) -> bool:
    n = (name or "").strip()
    if not n or re.search(r"\bn[cd]\b|-n[cd]", n, re.I):
        return False
    return bool(_LICENSE_OK.match(n))


def aspect_ok(w: int, h: int) -> bool:
    if w <= 0 or h <= 0:
        return True
    return max(w / h, h / w) <= 2.2


def commons_candidates(net: Net, query: str, rules: dict[str, Any]) -> list[Candidate]:
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrnamespace": "6",
        "gsrsearch": f"{query} filetype:bitmap",
        "gsrlimit": "20",
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": "1920",
        "iiextmetadatafilter": "ImageDescription|ObjectName|Categories|LicenseShortName|Artist",
    }
    raw = net.request(
        "commons",
        "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params),
        timeout=25,
    )
    return parse_commons(json.loads(raw.decode("utf-8", "replace")), rules)


def parse_commons(data: dict[str, Any], rules: dict[str, Any]) -> list[Candidate]:
    out: list[Candidate] = []
    for page in (data.get("query", {}).get("pages", {}) or {}).values():
        info = (page.get("imageinfo") or [{}])[0]
        if info.get("mime") != "image/jpeg":
            continue
        meta = {k: strip_html(v.get("value", "")) for k, v in (info.get("extmetadata") or {}).items()}
        lic = meta.get("LicenseShortName", "")
        title = re.sub(r"^File:", "", page.get("title", ""))
        primary = " ".join([title, meta.get("ObjectName", ""), meta.get("ImageDescription", "")]).lower()
        blob = (primary + " " + meta.get("Categories", "").replace("|", " ")).lower()
        ok, score, _ = relevance(primary, rules, blob)
        if not ok or not license_ok(lic):
            continue
        w, h = (
            int(info.get("thumbwidth") or info.get("width") or 0),
            int(info.get("thumbheight") or info.get("height") or 0),
        )
        if not aspect_ok(w, h):
            continue
        score += 1.3 if h >= w else 0.0
        score += 0.5 if min(w, h) >= 1000 else 0.0
        out.append(
            Candidate(
                "commons",
                info.get("thumburl") or info.get("url", ""),
                title,
                blob,
                lic,
                meta.get("Artist", ""),
                info.get("descriptionurl", ""),
                score,
                (w, h),
            )
        )
    return out


def openverse_candidates(net: Net, query: str, rules: dict[str, Any]) -> list[Candidate]:
    params = {
        "q": query,
        "license_type": "commercial",
        "page_size": "20",
        "extension": "jpg",
    }
    url = "https://api.openverse.org/v1/images/?" + urllib.parse.urlencode(params)
    raw = net.request("openverse", url, headers={"Accept": "application/json"}, timeout=25, auth_fatal=False)
    data = json.loads(raw.decode("utf-8", "replace"))
    out: list[Candidate] = []
    for item in data.get("results") or []:
        img_url = (item.get("url") or "").strip()
        if not img_url:
            continue
        lic = (item.get("license") or "").strip()
        if not license_ok(lic):
            continue
        title = (item.get("title") or "openverse image").strip()
        tags = " ".join(t.get("name", "") for t in (item.get("tags") or []) if isinstance(t, dict))
        primary = f"{title} {tags}".lower()
        ok, score, _ = relevance(primary, rules, primary)
        if not ok:
            continue
        w, h = int(item.get("width") or 0), int(item.get("height") or 0)
        if w and h and (max(w, h) < MIN_LONG_SIDE or min(w, h) < MIN_SHORT_SIDE or not aspect_ok(w, h)):
            continue
        score += 1.3 if h >= w else 0.0
        score += 0.5 if min(w, h) >= 1000 else 0.0
        out.append(
            Candidate(
                "openverse",
                img_url,
                title,
                primary,
                lic.upper(),
                (item.get("creator") or "").strip(),
                (item.get("foreign_landing_url") or "").strip(),
                score,
                (w, h),
            )
        )
    return out


def dhash(img: Image.Image) -> int:
    g = img.convert("L").resize((9, 8), Image.LANCZOS)
    px = list(g.get_flattened_data()) if hasattr(g, "get_flattened_data") else list(g.getdata())
    bits = 0
    for r in range(8):
        for c in range(8):
            bits = (bits << 1) | (px[r * 9 + c] > px[r * 9 + c + 1])
    return bits


def sharpness(img: Image.Image) -> float:
    g = img.convert("L")
    g.thumbnail((640, 640))
    lap = g.filter(ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128))
    return float(ImageStat.Stat(lap).var[0])


def visual_guard_check(img: Image.Image) -> tuple[bool, str]:
    """
    Lightweight visual guard to flag likely corner watermarks/text signatures
    or isolated sharp symbol clusters in generated storybook frames.
    """
    if not VISUAL_GUARD_ENABLED:
        return True, "guard_disabled"

    w, h = img.size
    gray = img.convert("L")
    edges = gray.filter(ImageFilter.FIND_EDGES)

    # Check bottom-right corner (where signatures like @/26 or @IA/16:10 often appear)
    br_box = (int(w * 0.72), int(h * 0.84), w, h)
    br_edges = edges.crop(br_box)
    br_px = list(br_edges.getdata())
    if br_px:
        strong_br = sum(1 for v in br_px if v > 68) / len(br_px)
        br_std = ImageStat.Stat(gray.crop(br_box)).stddev[0]
        if strong_br > 0.14 and br_std > 26.0:
            return False, f"corner watermark/text artifact (edge_ratio={strong_br:.2f}, std={br_std:.1f})"

    # Check bottom-left corner
    bl_box = (0, int(h * 0.84), int(w * 0.28), h)
    bl_edges = edges.crop(bl_box)
    bl_px = list(bl_edges.getdata())
    if bl_px:
        strong_bl = sum(1 for v in bl_px if v > 68) / len(bl_px)
        bl_std = ImageStat.Stat(gray.crop(bl_box)).stddev[0]
        if strong_bl > 0.14 and bl_std > 26.0:
            return False, f"bottom-left artifact (edge_ratio={strong_bl:.2f}, std={bl_std:.1f})"

    # Check lower-center floor patch for isolated cross/symbol-like high-contrast speckles
    low_mid_box = (int(w * 0.35), int(h * 0.68), int(w * 0.65), int(h * 0.88))
    lm_patch = gray.crop(low_mid_box).resize((96, 96), Image.BILINEAR)
    lm_edges = lm_patch.filter(ImageFilter.FIND_EDGES)
    lm_px = list(lm_edges.getdata())
    if lm_px:
        strong_lm = sum(1 for v in lm_px if v > 85) / len(lm_px)
        if 0.045 < strong_lm < 0.18:
            # Verify whether high contrast is localized to a small symbol rather than broad texture
            small_grid = lm_edges.resize((6, 6), Image.BOX)
            grid_vals = list(small_grid.getdata())
            max_cell = max(grid_vals) if grid_vals else 0
            avg_cell = sum(grid_vals) / max(1, len(grid_vals))
            if avg_cell > 0 and (max_cell / avg_cell) > 3.6 and max_cell > 55:
                return False, f"isolated symbol/cross artifact on floor (spike={max_cell/avg_cell:.1f}x)"

    return True, "ok"


MIN_GREY_LEVELS = 150
MIN_COLORS = 1200


def assess(img: Image.Image, forbidden_hashes: list[int], photo: bool = False) -> tuple[bool, str, dict[str, Any]]:
    w, h = img.size
    if not aspect_ok(w, h):
        return False, f"extreme aspect ratio ({w}x{h})", {"w": w, "h": h}
    g = img.convert("L")
    stat = ImageStat.Stat(g)
    m = {
        "w": w,
        "h": h,
        "sharp": round(sharpness(img), 1),
        "std": round(stat.stddev[0], 1),
        "mean": round(stat.mean[0], 1),
    }
    if max(w, h) < MIN_LONG_SIDE or min(w, h) < MIN_SHORT_SIDE:
        return False, f"too small ({w}x{h})", m
    if m["std"] < MIN_STDDEV or m["mean"] < 15 or m["mean"] > 242:
        return False, f"flat/blank (std={m['std']}, mean={m['mean']})", m
    if m["sharp"] < MIN_SHARPNESS:
        return False, f"blurry/low detail (sharp={m['sharp']})", m
    if photo:
        small = img.convert("RGB").resize((128, 128), Image.BILINEAR)
        colors = small.getcolors(maxcolors=16384) or []
        m["colors"] = len(colors)
        if len(colors) < MIN_COLORS:
            hist = small.convert("L").histogram()
            levels = sum(1 for c in hist if c >= 2)
            flat_share = sum(sorted(hist)[-4:]) / (128 * 128)
            chroma = ImageStat.Stat(small).stddev
            is_grey = max(abs(chroma[0] - chroma[1]), abs(chroma[1] - chroma[2])) < 2.0
            m["grey_levels"] = levels
            if not (is_grey and levels >= MIN_GREY_LEVELS and flat_share < 0.7):
                return False, f"not photographic (only {len(colors)} distinct colours)", m
    else:
        guard_ok, guard_reason = visual_guard_check(img)
        if not guard_ok:
            return False, f"guard_rejected: {guard_reason}", m

    dh = dhash(img)
    m["dhash"] = dh
    for prev in forbidden_hashes:
        if bin(dh ^ prev).count("1") <= DHASH_DUP_DISTANCE:
            return False, "duplicate or too similar to an already-used visual", m
    return True, "ok", m


def frame_to_canvas(img: Image.Image, mode: str = "auto") -> Image.Image:
    img = ImageOps.exif_transpose(img).convert("RGB")
    fitted = ImageOps.fit(img, (CANVAS_W, CANVAS_H), Image.LANCZOS, centering=(0.5, 0.38))
    return ImageEnhance.Contrast(fitted).enhance(1.03)


def plan_hash(scene_plan: dict[str, Any], style: str, text: str) -> str:
    return hashlib.sha1(
        json.dumps([scene_plan, style, text], sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]


VARIATION_HINTS = [
    "wide storybook framing, rich painterly detail, clean edges without any text or marks",
    "intimate close-up storybook angle, soft atmospheric depth, zero corner marks or symbols",
    "dynamic diagonal composition, expressive hand-painted strokes, completely clean border",
]


def _extract_first_output_url_or_b64(data: dict[str, Any]) -> str:
    out = data.get("output")
    if isinstance(out, list) and out:
        return str(out[0]).strip()
    if isinstance(out, str) and out.strip():
        return out.strip()
    for key in ("data", "images", "result", "image", "url", "b64_json"):
        val = data.get(key)
        if isinstance(val, list) and val:
            first = val[0]
            if isinstance(first, dict):
                return str(first.get("url") or first.get("b64_json") or "").strip()
            return str(first).strip()
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _build_model_input(model: str, full_prompt: str) -> dict[str, Any]:
    aspect = HORMOUZ_IMAGE_SIZE or "9:16"
    if model == "prunaai/z-image-turbo":
        return {
            "prompt": full_prompt,
            "aspect_ratio": aspect,
            "num_inference_steps": 8,
            "guidance_scale": 3.5,
            "output_format": "jpg",
        }
    if model == "black-forest-labs/flux-2-klein-4b":
        return {
            "prompt": full_prompt,
            "aspect_ratio": aspect,
            "output_megapixels": "1",
            "output_format": "jpg",
            "output_quality": 95,
        }
    return {
        "prompt": full_prompt,
        "aspect_ratio": aspect,
        "output_format": "jpg",
    }


class SceneFetcher:
    def __init__(self, net: Net, args: argparse.Namespace, global_hashes: list[int]):
        self.net, self.args = net, args
        self.global_hashes = global_hashes

    def _accept_bytes(self, raw: bytes, source: str, forbidden: list[int]) -> tuple[Image.Image | None, str, dict[str, Any]]:
        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
        except Exception as exc:
            return None, f"undecodable image ({exc.__class__.__name__})", {}
        img = ImageOps.exif_transpose(img).convert("RGB")
        ok, why, metrics = assess(img, forbidden, photo=source in ("commons", "openverse"))
        return (img if ok else None), why, metrics

    def _download_hormouz_result(self, data: dict[str, Any], headers: dict[str, str]) -> bytes:
        assets = data.get("assets")
        if isinstance(assets, list) and assets and isinstance(assets[0], dict):
            asset_id = str(assets[0].get("id") or "").strip()
            if asset_id:
                asset_url = f"{HORMOUZ_ROOT}/assets/{urllib.parse.quote(asset_id)}"
                return self.net.request("hormouz", asset_url, method="GET", headers=headers, timeout=60, retries=1)

        ref = _extract_first_output_url_or_b64(data)
        if not ref:
            raise FetchError("prediction succeeded without assets or output")
        if ref.startswith("data:image/"):
            _, b64_part = ref.split(",", 1)
            return base64.b64decode(b64_part)
        if ref.startswith(("http://", "https://")):
            proxy_url = f"{HORMOUZ_ROOT}/files/proxy?url=" + urllib.parse.quote(ref, safe="")
            try:
                return self.net.request("hormouz", proxy_url, method="GET", headers=headers, timeout=60, retries=1)
            except FetchError:
                return self.net.request("hormouz", ref, method="GET", timeout=60, retries=1, auth_fatal=False)
        return base64.b64decode(ref)

    def _poll_prediction_until_ready(self, data: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        pred_id = str(data.get("id") or "").strip()
        poll_url = ""
        urls = data.get("urls")
        if isinstance(urls, dict) and urls.get("get"):
            poll_url = str(urls["get"]).strip()
        elif pred_id:
            poll_url = f"{HORMOUZ_ROOT}/predictions/{pred_id}"

        succeeded_at: float | None = None
        for _ in range(55):
            status = str(data.get("status") or "").lower()
            if status in ("failed", "canceled", "error"):
                raise FetchError(f"prediction {status}: {data.get('error') or ''}")

            assets = data.get("assets")
            has_assets = isinstance(assets, list) and len(assets) > 0
            out_ref = _extract_first_output_url_or_b64(data)

            if status == "succeeded" and has_assets:
                return data
            if status == "succeeded":
                if succeeded_at is None:
                    succeeded_at = time.monotonic()
                if (time.monotonic() - succeeded_at) > 20.0 and out_ref:
                    return data

            if not poll_url:
                if out_ref:
                    return data
                break

            time.sleep(2.0)
            raw_poll = self.net.request("hormouz", poll_url, method="GET", headers=headers, timeout=30, retries=1)
            data = json.loads(raw_poll.decode("utf-8", "replace"))

        if _extract_first_output_url_or_b64(data):
            return data
        raise FetchError("prediction timed out waiting for assets")

    def _call_generator_once(
        self, model: str, full_prompt: str, headers: dict[str, str], forbidden: list[int]
    ) -> tuple[Image.Image | None, dict[str, Any], str] | None:
        pred_url = f"{HORMOUZ_ROOT}/predictions"
        payload: dict[str, Any] = {
            "version": model,
            "model": model,
            "input": _build_model_input(model, full_prompt),
        }

        try:
            raw_resp = self.net.request(
                "hormouz",
                pred_url,
                method="POST",
                payload=payload,
                headers=headers,
                timeout=60,
                retries=1,
            )
            data = json.loads(raw_resp.decode("utf-8", "replace"))
            if isinstance(data, dict):
                data = self._poll_prediction_until_ready(data, headers)
            img_bytes = self._download_hormouz_result(data, headers)
            img, why, metrics = self._accept_bytes(img_bytes, "hormouz", forbidden)
            if img is None:
                reason_code = "dup" if "duplicate or too similar" in why else ("guard" if "guard_rejected" in why else "other")
                log.info("   x hormouz (%s) image rejected: %s", model, why)
                return (None, {}, reason_code)
            return (
                img,
                {
                    "source": "hormouz",
                    "model": model,
                    "title": f"{model}: {full_prompt[:60]}",
                    "license": "generated",
                    "prompt": full_prompt,
                    "metrics": metrics,
                },
                "ok",
            )
        except FetchError as exc:
            log.warning("   hormouz (%s) failed: %s", model, exc)
            return None

    def from_hormouz(self, prompt: str, style: str, forbidden: list[int]) -> tuple[Image.Image, dict[str, Any]] | None:
        api_key = get_hormouz_key()
        if not api_key or self.net.is_disabled("hormouz"):
            return None
        headers = {"Authorization": f"Bearer {api_key}"}

        for model in HORMOUZ_IMAGE_MODELS:
            max_tries = 1 + HORMOUZ_MAX_DUP_RETRIES + HORMOUZ_GUARD_RETRIES
            for attempt in range(max_tries):
                hint = (
                    ""
                    if attempt == 0
                    else f" Variation {attempt}: {VARIATION_HINTS[(attempt - 1) % len(VARIATION_HINTS)]}."
                )
                full_prompt = (
                    f"Cohesive hand-painted storybook illustration frame, expressive brushwork, "
                    f"unified warm-and-teal color palette. Scene: {prompt}. {style}.{hint} "
                    f"Vertical 9:16 composition, no text, no letters, no watermark, no signature, no symbols."
                )
                res = self._call_generator_once(model, full_prompt, headers, forbidden)
                if res is None:
                    break
                img, meta, status_code = res
                if img is not None:
                    return img, meta
                if status_code in ("dup", "guard") and attempt < max_tries - 1:
                    log.info(
                        "   guard/dup retry on hormouz (%s) (attempt %d/%d)...",
                        model,
                        attempt + 2,
                        max_tries,
                    )
                    continue
                break
        return None

    def _from_pool(
        self,
        source: str,
        rules: dict[str, Any],
        forbidden: list[int],
        search_fn: Any,
    ) -> tuple[Image.Image, dict[str, Any]] | None:
        pool: dict[str, Candidate] = {}
        queries = rules.get("queries", [])
        if source == "openverse" and len(queries) > 2:
            queries = queries[:2]
        for q in queries:
            try:
                for cand in search_fn(self.net, q, rules):
                    if cand.url and (cand.url not in pool or cand.score > pool[cand.url].score):
                        cand.extra["query"] = q
                        pool[cand.url] = cand
            except FetchError as exc:
                log.warning("   %s search '%s' failed: %s", source, q, exc)
                if exc.fatal:
                    return None
            if len(pool) >= 8:
                break
        ranked = sorted(pool.values(), key=lambda c: -c.score)[:MAX_CANDIDATES_PER_SCENE]
        log.info("   %s: %d relevant candidate(s) after text & aspect verification", source, len(ranked))
        for cand in ranked:
            try:
                raw = self.net.request(source, cand.url, timeout=45, retries=1, auth_fatal=False)
            except FetchError as exc:
                log.warning("   download failed (%s): %s", cand.title[:50], exc)
                continue
            img, why, metrics = self._accept_bytes(raw, source, forbidden)
            if img is None:
                log.info("   x %s -> %s", cand.title[:60], why)
                continue
            return img, {
                "source": source,
                "title": cand.title,
                "url": cand.url,
                "page": cand.page,
                "license": cand.license,
                "author": cand.author,
                "query": cand.extra.get("query"),
                "relevance_score": cand.score,
                "metrics": metrics,
            }
        return None

    def build(
        self,
        sid: str,
        idx: int,
        text: str,
        scene_plan: dict[str, Any],
        style: str,
        local_seen: list[int],
    ) -> tuple[Image.Image, dict[str, Any]]:
        has_gen = bool(get_hormouz_key())
        if self.net.offline:
            order = ["card"]
        elif not self.args.no_generator and has_gen:
            order = ["hormouz", "commons", "openverse", "card"]
        else:
            order = ["commons", "openverse", "card"]

        combined_forbidden = list(dict.fromkeys(self.global_hashes + local_seen))
        base_seed = int(hashlib.sha1(f"{sid}:{idx}".encode()).hexdigest()[:6], 16)

        for source in order:
            if source == "card":
                break
            log.info("  [%s] scene %02d trying %s", sid, idx, source)
            if source == "hormouz":
                prompt = scene_plan.get("prompt") or scene_plan.get("query") or text
                found = self.from_hormouz(prompt, style, combined_forbidden)
            else:
                rules = scene_plan.get("commons", {})
                if not rules.get("queries"):
                    continue
                fn = commons_candidates if source == "commons" else openverse_candidates
                found = self._from_pool(source, rules, combined_forbidden, fn)
            if found:
                img, meta = found
                meta["verification"] = SOURCE_INFO[meta["source"]][0]
                return img, meta

        card = scene_plan.get("card") or {
            "palette": "desert_day",
            "layers": [["sun", {}], ["dunes", {}]],
        }
        varied_seed = base_seed + len(self.global_hashes) * 37 + idx * 13
        return render_card(card, varied_seed), {
            "source": "card",
            "verification": "procedural",
            "card": card,
        }


def process_short(
    fetcher: SceneFetcher,
    script: dict[str, Any],
    plan_entry: dict[str, Any],
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    sid = script["id"]
    out_dir = VISUALS_DIR / f"short_{sid}"
    out_dir.mkdir(parents=True, exist_ok=True)
    removed = clean_stale_part_files(out_dir)
    if removed:
        log.info("[%s] removed %d stale .part file(s)", sid, removed)

    manifest_path = out_dir / "manifest.json"
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
    scenes_meta: dict[str, Any] = manifest.get("scenes", {})
    style = plan_entry.get("style", "")
    plan_scenes = plan_entry.get("scenes", [])
    local_seen: list[int] = []
    results: list[dict[str, Any]] = []
    log.info("-" * 72)
    log.info("[%s] %s | %d scene(s)", sid, script["category"], len(script["scenes"]))

    targeted_scene = args.scene is not None

    for i, text in enumerate(script["scenes"], start=1):
        key = str(i)
        sp = plan_scenes[i - 1] if i - 1 < len(plan_scenes) else {"query": text, "prompt": text}
        h = plan_hash(sp, style, text)
        target = out_dir / f"scene_{i:02d}.jpg"
        cached = scenes_meta.get(key)
        is_target = (not targeted_scene) or (args.scene == i)

        if not is_target:
            if cached and target.is_file():
                log.info("[%s] scene %02d untouched (%s) - kept", sid, i, cached.get("source"))
                if cached.get("dhash") is not None:
                    dh_val = int(cached["dhash"])
                    local_seen.append(dh_val)
                    if dh_val not in fetcher.global_hashes:
                        fetcher.global_hashes.append(dh_val)
                results.append({"sid": sid, "idx": i, **cached, "cached": True})
                continue

        should_force = bool(args.force and is_target)
        if (
            cached
            and cached.get("plan_hash") == h
            and target.is_file()
            and not should_force
            and (cached.get("source") != "card" or not args.retry_cards)
        ):
            log.info("[%s] scene %02d cached (%s) - skipped", sid, i, cached.get("source"))
            if cached.get("dhash") is not None:
                dh_val = int(cached["dhash"])
                local_seen.append(dh_val)
                if dh_val not in fetcher.global_hashes:
                    fetcher.global_hashes.append(dh_val)
            results.append({"sid": sid, "idx": i, **cached, "cached": True})
            continue

        log.info("[%s] scene %02d: %s", sid, i, (text or sp.get("prompt", ""))[:90])
        img, meta = fetcher.build(sid, i, text, sp, style, local_seen)
        canvas = frame_to_canvas(img, args.framing)
        tmp = target.with_suffix(".jpg.tmp")
        try:
            canvas.save(tmp, format="JPEG", quality=93, subsampling=0)
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        dh = int(meta.get("metrics", {}).get("dhash") or dhash(canvas))
        local_seen.append(dh)
        fetcher.global_hashes.append(dh)
        entry = {**meta, "plan_hash": h, "dhash": dh, "file": target.name}
        entry.get("metrics", {}).pop("dhash", None)
        scenes_meta[key] = entry
        flag = {
            "verified-text": "OK ",
            "generated-unverified": "GEN",
            "procedural": "CARD",
        }[meta["verification"]]
        log.info("  -> scene_%02d.jpg  [%s] source=%s %s", i, flag, meta["source"], meta.get("title", ""))
        results.append({"sid": sid, "idx": i, **entry, "cached": False})

    manifest = {
        "id": sid,
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "scenes": scenes_meta,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    save_global_hashes(fetcher.global_hashes)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="Scene visuals fetcher supporting Hormouz API, Commons, Openverse & cards.")
    ap.add_argument("--force", action="store_true", help="re-fetch target scene(s)")
    ap.add_argument("--retry-cards", action="store_true", help="re-try providers for card scenes")
    ap.add_argument("--only", help="process only this short id")
    ap.add_argument("--scene", type=int, help="with --only: refresh only this scene number")
    ap.add_argument("--offline", action="store_true", help="no network: procedural cards only")
    ap.add_argument("--no-generator", action="store_true", help="skip AI generator")
    ap.add_argument("--reset-providers", action="store_true", help="reset provider circuit breaker")
    ap.add_argument("--framing", choices=["auto", "cover", "blurfill"], default="auto")
    args = ap.parse_args()

    log.info("=" * 72)
    log.info(
        "Phase 2B: visuals fetcher v6.13 (Hormouz=%s | guard=%s | chain=%s)",
        "YES" if get_hormouz_key() else "NO",
        "ON" if VISUAL_GUARD_ENABLED else "OFF",
        " -> ".join(HORMOUZ_IMAGE_MODELS),
    )
    log.info("=" * 72)
    try:
        scripts = load_scripts()
        plan = load_plan()
    except (OSError, ValueError) as exc:
        log.error("Initialization failed: %s", exc)
        return 1
    if args.only:
        scripts = [s for s in scripts if s["id"] == args.only]
    if not scripts:
        log.error("No scripts to process.")
        return 1

    global_hashes = load_global_hashes()
    fetcher = SceneFetcher(Net(offline=args.offline, reset=args.reset_providers), args, global_hashes)
    all_rows: list[dict[str, Any]] = []
    for sc in scripts:
        entry = plan.get(sc["id"], {"style": "", "scenes": []})
        all_rows.extend(process_short(fetcher, sc, entry, args))

    log.info("=" * 72)
    counts = {"verified-text": 0, "generated-unverified": 0, "procedural": 0}
    for r in all_rows:
        counts[r["verification"]] += 1
    log.info(
        "Totals: %d verified photo | %d generated | %d procedural",
        counts["verified-text"],
        counts["generated-unverified"],
        counts["procedural"],
    )
    log.info("=" * 72)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.warning("Interrupted.")
        sys.exit(130)
