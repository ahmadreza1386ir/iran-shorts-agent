#!/usr/bin/env python3
"""
Phase 1 (LLM): Autonomous script & visual plan writer via Hormouz Chat Completions API.
Generates fresh Iranian proverbs, short stories/fables, or cultural/historical facts
and appends them to scripts_batch_1.json and visual_plan.json without repeating topics.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from common import (
    BASE_DIR,
    INPUT_JSON,
    PLAN_JSON,
    safe_script_id,
    setup_logging,
)

log = setup_logging("llm_writer")

TOPICS_HISTORY_FILE = BASE_DIR / "data" / "used_topics.json"
MOTIONS = ["push_in", "pan_right", "drift_up", "pull_out", "pan_left", "drift_down"]
FORMATS = ["Proverb", "Short Story", "Cultural/Historical Fact"]


def _env_first(names: tuple[str, ...], default: str = "") -> str:
    for name in names:
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    return default


HORMOUZ_BASE_URL = _env_first(
    ("HORMOUZ_BASE_URL", "GAPGPT_BASE_URL"),
    "https://global-api.hormouz.net/v1",
).rstrip("/")

HORMOUZ_CHAT_MODEL = _env_first(
    ("HORMOUZ_CHAT_MODEL", "HORMOUZ_TEXT_MODEL"),
    "anthropic/claude-haiku-5.5",
)


def get_api_key() -> str:
    return _env_first(("HORMOUZ_API_KEY", "GAPGPT_API_KEY"), "")


def load_used_topics() -> list[str]:
    if not TOPICS_HISTORY_FILE.is_file():
        return []
    try:
        raw = json.loads(TOPICS_HISTORY_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
    except (OSError, ValueError):
        pass
    return []


def save_used_topics(topics: list[str]) -> None:
    TOPICS_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    cleaned = list(dict.fromkeys(t.strip() for t in topics if t.strip()))
    TOPICS_HISTORY_FILE.write_text(
        json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _next_numeric_id(existing_ids: list[str]) -> str:
    nums = [int(x) for x in existing_ids if x.isdigit()]
    return str((max(nums) if nums else 0) + 1)


def _backup_if_exists(path: Path, dry_run: bool) -> None:
    if path.is_file() and not dry_run:
        shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    txt = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", txt)
    if fence_match:
        txt = fence_match.group(1).strip()
    start = txt.find("{")
    end = txt.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No valid JSON object found in LLM response.")
    parsed = json.loads(txt[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Top-level JSON value must be an object.")
    return parsed


def call_llm(api_key: str, fmt: str, used_topics: list[str], num_scenes: int) -> dict[str, Any]:
    url = (
        HORMOUZ_BASE_URL
        if HORMOUZ_BASE_URL.endswith("/chat/completions")
        else f"{HORMOUZ_BASE_URL}/chat/completions"
    )
    avoid_list = ", ".join(used_topics[-40:]) if used_topics else "none"

    system_prompt = (
        "You are an expert scriptwriter and visual art director for viral English YouTube Shorts "
        "about Iranian proverbs, short stories/fables, and cultural/historical facts. "
        "Output ONLY valid JSON with no extra commentary."
    )

    user_prompt = f"""Create ONE original YouTube Short script in format "{fmt}".
Rules:
1. Do NOT repeat any of these previously used topics: [{avoid_list}].
2. Narration length: 85 to 110 English words (around 32-40 seconds when spoken).
3. Hook: Start immediately with a punchy, non-cliche hook (never start with "Did you know").
4. Pronunciation & TTS:
   - "narration_display": clean natural English with authentic transliteration/terms for subtitles.
   - "narration_tts": phonetically smooth English text so text-to-speech pronounces Persian names/proverbs cleanly without awkward pauses.
5. Scenes: Provide exact {num_scenes} visual scenes in English.
   - Explicitly specify exact character counts when people appear (for example: "two people shaking hands facing each other").
   - Never include text, letters, signs, screens with text, or watermarks in scene prompts.
   - Keep visual style coherent: cohesive hand-painted storybook illustration, warm and teal palette, vertical 9:16 framing.

Return strictly this JSON schema:
{{
  "topic_key": "short unique topic identifier",
  "proverb_fa": "Persian phrase/title if applicable or empty string",
  "proverb_en": "transliteration or short title",
  "hook_en": "opening hook sentence",
  "narration_display": "full narration for display/subtitles",
  "narration_tts": "full narration optimized for TTS",
  "scenes": [
    {{
      "desc": "short scene summary",
      "query": "2-4 English keywords for image search fallback",
      "prompt": "detailed vertical 9:16 hand-painted storybook scene description, exact character count, no text, no watermark"
    }}
  ]
}}"""

    payload = {
        "model": HORMOUZ_CHAT_MODEL,
        "temperature": 0.75,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "IranShortsAgent/LLMWriter",
        },
        method="POST",
    )

    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            content = (
                body.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Empty content returned by chat model.")
            return _extract_json_object(content)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, KeyError) as exc:
            last_err = exc
            log.warning("LLM request attempt %d/3 failed: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(2.5 * (attempt + 1))
    assert last_err is not None
    raise last_err


def _anchor_from_segment(seg: str, idx: int) -> str | None:
    if idx == 0:
        return None
    words = re.findall(r"[A-Za-z0-9']+", seg)
    return " ".join(words[: min(3, len(words))]) if words else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate scripts & visual plans using LLM via Hormouz.")
    ap.add_argument("--count", type=int, default=1, help="Number of new shorts to generate")
    ap.add_argument("--scenes", type=int, default=11, help="Number of scenes per short")
    ap.add_argument("--format", choices=FORMATS + ["Auto"], default="Auto")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    api_key = get_api_key()
    if not api_key:
        log.error("HORMOUZ_API_KEY (or GAPGPT_API_KEY) is not set.")
        return 1

    scripts: list[dict[str, Any]] = []
    if INPUT_JSON.is_file():
        loaded = json.loads(INPUT_JSON.read_text(encoding="utf-8-sig"))
        if isinstance(loaded, list):
            scripts = loaded

    plan_data: dict[str, Any] = {"shorts": []}
    if PLAN_JSON.is_file():
        raw_plan = json.loads(PLAN_JSON.read_text(encoding="utf-8-sig"))
        if isinstance(raw_plan, dict) and isinstance(raw_plan.get("shorts"), list):
            plan_data = raw_plan
        elif isinstance(raw_plan, list):
            plan_data = {"shorts": raw_plan}

    plan_shorts: list[dict[str, Any]] = plan_data["shorts"]
    existing_ids: list[str] = []
    for item in scripts:
        if isinstance(item, dict):
            sid = safe_script_id(item.get("id"))
            if sid:
                existing_ids.append(sid)
    for entry in plan_shorts:
        if isinstance(entry, dict):
            sid = safe_script_id(entry.get("id"))
            if sid:
                existing_ids.append(sid)

    used_topics = load_used_topics()
    num_scenes = max(6, args.scenes)
    generated_count = 0

    for idx in range(max(0, args.count)):
        fmt = (
            FORMATS[(len(used_topics) + idx) % len(FORMATS)]
            if args.format == "Auto"
            else args.format
        )
        log.info("Generating short #%d with format='%s' via %s...", idx + 1, fmt, HORMOUZ_CHAT_MODEL)
        llm_out = call_llm(api_key, fmt, used_topics, num_scenes)

        disp = str(llm_out.get("narration_display") or llm_out.get("narration") or "").strip()
        tts = str(llm_out.get("narration_tts") or disp).strip()
        if not disp or not tts:
            log.error("LLM returned empty narration.")
            return 1

        raw_scenes = llm_out.get("scenes")
        if not isinstance(raw_scenes, list) or not raw_scenes:
            log.error("LLM returned invalid scenes list.")
            return 1

        # Normalize scene count to exact num_scenes
        normalized_scenes: list[dict[str, str]] = []
        for s_i in range(num_scenes):
            src_sc = raw_scenes[s_i % len(raw_scenes)]
            if isinstance(src_sc, dict):
                desc = str(src_sc.get("desc") or src_sc.get("scene") or f"Scene {s_i + 1}").strip()
                query = str(src_sc.get("query") or "iranian landscape").strip()
                prompt = str(src_sc.get("prompt") or desc).strip()
            else:
                desc = str(src_sc).strip() or f"Scene {s_i + 1}"
                query = "iranian landscape"
                prompt = desc
            if "no text" not in prompt.lower():
                prompt = f"{prompt}, no text, no watermark"
            normalized_scenes.append({"desc": desc, "query": query, "prompt": prompt})

        target_id = _next_numeric_id(existing_ids)
        existing_ids.append(target_id)

        topic_key = str(llm_out.get("topic_key") or llm_out.get("proverb_en") or f"{fmt}_{target_id}").strip()
        proverb_fa = str(llm_out.get("proverb_fa") or "").strip()
        proverb_en = str(llm_out.get("proverb_en") or topic_key).strip()
        hook_en = str(llm_out.get("hook_en") or disp.split(".")[0]).strip()
        scene_descs = [s["desc"] for s in normalized_scenes]

        new_script = {
            "id": int(target_id),
            "category": fmt,
            "proverb_fa": proverb_fa,
            "proverb_en": proverb_en,
            "word_count": len(disp.split()),
            "hook_en": hook_en,
            "narration": disp,
            "narration_alt": disp,
            "narration_display": disp,
            "narration_tts": tts,
            "visual_scenes": scene_descs,
            "scenes": scene_descs,
        }

        new_plan_entry = {
            "id": target_id,
            "style": "Cohesive hand-painted storybook illustration, expressive brushwork, unified warm-and-teal color palette, vertical 9:16 composition, no text, no watermark",
            "scenes": [
                {
                    "desc": sc["desc"],
                    "query": sc["query"],
                    "anchor": _anchor_from_segment(sc["desc"], s_i),
                    "motion": MOTIONS[s_i % len(MOTIONS)],
                    "order": ["commons", "card"],
                    "commons": {
                        "queries": [sc["query"], " ".join(sc["query"].split()[:2])],
                        "reject": ["text", "watermark", "logo", "map", "diagram", "cartoon", "clipart"],
                    },
                    "prompt": sc["prompt"],
                    "card": {"palette": "story_sunset", "layers": [["sun", {}], ["dunes", {}]]},
                }
                for s_i, sc in enumerate(normalized_scenes)
            ],
        }

        if not args.dry_run:
            scripts.append(new_script)
            plan_shorts.append(new_plan_entry)
            used_topics.append(topic_key)
        generated_count += 1
        log.info("Prepared short id=%s (%s): %s", target_id, fmt, topic_key)

    if not args.dry_run and generated_count > 0:
        _backup_if_exists(INPUT_JSON, args.dry_run)
        _backup_if_exists(PLAN_JSON, args.dry_run)
        INPUT_JSON.write_text(json.dumps(scripts, ensure_ascii=False, indent=2), encoding="utf-8")
        plan_data["shorts"] = plan_shorts
        PLAN_JSON.write_text(json.dumps(plan_data, ensure_ascii=False, indent=2), encoding="utf-8")
        save_used_topics(used_topics)
        log.info("Saved %d new script(s) to %s and %s.", generated_count, INPUT_JSON.name, PLAN_JSON.name)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())