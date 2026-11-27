#!/usr/bin/env python3
"""
Phase 2c (Optional): Upscale generated scene images via Hormouz predictions API
(e.g., prunaai/p-image-upscale) while preserving exact aspect ratio and caching
upscaled markers so images are never billed twice.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from common import (
    IMAGE_EXTS,
    VISUALS_DIR,
    load_scripts,
    safe_script_id,
    setup_logging,
)

log = setup_logging("upscale_visuals")


def _env_first(names: tuple[str, ...], default: str = "") -> str:
    for name in names:
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    return default


def _validate_base_url(raw_url: str) -> str:
    cleaned = raw_url.strip().rstrip("/")
    if not (cleaned.startswith("https://") or cleaned.startswith("http://")):
        raise ValueError("Invalid HORMOUZ_BASE_URL scheme; expected http:// or https://")
    return cleaned


HORMOUZ_BASE_URL = _validate_base_url(
    _env_first(
        ("HORMOUZ_BASE_URL", "GAPGPT_BASE_URL"),
        "https://global-api.hormouz.net/v1",
    )
)

DEFAULT_UPSCALE_MODEL = _env_first(
    ("HORMOUZ_UPSCALE_MODEL",),
    "prunaai/p-image-upscale",
)


def get_api_key() -> str:
    return _env_first(("HORMOUZ_API_KEY", "GAPGPT_API_KEY"), "")


def _image_to_data_uri(img_path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(img_path))
    if not mime:
        mime = "image/png"
    b64 = base64.b64encode(img_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _download_bytes(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "IranShortsAgent/Upscaler"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _extract_image_bytes(payload: dict[str, Any]) -> Optional[bytes]:
    for key in ("image_base64", "b64_json", "image", "output_base64"):
        val = payload.get(key)
        if isinstance(val, str) and len(val) > 200 and not val.startswith("http"):
            raw_b64 = val.split(",", 1)[-1]
            try:
                decoded = base64.b64decode(raw_b64, validate=False)
                if len(decoded) > 1024:
                    return decoded
            except Exception:
                pass

    candidates: list[Any] = [
        payload.get("output"),
        payload.get("image_url"),
        payload.get("url"),
    ]
    data_obj = payload.get("data")
    if isinstance(data_obj, dict):
        candidates.extend([data_obj.get("url"), data_obj.get("image_url"), data_obj.get("output")])
    elif isinstance(data_obj, list) and data_obj:
        first = data_obj[0]
        if isinstance(first, dict):
            candidates.extend([first.get("url"), first.get("image_url")])

    for cand in candidates:
        if isinstance(cand, str):
            if cand.startswith("data:image/") and "," in cand:
                try:
                    decoded = base64.b64decode(cand.split(",", 1)[1], validate=False)
                    if len(decoded) > 1024:
                        return decoded
                except Exception:
                    pass
            elif cand.startswith(("http://", "https://")):
                downloaded = _download_bytes(cand)
                if len(downloaded) > 1024:
                    return downloaded
        elif isinstance(cand, list):
            for item in cand:
                if isinstance(item, str) and item.startswith(("http://", "https://")):
                    downloaded = _download_bytes(item)
                    if len(downloaded) > 1024:
                        return downloaded
                elif isinstance(item, dict):
                    u = item.get("url") or item.get("image_url")
                    if isinstance(u, str) and u.startswith(("http://", "https://")):
                        downloaded = _download_bytes(u)
                        if len(downloaded) > 1024:
                            return downloaded
    return None


def upscale_single_image(
    img_path: Path,
    api_key: str,
    model: str,
) -> bytes:
    base = HORMOUZ_BASE_URL.rstrip("/")
    pred_url = base if base.endswith("/predictions") else f"{base}/predictions"

    data_uri = _image_to_data_uri(img_path)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "IranShortsAgent/Upscaler",
    }

    payload = {
        "model": model,
        "input": {
            "image": data_uri,
        },
    }

    req = urllib.request.Request(
        pred_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=90) as resp:
        raw_body = resp.read().decode("utf-8", "replace")
    parsed = json.loads(raw_body)
    if not isinstance(parsed, dict):
        raise RuntimeError("Invalid JSON response from Hormouz /predictions.")

    pred_id = parsed.get("id")
    status = str(parsed.get("status") or "").lower()
    if pred_id and status in ("starting", "processing", "queued", "pending"):
        poll_url = f"{pred_url}/{pred_id}"
        for _ in range(30):
            time.sleep(1.5)
            poll_req = urllib.request.Request(
                poll_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": "IranShortsAgent/Upscaler",
                },
                method="GET",
            )
            with urllib.request.urlopen(poll_req, timeout=30) as presp:
                parsed = json.loads(presp.read().decode("utf-8", "replace"))
            status = str(parsed.get("status") or "").lower()
            if status in ("succeeded", "completed", "failed", "canceled", "error"):
                break

    if status in ("failed", "canceled", "error"):
        raise RuntimeError(f"Upscale prediction failed: {parsed.get('error')}")

    extracted = _extract_image_bytes(parsed)
    if not extracted:
        raise RuntimeError("No upscaled image output returned in prediction payload.")
    return extracted


def _atomic_replace_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def collect_scene_images_for_short(sid: str) -> list[Path]:
    scene_dir = VISUALS_DIR / f"short_{sid}"
    found: list[Path] = []
    if scene_dir.is_dir():
        for p in sorted(scene_dir.glob("scene_*.*")):
            if p.suffix.lower() in IMAGE_EXTS and p.is_file() and p.stat().st_size > 1024:
                found.append(p)
    if not found:
        for p in sorted(VISUALS_DIR.glob(f"short_{sid}_scene_*.*")):
            if p.suffix.lower() in IMAGE_EXTS and p.is_file() and p.stat().st_size > 1024:
                found.append(p)
    return found


def process_short_upscale(
    sid: str,
    api_key: str,
    model: str,
    force: bool = False,
) -> int:
    images = collect_scene_images_for_short(sid)
    if not images:
        log.warning("[%s] No scene images found to upscale.", sid)
        return 0

    upgraded = 0
    for img_path in images:
        marker = img_path.with_suffix(img_path.suffix + ".upscaled")
        if marker.is_file() and not force:
            log.info("[%s] %s already upscaled; skipping.", sid, img_path.name)
            continue

        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                t0 = time.monotonic()
                new_bytes = upscale_single_image(img_path, api_key, model)
                _atomic_replace_bytes(img_path, new_bytes)
                marker.write_text(f"{model}\n", encoding="utf-8")
                elapsed = time.monotonic() - t0
                log.info(
                    "[%s] Upscaled %s via %s in %.2fs (%.1f KB)",
                    sid,
                    img_path.name,
                    model,
                    elapsed,
                    len(new_bytes) / 1024.0,
                )
                upgraded += 1
                break
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError, ValueError) as exc:
                last_err = exc
                log.warning(
                    "[%s] Upscale attempt %d/3 failed for %s: %s",
                    sid,
                    attempt,
                    img_path.name,
                    exc,
                )
                if attempt < 3:
                    time.sleep(2.0 * attempt)
        else:
            log.error("[%s] Keeping original %s after upscale failure: %s", sid, img_path.name, last_err)

    return upgraded


def main() -> int:
    ap = argparse.ArgumentParser(description="Upscale scene images via Hormouz predictions API.")
    ap.add_argument("--id", type=str, default=None, help="Specific short ID to upscale")
    ap.add_argument("--only", type=str, default=None, help="Alias for --id")
    ap.add_argument("--model", type=str, default=DEFAULT_UPSCALE_MODEL, help="Hormouz upscale model ID")
    ap.add_argument("--force", action="store_true", help="Re-upscale even if .upscaled marker exists")
    args = ap.parse_args()

    model = (args.model or "").strip()
    if not model:
        log.info("No upscale model configured; skipping upscale step.")
        return 0

    api_key = get_api_key()
    if not api_key:
        log.warning("HORMOUZ_API_KEY not set; skipping upscale step.")
        return 0

    target_id = (args.id or args.only or "").strip() or None
    if target_id:
        target_ids = [target_id]
    else:
        scripts = load_scripts()
        target_ids = [
            safe_script_id(s.get("id"))
            for s in scripts
            if isinstance(s, dict) and safe_script_id(s.get("id"))
        ]

    if not target_ids:
        log.warning("No target short IDs found for upscale.")
        return 0

    total_upgraded = 0
    for sid in target_ids:
        total_upgraded += process_short_upscale(sid, api_key, model, force=args.force)

    log.info("Upscale finished: %d image(s) upgraded.", total_upgraded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
