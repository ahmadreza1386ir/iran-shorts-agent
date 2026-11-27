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
            return False, f"guard_rejected: {guard_reason}", m

    dh = dhash(img)
    m["dhash"] = dh
    for prev in forbidden_hashes:
        if bin(dh ^ prev).count("1") <= DHASH_DUP_DISTANCE:
            return False, "duplicate or too similar to an already-used visual", m
    return True, "ok", m


def frame_to_canvas(img: Image.Image, mode: str = "auto") -> Image.Image:
    img = ImageOps.exif_transpose(img).convert("RGB")
    fitted = ImageOps.fit(img, (CANVAS_W, CANVAS_H), Image.LANCZOS, centering=(0.5, 0.35))
    return ImageEnhance.Contrast(fitted).enhance(1.03)


def plan_hash(scene_plan: dict[str, Any], style: str, text: str) -> str:
    payload = {
        "style": style,
        "text": text,
        "desc": scene_plan.get("desc", ""),
        "query": scene_plan.get("query", ""),
        "prompt": scene_plan.get("prompt", ""),
        "motion": scene_plan.get("motion", ""),
        "canvas": [CANVAS_W, CANVAS_H],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _decode_image_bytes(raw_bytes: bytes) -> Image.Image:
    with Image.open(io.BytesIO(raw_bytes)) as im:
        im.load()
        return im.convert("RGB")


def generate_hormouz_image(net: Net, api_key: str, prompt: str, model: str) -> Image.Image:
    url = f"{HORMOUZ_ROOT}/images/generations"
    payload = {
        "model": model,
        "prompt": prompt,
        "size": HORMOUZ_IMAGE_SIZE,
        "n": 1,
    }
    raw = net.request(
        "hormouz",
        url,
        method="POST",
        payload=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=120,
        retries=1,
        auth_fatal=True,
    )
    data = json.loads(raw.decode("utf-8", "replace"))
    items = data.get("data") or []
    if not isinstance(items, list) or not items:
        raise FetchError("Hormouz image response missing 'data' list.")

    first = items[0] if isinstance(items[0], dict) else {}
    b64_data = first.get("b64_json") or first.get("image_base64")
    if isinstance(b64_data, str) and b64_data.strip():
        clean_b64 = b64_data.split(",", 1)[-1].strip()
        img_bytes = base64.b64decode(clean_b64)
        if len(img_bytes) > MAX_BYTES:
            raise FetchError("Decoded image exceeds size limit.")
        return _decode_image_bytes(img_bytes)

    img_url = str(first.get("url") or "").strip()
    if img_url:
        img_bytes = net.request("hormouz", img_url, method="GET", timeout=60, retries=1, auth_fatal=False)
        return _decode_image_bytes(img_bytes)

    raise FetchError("Hormouz response contained neither b64_json nor url.")


def fetch_scene_image(
    net: Net,
    api_key: str,
    scene_plan: dict[str, Any],
    style: str,
    forbidden_hashes: list[int],
) -> tuple[Image.Image, str, dict[str, Any]]:
    desc = str(scene_plan.get("desc") or "Iranian cultural scene").strip()
    query = str(scene_plan.get("query") or "iranian heritage").strip()
    base_prompt = str(scene_plan.get("prompt") or desc).strip()
    full_prompt = f"{base_prompt}. {style}".strip()

    # 1. Try Hormouz multi-model chain with guard & duplicate retries
    if api_key and not net.is_disabled("hormouz"):
        for model in HORMOUZ_IMAGE_MODELS:
            max_tries = 1 + max(0, HORMOUZ_GUARD_RETRIES) + max(0, HORMOUZ_MAX_DUP_RETRIES)
            for attempt in range(1, max_tries + 1):
                try:
                    img = generate_hormouz_image(net, api_key, full_prompt, model)
                    ok, reason, metrics = assess(img, forbidden_hashes, photo=False)
                    if ok:
                        return img, f"hormouz:{model}", metrics
                    log.info("Hormouz (%s) attempt %d rejected: %s", model, attempt, reason)
                except FetchError as exc:
                    log.warning("Hormouz (%s) attempt %d error: %s", model, attempt, exc)
                    if exc.status in (401, 402, 403):
                        break
                except Exception as exc:
                    log.warning("Hormouz (%s) attempt %d failed: %s", model, attempt, exc)

    # 2. Fallback to Commons / Openverse
    rules = scene_plan.get("commons") if isinstance(scene_plan.get("commons"), dict) else {}
    queries = [query]
    if isinstance(rules.get("queries"), list):
        for q in rules["queries"]:
            qs = str(q).strip()
            if qs and qs not in queries:
                queries.append(qs)

    candidates: list[Candidate] = []
    for q in queries[:2]:
        try:
            candidates.extend(commons_candidates(net, q, rules))
        except Exception as exc:
            log.debug("Commons query %r skipped: %s", q, exc)
        try:
            candidates.extend(openverse_candidates(net, q, rules))
        except Exception as exc:
            log.debug("Openverse query %r skipped: %s", q, exc)
        if len(candidates) >= MAX_CANDIDATES_PER_SCENE:
            break

    candidates.sort(key=lambda c: c.score, reverse=True)
    for cand in candidates[:MAX_CANDIDATES_PER_SCENE]:
        try:
            raw_img = net.request(cand.source, cand.url, method="GET", timeout=35, retries=1, auth_fatal=False)
            img = _decode_image_bytes(raw_img)
            ok, reason, metrics = assess(img, forbidden_hashes, photo=True)
            if ok:
                return img, cand.source, metrics
            log.debug("Candidate %s rejected: %s", cand.url, reason)
        except Exception as exc:
            log.debug("Candidate download failed (%s): %s", cand.url, exc)

    # 3. Guaranteed procedural card fallback
    card_spec = scene_plan.get("card") if isinstance(scene_plan.get("card"), dict) else {}
    card_img = render_card(
        title=desc,
        subtitle=query,
        palette=str(card_spec.get("palette") or "story_sunset"),
        width=CANVAS_W,
        height=CANVAS_H,
    ).convert("RGB")
    dh = dhash(card_img)
    return card_img, "card", {"w": CANVAS_W, "h": CANVAS_H, "dhash": dh}


def save_scene_png(img: Image.Image, out_path: Path) -> None:
    canvas = frame_to_canvas(img)
    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    _atomic_write_bytes(out_path, buf.getvalue())


def process_short(
    short_id: str,
    plan_entry: dict[str, Any],
    api_key: str,
    net: Net,
    global_hashes: list[int],
    force: bool = False,
) -> bool:
    clean_stale_part_files(VISUALS_DIR)
    scenes = plan_entry.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        log.error("Short %s has no scenes in visual plan.", short_id)
        return False

    style = str(plan_entry.get("style") or "Cohesive hand-painted storybook illustration, vertical 9:16").strip()
    local_hashes = list(global_hashes)
    all_ok = True

    for idx, sc in enumerate(scenes):
        if not isinstance(sc, dict):
            log.warning("Short %s scene %d invalid; using fallback dict.", short_id, idx + 1)
            sc = {"desc": f"Scene {idx + 1}", "query": "iranian heritage", "prompt": f"Scene {idx + 1}"}

        out_path = VISUALS_DIR / f"short_{short_id}_scene_{idx + 1:02d}.png"
        h_val = plan_hash(sc, style, str(sc.get("desc") or ""))

        if not force and out_path.is_file() and out_path.stat().st_size > 1024:
            try:
                with Image.open(out_path) as existing:
                    existing.load()
                    if existing.size == (CANVAS_W, CANVAS_H):
                        local_hashes.append(dhash(existing))
                        continue
            except Exception:
                pass

        try:
            img, src_name, metrics = fetch_scene_image(net, api_key, sc, style, local_hashes)
            save_scene_png(img, out_path)
            dh = metrics.get("dhash")
            if isinstance(dh, int):
                local_hashes.append(dh)
                global_hashes.append(dh)
            log.info(
                "Short %s scene %02d/%02d ready via %s (hash=%s)",
                short_id,
                idx + 1,
                len(scenes),
                src_name,
                h_val,
            )
        except Exception as exc:
            log.error("Short %s scene %02d failed: %s", short_id, idx + 1, exc)
            all_ok = False

    return all_ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch or generate scene visuals for shorts.")
    ap.add_argument("--id", type=str, default=None, help="Target short id")
    ap.add_argument("--force", action="store_true", help="Regenerate existing scene images")
    ap.add_argument("--offline", action="store_true", help="Skip network requests and use fallbacks")
    ap.add_argument("--reset-state", action="store_true", help="Reset provider state")
    args = ap.parse_args()

    VISUALS_DIR.mkdir(parents=True, exist_ok=True)
    scripts = load_scripts()
    plan_data = load_plan()
    plan_shorts = plan_data.get("shorts") if isinstance(plan_data, dict) else []
    if not isinstance(plan_shorts, list):
        plan_shorts = []

    plan_by_id: dict[str, dict[str, Any]] = {}
    for entry in plan_shorts:
        if isinstance(entry, dict):
            sid = safe_script_id(entry.get("id"))
            if sid:
                plan_by_id[sid] = entry

    target_ids: list[str] = []
    if args.id is not None:
        sid = safe_script_id(args.id)
        if not sid:
            log.error("Invalid --id %r.", args.id)
            return 1
        target_ids = [sid]
    else:
        for item in scripts:
            if isinstance(item, dict):
                sid = safe_script_id(item.get("id"))
                if sid and sid not in target_ids:
                    target_ids.append(sid)
        for sid in plan_by_id:
            if sid not in target_ids:
                target_ids.append(sid)

    if not target_ids:
        log.warning("No shorts found to process visuals for.")
        return 0

    api_key = get_hormouz_key()
    net = Net(offline=args.offline, reset=args.reset_state)
    global_hashes = load_global_hashes()
    success_count = 0

    for sid in target_ids:
        entry = plan_by_id.get(sid)
        if not entry:
            log.error("No visual plan entry found for short id=%s.", sid)
            continue
        if process_short(sid, entry, api_key, net, global_hashes, force=args.force):
            success_count += 1

    save_global_hashes(global_hashes)

    if success_count == 0:
        log.error("Failed generating visuals for all %d target short(s).", len(target_ids))
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
