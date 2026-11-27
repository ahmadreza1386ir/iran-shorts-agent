#!/usr/bin/env python3
"""
Phase 1 (LLM): Hardened script & visual plan writer via Hormouz Chat Completions API.
Uses explosive hook brainstorming to craft gripping openings, builds human curiosity
and varied rhythm across the story, locks core meaning, and appends validated entries
to scripts_batch_1.json and visual_plan.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
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

DATA_DIR = BASE_DIR / "data"
TOPICS_HISTORY_FILE = DATA_DIR / "used_topics.json"
BACKUP_DIR = DATA_DIR / ".backups"

MOTIONS = ["push_in", "pan_right", "drift_up", "pull_out", "pan_left", "drift_down"]
FORMATS = ["Proverb", "Short Story", "Cultural/Historical Fact"]

MIN_WORDS = 75
MAX_WORDS = 130
MIN_SCENES = 5
MAX_SCENES = 20


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

HORMOUZ_CHAT_MODEL = _env_first(
    ("HORMOUZ_CHAT_MODEL", "HORMOUZ_TEXT_MODEL"),
    "anthropic/claude-haiku-5.5",
)


def get_api_key() -> str:
    return _env_first(("HORMOUZ_API_KEY", "GAPGPT_API_KEY"), "")


def _atomic_write_text(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
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


def _backup_if_exists(path: Path, dry_run: bool) -> None:
    if path.is_file() and not dry_run:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        dest = BACKUP_DIR / f"{path.name}.bak"
        shutil.copyfile(path, dest)


def load_used_topics() -> list[str]:
    if not TOPICS_HISTORY_FILE.is_file():
        return []
    try:
        raw = json.loads(TOPICS_HISTORY_FILE.read_text(encoding="utf-8-sig"))
        if isinstance(raw, list):
            return [str(x).strip()[:160] for x in raw if str(x).strip()]
    except (OSError, ValueError) as exc:
        log.warning("Could not read used_topics.json cleanly: %s", exc)
    return []


def save_used_topics(topics: list[str], dry_run: bool) -> None:
    cleaned = list(dict.fromkeys(t.strip()[:160] for t in topics if t.strip()))
    if not dry_run:
        _atomic_write_text(
            TOPICS_HISTORY_FILE,
            json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n",
        )


def _next_numeric_id(existing_ids: list[str]) -> str:
    nums = [int(x) for x in existing_ids if x.isdigit()]
    return str((max(nums) if nums else 0) + 1)


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    txt = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", txt, flags=re.IGNORECASE)
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


def sanitize_tts_text(text: str) -> str:
    cleaned = re.sub(r"[\r\n\t]+", " ", text)
    cleaned = re.sub(r"\b[A-Za-z]+-[A-Za-z]+(?:-[A-Za-z]+)+\b", "", cleaned)
    cleaned = re.sub(r"\s+([.,!?;:])", r"\1", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


def _ensure_no_text_clause(prompt: str) -> str:
    lower = prompt.lower()
    addition_parts: list[str] = []
    if "no text" not in lower:
        addition_parts.append("no text")
    if "no watermark" not in lower:
        addition_parts.append("no watermark")
    if not addition_parts:
        return prompt.strip()
    return f"{prompt.strip()}, {', '.join(addition_parts)}"


def _validate_llm_payload(
    data: dict[str, Any],
    num_scenes: int,
) -> tuple[str, str, str, str, str, str, str, list[dict[str, str]]]:
    disp = re.sub(r"\s+", " ", str(data.get("narration_display") or data.get("narration") or "")).strip()
    if not disp:
        raise ValueError("Empty narration_display.")

    word_count = len(disp.split())
    if word_count < MIN_WORDS or word_count > MAX_WORDS:
        raise ValueError(f"Narration word count {word_count} out of range [{MIN_WORDS}, {MAX_WORDS}].")

    raw_tts = str(data.get("narration_tts") or "").strip()
    tts = sanitize_tts_text(raw_tts if raw_tts else disp)
    if len(tts.split()) < MIN_WORDS - 10:
        raise ValueError("Sanitized TTS narration too short.")

    raw_scenes = data.get("scenes")
    if not isinstance(raw_scenes, list) or len(raw_scenes) < num_scenes:
        raise ValueError(
            f"Expected at least {num_scenes} scenes, got {len(raw_scenes) if isinstance(raw_scenes, list) else 0}."
        )

    scenes_out: list[dict[str, str]] = []
    for i in range(num_scenes):
        item = raw_scenes[i]
        if not isinstance(item, dict):
            raise ValueError(f"Scene {i + 1} is not an object.")
        desc = re.sub(r"\s+", " ", str(item.get("desc") or "").strip())
        query = re.sub(r"\s+", " ", str(item.get("query") or "").strip())
        prompt = re.sub(r"\s+", " ", str(item.get("prompt") or "").strip())
        if not desc or not prompt:
            raise ValueError(f"Scene {i + 1} missing desc or prompt.")
        if not query:
            query = " ".join(re.findall(r"[A-Za-z]+", desc)[:3]) or "iranian heritage"
        scenes_out.append({
            "desc": desc[:240],
            "query": query[:100],
            "prompt": _ensure_no_text_clause(prompt[:600]),
        })

    core_message = re.sub(r"\s+", " ", str(data.get("core_message") or "")).strip()
    if not core_message:
        raise ValueError("Missing core_message.")

    topic_key = re.sub(r"\s+", " ", str(data.get("topic_key") or "")).strip()
    if not topic_key:
        raise ValueError("Missing topic_key.")

    proverb_fa = str(data.get("proverb_fa") or "").strip()[:160]
    proverb_en = re.sub(r"\s+", " ", str(data.get("proverb_en") or topic_key)).strip()[:160]

    raw_hook = str(data.get("hook_en") or "").strip()
    if not raw_hook and isinstance(data.get("hook_candidates"), list) and data["hook_candidates"]:
        raw_hook = str(data["hook_candidates"][0]).strip()
    if not raw_hook:
        raw_hook = disp.split(".")[0].strip()
    hook_en = re.sub(r"\s+", " ", raw_hook)[:220]

    return topic_key, core_message, proverb_fa, proverb_en, hook_en, disp, tts, scenes_out


def call_llm(
    api_key: str,
    fmt: str,
    used_topics: list[str],
    num_scenes: int,
) -> tuple[str, str, str, str, str, str, str, list[dict[str, str]]]:
    url = (
        HORMOUZ_BASE_URL
        if HORMOUZ_BASE_URL.endswith("/chat/completions")
        else f"{HORMOUZ_BASE_URL}/chat/completions"
    )
    avoid_list = ", ".join(used_topics[-40:]) if used_topics else "none"

    system_prompt = (
        "You are an expressive, human-sounding storyteller and scriptwriter for English YouTube Shorts "
        "about Iranian proverbs, short stories/fables, and cultural/historical facts. "
        "Write like a real person talking with genuine excitement, surprise, and curiosity—never like a flat textbook "
        "or monotonous narrator. Use divergent hook brainstorming to open with an arresting surprise or contrast "
        "(such as immovable mountains standing still while human paths cross again), keep curiosity alive mid-way "
        "with shifting pacing and vivid detail, and deliver a sharp, memorable landing. "
        "Stay strictly faithful to the true meaning of the proverb or story. "
        "Output ONLY valid JSON with no extra commentary."
    )

    user_prompt = f"""Create ONE original YouTube Short script in format "{fmt}".
Rules:
1. Do NOT repeat any of these previously used topics: [{avoid_list}].
2. Define "core_message" first so the hook, curiosity arc, and ending all lock onto the exact true meaning.
3. Run a divergent brainstorm in "hook_candidates" (3 distinct punchy opening lines: one surprising/counterintuitive contrast like immovable mountains while people cross paths again, one sudden human twist or exclamation, one intriguing paradox). Pick the most captivating opening as "hook_en" and start "narration_display" directly with that hook.
4. Human tone & non-monotonous pacing:
   - Open with genuine excitement or surprise that grabs attention immediately.
   - Continue with curiosity and tension—vary sentence lengths (mix short punchy beats with slightly longer storytelling lines) so it never sounds robotic or flat.
   - Avoid stiff transitions or clichés like "Did you know", "Today we will talk about", "In summary", or "In conclusion".
5. "narration_display": 85 to 112 natural English words (around 32-38 seconds spoken).
6. "narration_tts": natural spoken English with expressive punctuation (exclamation/question/comma pauses where natural) so TTS sounds alive and human. Avoid hyphenated Persian transliterations that sound choppy.
7. "scenes": Provide EXACTLY {num_scenes} distinct visual scenes in English.
   - Scene 1 MUST visually match the opening hook immediately.
   - Each subsequent scene must advance the story or curiosity arc without repeating the same action.
   - Specify exact character counts whenever people appear (e.g., "two travelers meeting on a mountain pass").
   - Never include text, letters, signs, captions, screens with text, or watermarks in scene prompts.
   - Visual style: cohesive hand-painted storybook illustration, warm and teal palette, vertical 9:16 framing.

Return strictly this JSON schema:
{{
  "topic_key": "short unique topic identifier",
  "core_message": "one clear sentence stating the exact true meaning/lesson",
  "proverb_fa": "Persian phrase/title if applicable or empty string",
  "proverb_en": "clear English title or short phrase",
  "hook_candidates": [
    "hook option 1 (surprising contrast)",
    "hook option 2 (excited/curious opener)",
    "hook option 3 (intriguing paradox)"
  ],
  "hook_en": "the single most gripping opening hook line",
  "narration_display": "full narration starting with hook_en with varied human pacing",
  "narration_tts": "full narration optimized for expressive, natural TTS",
  "scenes": [
    {{
      "desc": "concise scene description",
      "query": "2-4 English keywords",
      "prompt": "vertical 9:16 hand-painted storybook illustration, exact character count, warm and teal palette, no text, no watermark"
    }}
  ]
}}"""

    payload = {
        "model": HORMOUZ_CHAT_MODEL,
        "temperature": 0.9,
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
            "Accept": "application/json",
            "User-Agent": "IranShortsAgent/LLMWriter",
        },
        method="POST",
    )

    last_err: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw_body = resp.read().decode("utf-8", "replace")
            body = json.loads(raw_body)
            choices = body.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ValueError("Missing choices in LLM response.")
            content = choices[0].get("message", {}).get("content", "")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Empty content returned by chat model.")
            parsed = _extract_json_object(content)
            return _validate_llm_payload(parsed, num_scenes)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, KeyError, TypeError) as exc:
            last_err = exc
            log.warning("LLM attempt %d/4 failed: %s", attempt + 1, exc)
            if attempt < 3:
                time.sleep(2.5 * (attempt + 1))

    assert last_err is not None
    raise last_err


def _anchor_from_segment(seg: str, idx: int) -> str | None:
    if idx == 0:
        return None
    words = re.findall(r"[A-Za-z0-9']+", seg)
    return " ".join(words[: min(3, len(words))]) if words else None


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(loaded, list):
        return [x for x in loaded if isinstance(x, dict)]
    raise ValueError(f"Expected list in {path.name}")


def _load_plan(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"shorts": []}
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(raw, dict) and isinstance(raw.get("shorts"), list):
        raw["shorts"] = [x for x in raw["shorts"] if isinstance(x, dict)]
        return raw
    if isinstance(raw, list):
        return {"shorts": [x for x in raw if isinstance(x, dict)]}
    raise ValueError(f"Invalid structure in {path.name}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate validated scripts & visual plans via Hormouz.")
    ap.add_argument("--count", type=int, default=1, help="Number of new shorts to generate")
    ap.add_argument("--scenes", type=int, default=11, help="Number of scenes per short")
    ap.add_argument("--format", choices=FORMATS + ["Auto"], default="Auto")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.count < 0:
        log.error("--count cannot be negative.")
        return 1

    num_scenes = args.scenes
    if num_scenes < MIN_SCENES or num_scenes > MAX_SCENES:
        log.error("--scenes must be between %d and %d.", MIN_SCENES, MAX_SCENES)
        return 1

    api_key = get_api_key()
    if not api_key:
        log.error("HORMOUZ_API_KEY (or GAPGPT_API_KEY) is not set.")
        return 1

    try:
        scripts = _load_json_list(INPUT_JSON)
        plan_data = _load_plan(PLAN_JSON)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        log.error("Failed loading existing data files: %s", exc)
        return 1

    plan_shorts: list[dict[str, Any]] = plan_data["shorts"]
    existing_ids: list[str] = []
    for item in scripts:
        sid = safe_script_id(item.get("id"))
        if sid:
            existing_ids.append(sid)
    for entry in plan_shorts:
        sid = safe_script_id(entry.get("id"))
        if sid:
            existing_ids.append(sid)

    used_topics = load_used_topics()
    used_lower = {t.lower() for t in used_topics}
    generated_count = 0

    for idx in range(args.count):
        fmt = (
            FORMATS[(len(used_topics) + idx) % len(FORMATS)]
            if args.format == "Auto"
            else args.format
        )
        log.info("Generating short #%d with format='%s' via %s...", idx + 1, fmt, HORMOUZ_CHAT_MODEL)

        topic_key, core_message, proverb_fa, proverb_en, hook_en, disp, tts, scenes_out = call_llm(
            api_key, fmt, used_topics, num_scenes
        )

        if topic_key.lower() in used_lower:
            topic_key = f"{topic_key}_{_next_numeric_id(existing_ids)}"

        target_id = _next_numeric_id(existing_ids)
        existing_ids.append(target_id)
        used_lower.add(topic_key.lower())

        scene_descs = [s["desc"] for s in scenes_out]

        new_script = {
            "id": int(target_id),
            "category": fmt,
            "core_message": core_message,
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
                for s_i, sc in enumerate(scenes_out)
            ],
        }

        if not args.dry_run:
            scripts.append(new_script)
            plan_shorts.append(new_plan_entry)
            used_topics.append(topic_key)

        generated_count += 1
        log.info("Prepared short id=%s (%s): %s | hook=%r", target_id, fmt, topic_key, hook_en)

    if not args.dry_run and generated_count > 0:
        _backup_if_exists(INPUT_JSON, args.dry_run)
        _backup_if_exists(PLAN_JSON, args.dry_run)
        _atomic_write_text(INPUT_JSON, json.dumps(scripts, ensure_ascii=False, indent=2) + "\n")
        plan_data["shorts"] = plan_shorts
        _atomic_write_text(PLAN_JSON, json.dumps(plan_data, ensure_ascii=False, indent=2) + "\n")
        save_used_topics(used_topics, args.dry_run)
        log.info("Saved %d new script(s) to %s and %s.", generated_count, INPUT_JSON.name, PLAN_JSON.name)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
