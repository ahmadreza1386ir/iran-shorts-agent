#!/usr/bin/env python3
"""
Hormouz Predictions Image Generator with automatic multi-model fallback:
1) black-forest-labs/flux-schnell
2) prunaai/z-image-turbo
3) black-forest-labs/flux-2-klein-4b
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_IMAGE_MODELS = [
    "black-forest-labs/flux-schnell",
    "prunaai/z-image-turbo",
    "black-forest-labs/flux-2-klein-4b",
]


def get_hormouz_image_models() -> list[str]:
    raw = (
        os.environ.get("HORMOUZ_IMAGE_MODELS")
        or os.environ.get("HORMOUZ_IMAGE_MODEL")
        or ""
    ).strip()
    if not raw:
        return list(DEFAULT_IMAGE_MODELS)
    parsed = [m.strip() for m in raw.split(",") if m.strip()]
    for fallback in DEFAULT_IMAGE_MODELS:
        if fallback not in parsed:
            parsed.append(fallback)
    return parsed


def _http_json(url: str, api_key: str, payload: dict[str, Any] | None = None, timeout: int = 45) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "IranShortsAgent/HormouzPredictions",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _http_bytes(url: str, api_key: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "IranShortsAgent/HormouzAssets",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _build_prediction_input(model: str, prompt: str, aspect_ratio: str = "9:16") -> dict[str, Any]:
    if model == "prunaai/z-image-turbo":
        return {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "num_inference_steps": 8,
            "guidance_scale": 3.5,
            "output_format": "jpg",
        }
    if model == "black-forest-labs/flux-2-klein-4b":
        return {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "output_megapixels": "1",
            "output_format": "jpg",
            "output_quality": 95,
        }
    return {
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "output_format": "jpg",
    }


def _generate_via_predictions(
    prompt: str,
    model: str,
    api_key: str,
    base_url: str,
    aspect_ratio: str = "9:16",
    max_wait_sec: int = 120,
) -> bytes:
    api_root = base_url.rstrip("/")
    if api_root.endswith("/images/generations"):
        api_root = api_root[: -len("/images/generations")]
    if not api_root.endswith("/v1"):
        api_root = f"{api_root}/v1"

    pred = _http_json(
        f"{api_root}/predictions",
        api_key,
        payload={
            "version": model,
            "input": _build_prediction_input(model, prompt, aspect_ratio=aspect_ratio),
        },
        timeout=45,
    )
    pred_id = str(pred.get("id") or "").strip()
    if not pred_id:
        raise RuntimeError(f"Missing prediction id for {model}: {pred}")

    deadline = time.time() + max_wait_sec
    succeed_at: float | None = None
    last_res: dict[str, Any] = pred

    while time.time() < deadline:
        last_res = _http_json(f"{api_root}/predictions/{pred_id}", api_key, timeout=30)
        status = str(last_res.get("status") or "").lower()
        if status in ("failed", "canceled"):
            raise RuntimeError(f"Prediction {status} ({model}): {last_res.get('error') or last_res}")

        assets = last_res.get("assets") or []
        if status == "succeeded" and isinstance(assets, list) and len(assets) > 0:
            asset_id = str(assets[0].get("id") or "").strip()
            if asset_id:
                img_data = _http_bytes(f"{api_root}/assets/{asset_id}", api_key, timeout=60)
                if len(img_data) > 2000:
                    return img_data

        if status == "succeeded":
            if succeed_at is None:
                succeed_at = time.time()
            # Fallback to Hormouz file proxy if asset mirror takes > 20s
            if (time.time() - succeed_at) > 20:
                out = last_res.get("output")
                out_url = out[0] if isinstance(out, list) and out else (out if isinstance(out, str) else "")
                if out_url:
                    q = urllib.parse.urlencode({"url": out_url})
                    img_data = _http_bytes(f"{api_root}/files/proxy?{q}", api_key, timeout=60)
                    if len(img_data) > 2000:
                        return img_data

        time.sleep(2.0)

    raise TimeoutError(f"Prediction timed out after {max_wait_sec}s for {model}")


def generate_image_with_fallback(
    prompt: str,
    api_key: str,
    base_url: str = "https://api.hormouz.net/v1",
    aspect_ratio: str = "9:16",
    logger: Any = None,
) -> tuple[bytes, str]:
    errors: list[str] = []
    for model in get_hormouz_image_models():
        try:
            img_bytes = _generate_via_predictions(
                prompt=prompt,
                model=model,
                api_key=api_key,
                base_url=base_url,
                aspect_ratio=aspect_ratio,
            )
            return img_bytes, model
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, RuntimeError, ValueError) as exc:
            msg = f"{model} -> {exc}"
            errors.append(msg)
            if logger:
                logger.warning("Fallback trigger: %s", msg)
            continue
    raise RuntimeError("All Hormouz image models failed: " + " | ".join(errors))