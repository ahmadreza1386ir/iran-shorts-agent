#!/usr/bin/env python3
"""
Phase 2: Generate narration audio and synchronized word-group subtitles.

Primary TTS engine: Hormouz-hosted `openai/gpt-4o-mini-tts` (with expressive
storytelling instructions). Automatic fallback: `edge-tts` so the pipeline
never stalls if the paid TTS endpoint is temporarily unreachable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import edge_tts

BASE_DIR = Path(__file__).resolve().parent

INPUT_FILE = BASE_DIR / "data" / "scripts_batch_1.json"
OUTPUT_DIR = BASE_DIR / "output" / "audio"

HORMOUZ_TTS_MODEL = (
    os.environ.get("HORMOUZ_TTS_MODEL") or "openai/gpt-4o-mini-tts"
).strip()
HORMOUZ_TTS_VOICE = (
    os.environ.get("HORMOUZ_TTS_VOICE") or "onyx"
).strip()
HORMOUZ_TTS_INSTRUCTIONS = (
    os.environ.get("HORMOUZ_TTS_INSTRUCTIONS")
    or (
        "Speak like a warm, grounded, experienced storyteller narrating an Iranian fable or proverb "
        "for a short video. Keep a smooth, calm, natural pace with subtle pauses at commas and periods. "
        "Avoid robotic monotone, harsh sibilants, or over-dramatic shouting."
    )
).strip()

EDGE_VOICE = (os.environ.get("TTS_VOICE") or "en-US-AndrewNeural").strip()
EDGE_RATE = (os.environ.get("TTS_RATE") or "+0%").strip()

MIN_WORDS_PER_SUBTITLE = 2
MAX_WORDS_PER_SUBTITLE = 3
MIN_SUBTITLE_DURATION = 0.38
MIN_EXPECTED_AUDIO_SECONDS = 3.0
TTS_MAX_ATTEMPTS = 3
TTS_STREAM_TIMEOUT_SECONDS = 75.0
TIMESTAMP_EPSILON = 0.001

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(message)s"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt="%H:%M:%S",
)

logger = logging.getLogger("voice_and_subs")


@dataclass(frozen=True)
class WordBoundary:
    offset_seconds: float
    duration_seconds: float
    text: str


@dataclass(frozen=True)
class Subtitle:
    start: float
    end: float
    text: str


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


def get_hormouz_api_key() -> str:
    return _env_first(("HORMOUZ_API_KEY", "GAPGPT_API_KEY"), "")


def extract_words(text: str) -> list[str]:
    return re.findall(
        r"[^\W_]+(?:['’][^\W_]+)*(?:[-‐‑–][^\W_]+(?:['’][^\W_]+)*)*",
        text,
        flags=re.UNICODE,
    )


def normalize_word(word: str) -> str:
    return re.sub(r"[^\w]", "", word.casefold(), flags=re.UNICODE)


def chunk_words(
    words: list[str],
    minimum: int = MIN_WORDS_PER_SUBTITLE,
    maximum: int = MAX_WORDS_PER_SUBTITLE,
) -> list[list[str]]:
    if not words:
        return []

    if minimum < 1 or maximum < minimum:
        raise ValueError("Invalid subtitle chunk size configuration.")

    chunks = [
        words[index : index + maximum]
        for index in range(0, len(words), maximum)
    ]

    if len(chunks) >= 2 and len(chunks[-1]) < minimum:
        combined_size = len(chunks[-2]) + len(chunks[-1])

        if combined_size <= maximum:
            chunks[-2].extend(chunks[-1])
            chunks.pop()
        else:
            while len(chunks[-1]) < minimum and len(chunks[-2]) > minimum:
                chunks[-1].insert(0, chunks[-2].pop())

    return chunks


def format_subtitle_text(words: list[str]) -> str:
    return " ".join(words).strip()


def probe_media_duration(audio_path: Path) -> Optional[float]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        val = float(proc.stdout.strip())
        return val if val > 0.1 else None
    except Exception:
        return None


def _looks_like_audio_bytes(data: bytes) -> bool:
    if not data or len(data) < 256:
        return False
    # MP3 ID3 tag or frame sync, WAV RIFF header, or OGG header
    if data[:3] == b"ID3" or data[:4] in (b"RIFF", b"OggS"):
        return True
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return True
    # Accept raw audio payload if it isn't JSON/HTML error text
    stripped = data.lstrip()[:20].lower()
    if stripped.startswith(b"{") or stripped.startswith(b"[") or stripped.startswith(b"<!doctype") or stripped.startswith(b"<html"):
        return False
    return True


def _download_audio_from_url(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "IranShortsAgent/TTS"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _extract_audio_bytes_from_json_payload(payload: dict[str, Any]) -> Optional[bytes]:
    import base64

    # Direct base64 audio field if returned
    for key in ("audio_base64", "audio", "content", "b64_json"):
        val = payload.get(key)
        if isinstance(val, str) and len(val) > 200 and not val.startswith("http"):
            try:
                decoded = base64.b64decode(val, validate=False)
                if _looks_like_audio_bytes(decoded):
                    return decoded
            except Exception:
                pass

    # URL inside output / data / result fields
    candidates: list[Any] = [
        payload.get("output"),
        payload.get("audio_url"),
        payload.get("url"),
    ]
    data_obj = payload.get("data")
    if isinstance(data_obj, dict):
        candidates.extend([data_obj.get("url"), data_obj.get("audio_url"), data_obj.get("output")])
    elif isinstance(data_obj, list) and data_obj:
        first = data_obj[0]
        if isinstance(first, dict):
            candidates.extend([first.get("url"), first.get("audio_url")])

    for cand in candidates:
        if isinstance(cand, str) and cand.startswith(("http://", "https://")):
            audio_bytes = _download_audio_from_url(cand)
            if _looks_like_audio_bytes(audio_bytes):
                return audio_bytes
        elif isinstance(cand, list):
            for item in cand:
                if isinstance(item, str) and item.startswith(("http://", "https://")):
                    audio_bytes = _download_audio_from_url(item)
                    if _looks_like_audio_bytes(audio_bytes):
                        return audio_bytes
                elif isinstance(item, dict):
                    u = item.get("url") or item.get("audio_url")
                    if isinstance(u, str) and u.startswith(("http://", "https://")):
                        audio_bytes = _download_audio_from_url(u)
                        if _looks_like_audio_bytes(audio_bytes):
                            return audio_bytes

    return None


def _synthesize_hormouz_speech_once(narration: str, api_key: str) -> bytes:
    base = HORMOUZ_BASE_URL.rstrip("/")
    speech_url = base if base.endswith("/audio/speech") else f"{base}/audio/speech"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "audio/mpeg, application/octet-stream, application/json",
        "User-Agent": "IranShortsAgent/TTS",
    }

    body_dict: dict[str, Any] = {
        "model": HORMOUZ_TTS_MODEL,
        "input": narration,
        "voice": HORMOUZ_TTS_VOICE,
        "response_format": "mp3",
    }
    if HORMOUZ_TTS_INSTRUCTIONS:
        body_dict["instructions"] = HORMOUZ_TTS_INSTRUCTIONS

    req = urllib.request.Request(
        speech_url,
        data=json.dumps(body_dict, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=75) as resp:
            raw = resp.read()
        if _looks_like_audio_bytes(raw):
            return raw
        try:
            parsed = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(parsed, dict):
                extracted = _extract_audio_bytes_from_json_payload(parsed)
                if extracted:
                    return extracted
        except Exception:
            pass
        raise RuntimeError("Hormouz /audio/speech returned non-audio payload.")
    except urllib.error.HTTPError as http_err:
        err_detail = ""
        try:
            err_detail = http_err.read().decode("utf-8", "replace")[:240]
        except Exception:
            pass
        if http_err.code not in (404, 405):
            raise RuntimeError(f"Hormouz TTS HTTP {http_err.code}: {err_detail}") from http_err

    # Fallback to /predictions endpoint format if /audio/speech isn't used for this model
    pred_url = base if base.endswith("/predictions") else f"{base}/predictions"
    pred_payload = {
        "model": HORMOUZ_TTS_MODEL,
        "input": {
            "text": narration,
            "input": narration,
            "voice": HORMOUZ_TTS_VOICE,
            "instructions": HORMOUZ_TTS_INSTRUCTIONS,
        },
    }
    pred_req = urllib.request.Request(
        pred_url,
        data=json.dumps(pred_payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(pred_req, timeout=75) as resp:
        pred_raw = resp.read()
    parsed_pred = json.loads(pred_raw.decode("utf-8", "replace"))
    if not isinstance(parsed_pred, dict):
        raise RuntimeError("Invalid JSON from Hormouz /predictions TTS.")

    # Poll if prediction is queued/processing
    pred_id = parsed_pred.get("id")
    status = str(parsed_pred.get("status") or "").lower()
    if pred_id and status in ("starting", "processing", "queued", "pending"):
        poll_url = f"{pred_url}/{pred_id}"
        for _ in range(25):
            time.sleep(1.5)
            poll_req = urllib.request.Request(
                poll_url,
                headers={"Authorization": f"Bearer {api_key}", "User-Agent": "IranShortsAgent/TTS"},
                method="GET",
            )
            with urllib.request.urlopen(poll_req, timeout=30) as presp:
                parsed_pred = json.loads(presp.read().decode("utf-8", "replace"))
            status = str(parsed_pred.get("status") or "").lower()
            if status in ("succeeded", "completed", "failed", "canceled", "error"):
                break

    if status in ("failed", "canceled", "error"):
        raise RuntimeError(f"Hormouz TTS prediction failed: {parsed_pred.get('error')}")

    extracted = _extract_audio_bytes_from_json_payload(parsed_pred)
    if not extracted:
        raise RuntimeError("No audio output found in Hormouz /predictions response.")
    return extracted


async def _try_hormouz_tts(narration: str, target_dir: Path) -> Optional[Path]:
    api_key = get_hormouz_api_key()
    if not api_key or not HORMOUZ_TTS_MODEL:
        return None

    target_dir.mkdir(parents=True, exist_ok=True)
    last_err: Optional[Exception] = None

    for attempt in range(1, TTS_MAX_ATTEMPTS + 1):
        try:
            audio_bytes = await asyncio.to_thread(
                _synthesize_hormouz_speech_once,
                narration,
                api_key,
            )
            with tempfile.NamedTemporaryFile(
                mode="wb",
                suffix=".mp3",
                prefix="hormouz_tts_",
                dir=str(target_dir),
                delete=False,
            ) as tmp_f:
                tmp_path = Path(tmp_f.name)
                tmp_f.write(audio_bytes)

            probed_dur = probe_media_duration(tmp_path)
            if probed_dur is None or probed_dur < MIN_EXPECTED_AUDIO_SECONDS:
                tmp_path.unlink(missing_ok=True)
                raise RuntimeError(f"Hormouz TTS audio too short or invalid ({probed_dur}).")

            logger.info(
                "Synthesized audio via Hormouz (%s, voice=%s, %.2fs).",
                HORMOUZ_TTS_MODEL,
                HORMOUZ_TTS_VOICE,
                probed_dur,
            )
            return tmp_path
        except Exception as exc:
            last_err = exc
            logger.warning(
                "Hormouz TTS attempt %d/%d failed (%s): %s",
                attempt,
                TTS_MAX_ATTEMPTS,
                HORMOUZ_TTS_MODEL,
                exc,
            )
            if attempt < TTS_MAX_ATTEMPTS:
                await asyncio.sleep(1.5 * attempt)

    logger.warning("Falling back to edge-tts (%s) due to Hormouz TTS failure: %s", EDGE_VOICE, last_err)
    return None


async def _stream_edge_tts_once(
    narration: str,
    target_dir: Path,
) -> tuple[Path, list[WordBoundary]]:
    target_dir.mkdir(parents=True, exist_ok=True)
    boundaries: list[WordBoundary] = []

    communicate = edge_tts.Communicate(
        text=narration,
        voice=EDGE_VOICE,
        rate=EDGE_RATE,
    )

    with tempfile.NamedTemporaryFile(
        mode="wb",
        suffix=".mp3",
        prefix="edge_tts_",
        dir=str(target_dir),
        delete=False,
    ) as temporary_file:
        temporary_path = Path(temporary_file.name)

    async def _consume() -> None:
        with temporary_path.open("wb") as audio_file:
            async for event in communicate.stream():
                event_type = event.get("type")

                if event_type == "audio":
                    data = event.get("data")
                    if data:
                        audio_file.write(data)

                elif event_type == "WordBoundary":
                    offset = event.get("offset")
                    duration = event.get("duration")
                    text = event.get("text", "")

                    if offset is None:
                        continue

                    offset_seconds = max(0.0, float(offset) / 10_000_000)
                    duration_seconds = (
                        max(0.0, float(duration) / 10_000_000)
                        if duration is not None
                        else 0.0
                    )

                    boundaries.append(
                        WordBoundary(
                            offset_seconds=offset_seconds,
                            duration_seconds=duration_seconds,
                            text=str(text),
                        )
                    )

    try:
        await asyncio.wait_for(_consume(), timeout=TTS_STREAM_TIMEOUT_SECONDS)

        if not temporary_path.exists() or temporary_path.stat().st_size == 0:
            raise RuntimeError("Edge TTS produced an empty audio file.")

        probed_dur = probe_media_duration(temporary_path)
        if probed_dur is not None and probed_dur < MIN_EXPECTED_AUDIO_SECONDS:
            raise RuntimeError(f"Edge TTS audio unexpectedly short ({probed_dur:.2f}s).")

        return temporary_path, boundaries
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


async def synthesize_audio(
    narration: str,
    audio_path: Path,
) -> tuple[list[WordBoundary], Optional[float]]:
    cleaned = re.sub(r"\s+", " ", narration).strip()
    if not cleaned:
        raise ValueError("narration_tts is empty.")

    audio_path.parent.mkdir(parents=True, exist_ok=True)

    hormouz_mp3 = await _try_hormouz_tts(cleaned, audio_path.parent)
    if hormouz_mp3 is not None:
        probed_dur = probe_media_duration(hormouz_mp3)
        hormouz_mp3.replace(audio_path)
        return [], probed_dur

    last_err: Optional[Exception] = None
    for attempt in range(1, TTS_MAX_ATTEMPTS + 1):
        try:
            temp_mp3, boundaries = await _stream_edge_tts_once(cleaned, audio_path.parent)
            probed_dur = probe_media_duration(temp_mp3)
            temp_mp3.replace(audio_path)
            return boundaries, probed_dur
        except Exception as exc:
            last_err = exc
            logger.warning(
                "Edge TTS attempt %d/%d failed for %s: %s",
                attempt,
                TTS_MAX_ATTEMPTS,
                audio_path.name,
                exc,
            )
            if attempt < TTS_MAX_ATTEMPTS:
                await asyncio.sleep(1.5 * attempt)

    assert last_err is not None
    raise RuntimeError(f"Failed to synthesize audio for {audio_path.name}: {last_err}") from last_err


def detect_speech_segments(audio_path: Path, total_duration: float) -> list[tuple[float, float]]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or total_duration <= 0.2:
        return [(0.0, max(total_duration, 0.2))]

    try:
        proc = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-nostats",
                "-i",
                str(audio_path),
                "-af",
                "silencedetect=noise=-34dB:d=0.18",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        stderr = proc.stderr or ""
        starts = [float(m) for m in re.findall(r"silence_start:\s*([0-9]+(?:\.[0-9]+)?)", stderr)]
        ends = [float(m) for m in re.findall(r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)", stderr)]

        silences: list[tuple[float, float]] = []
        for idx, s in enumerate(starts):
            e = ends[idx] if idx < len(ends) else total_duration
            s_clamped = max(0.0, min(total_duration, s))
            e_clamped = max(s_clamped, min(total_duration, e))
            if e_clamped - s_clamped >= 0.14:
                silences.append((s_clamped, e_clamped))

        if not silences:
            return [(0.0, total_duration)]

        speech: list[tuple[float, float]] = []
        cursor = 0.0
        for s_start, s_end in silences:
            if s_start - cursor >= 0.15:
                speech.append((cursor, s_start))
            cursor = max(cursor, s_end)

        if total_duration - cursor >= 0.15:
            speech.append((cursor, total_duration))

        return speech if speech else [(0.0, total_duration)]
    except Exception:
        return [(0.0, total_duration)]


def speech_paced_word_times(
    display_words: list[str],
    audio_path: Path,
    fallback_boundaries: list[WordBoundary],
    known_duration: Optional[float] = None,
) -> list[tuple[float, float]]:
    n_words = len(display_words)
    if n_words == 0:
        return []

    probed_dur = known_duration if known_duration is not None else probe_media_duration(audio_path)
    boundary_dur = max(
        (b.offset_seconds + max(b.duration_seconds, 0.12) for b in fallback_boundaries),
        default=0.0,
    )
    total_dur = probed_dur or boundary_dur or max(n_words * 0.36, 0.2)

    segments = detect_speech_segments(audio_path, total_dur)
    seg_durations = [max(0.05, e - s) for s, e in segments]
    total_speech_time = sum(seg_durations)

    if total_speech_time <= 0.1:
        return [
            (total_dur * i / n_words, total_dur * (i + 1) / n_words)
            for i in range(n_words)
        ]

    weights = [max(2.0, float(len(normalize_word(w)) or len(w))) for w in display_words]
    total_weight = sum(weights)

    def speech_time_to_real_time(st: float) -> float:
        st_clamped = max(0.0, min(total_speech_time, st))
        acc = 0.0
        for (seg_s, seg_e), seg_len in zip(segments, seg_durations):
            if st_clamped <= acc + seg_len:
                return seg_s + (st_clamped - acc)
            acc += seg_len
        return segments[-1][1]

    times: list[tuple[float, float]] = []
    w_cursor = 0.0
    for w in weights:
        s_t0 = (w_cursor / total_weight) * total_speech_time
        w_cursor += w
        s_t1 = (w_cursor / total_weight) * total_speech_time
        t0 = speech_time_to_real_time(s_t0)
        t1 = max(t0 + TIMESTAMP_EPSILON, speech_time_to_real_time(s_t1))
        times.append((t0, t1))

    return times


def infer_audio_duration(boundaries: list[WordBoundary]) -> float:
    if not boundaries:
        return 0.0
    return max(
        boundary.offset_seconds + boundary.duration_seconds
        for boundary in boundaries
    )


def boundary_word_end(boundary: WordBoundary) -> float:
    end = boundary.offset_seconds + boundary.duration_seconds
    if end <= boundary.offset_seconds:
        end = boundary.offset_seconds + 0.12
    return end


def proportional_word_times(
    word_count: int,
    duration: float,
) -> list[tuple[float, float]]:
    if word_count <= 0:
        return []
    duration = max(duration, word_count * 0.12)
    duration = max(duration, 0.1)
    return [
        (
            duration * index / word_count,
            duration * (index + 1) / word_count,
        )
        for index in range(word_count)
    ]


def align_display_words(
    display_words: list[str],
    boundaries: list[WordBoundary],
    audio_path: Optional[Path] = None,
    known_duration: Optional[float] = None,
) -> list[tuple[float, float]]:
    if not display_words:
        return []

    if not boundaries:
        if audio_path is not None and audio_path.is_file():
            return speech_paced_word_times(
                display_words,
                audio_path,
                boundaries,
                known_duration=known_duration,
            )
        return proportional_word_times(
            len(display_words),
            known_duration or max(len(display_words) * 0.35, 0.1),
        )

    ordered_boundaries = sorted(
        boundaries,
        key=lambda boundary: boundary.offset_seconds,
    )

    boundary_times = [
        (
            boundary.offset_seconds,
            max(
                boundary.offset_seconds + TIMESTAMP_EPSILON,
                boundary_word_end(boundary),
            ),
        )
        for boundary in ordered_boundaries
    ]

    if len(display_words) == len(ordered_boundaries):
        return boundary_times

    display_normalized = [normalize_word(word) for word in display_words]
    tts_normalized = [normalize_word(boundary.text) for boundary in ordered_boundaries]

    anchors: dict[int, int] = {}
    tts_index = 0
    matched = 0

    for display_index, display_word in enumerate(display_normalized):
        if not display_word:
            continue

        best_index: Optional[int] = None

        for candidate_index in range(tts_index, len(tts_normalized)):
            candidate = tts_normalized[candidate_index]
            if not candidate:
                continue

            if display_word == candidate:
                best_index = candidate_index
                break

            if (
                len(display_word) >= 5
                and len(candidate) >= 5
                and (display_word in candidate or candidate in display_word)
            ):
                best_index = candidate_index
                break

        if best_index is not None:
            anchors[display_index] = best_index
            tts_index = best_index + 1
            matched += 1

    minimum_matches = max(1, len(display_words) // 4)

    if matched < minimum_matches:
        if audio_path is not None and audio_path.is_file():
            return speech_paced_word_times(
                display_words,
                audio_path,
                ordered_boundaries,
                known_duration=known_duration,
            )
        estimated_duration = known_duration or infer_audio_duration(ordered_boundaries)
        if estimated_duration <= 0:
            estimated_duration = max(len(display_words) * 0.35, 0.1)
        return proportional_word_times(len(display_words), estimated_duration)

    anchor_times: dict[int, tuple[float, float]] = {}
    for display_index, boundary_index in anchors.items():
        anchor_times[display_index] = boundary_times[boundary_index]

    last_end = max(
        boundary_times[-1][1],
        boundary_times[-1][0] + TIMESTAMP_EPSILON,
    )
    if known_duration is not None and known_duration > last_end:
        last_end = known_duration

    first_anchor = min(anchor_times)
    if first_anchor > 0:
        anchor_start = anchor_times[first_anchor][0]
        count = first_anchor
        interval = max(anchor_start, 0.0) / count
        for index in range(first_anchor):
            start = interval * index
            end = interval * (index + 1)
            anchor_times[index] = (start, max(end, start + TIMESTAMP_EPSILON))

    sorted_anchor_indices = sorted(anchors)
    for left_index, right_index in zip(
        sorted_anchor_indices,
        sorted_anchor_indices[1:],
    ):
        if right_index - left_index <= 1:
            continue

        left_end = anchor_times[left_index][1]
        right_start = anchor_times[right_index][0]
        gap = max(0.0, right_start - left_end)
        missing_count = right_index - left_index - 1
        interval = gap / (missing_count + 1)

        for step in range(1, missing_count + 1):
            index = left_index + step
            start = left_end + interval * (step - 1)
            end = left_end + interval * step
            if end <= start:
                end = start + TIMESTAMP_EPSILON
            anchor_times[index] = (start, end)

    last_anchor = max(anchor_times)
    if last_anchor < len(display_words) - 1:
        start_time = anchor_times[last_anchor][1]
        remaining = len(display_words) - last_anchor - 1
        available = max(0.0, last_end - start_time)
        interval = available / remaining if remaining else 0.0

        for index in range(last_anchor + 1, len(display_words)):
            start = start_time + interval * (index - last_anchor - 1)
            end = start_time + interval * (index - last_anchor)
            if end <= start:
                end = start + TIMESTAMP_EPSILON
            anchor_times[index] = (start, end)

    result: list[tuple[float, float]] = []
    for index in range(len(display_words)):
        interval = anchor_times.get(index)
        if interval is None:
            interval = proportional_word_times(
                len(display_words),
                max(last_end, len(display_words) * 0.35),
            )[index]

        start, end = interval
        start = max(0.0, start)
        end = max(start + TIMESTAMP_EPSILON, end)
        result.append((start, end))

    return result


def build_subtitles(
    display_words: list[str],
    word_times: list[tuple[float, float]],
    max_duration: Optional[float] = None,
) -> list[Subtitle]:
    if not display_words or not word_times:
        return []

    chunks = chunk_words(display_words)
    subtitles: list[Subtitle] = []

    word_index = 0
    previous_end = 0.0
    cap_end = max_duration if (max_duration is not None and max_duration > 0.2) else None

    for chunk in chunks:
        chunk_length = len(chunk)
        raw_start = word_times[word_index][0]
        raw_end = word_times[word_index + chunk_length - 1][1]

        start = max(0.0, raw_start, previous_end)
        if cap_end is not None:
            start = min(start, max(0.0, cap_end - TIMESTAMP_EPSILON))

        end = max(raw_end, start + MIN_SUBTITLE_DURATION)
        if cap_end is not None:
            end = min(end, cap_end)
            end = max(end, start + TIMESTAMP_EPSILON)

        subtitles.append(
            Subtitle(
                start=start,
                end=end,
                text=format_subtitle_text(chunk),
            )
        )

        previous_end = end
        word_index += chunk_length

    return subtitles


def format_srt_timestamp(seconds: float) -> str:
    total_milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def write_srt(
    subtitles: list[Subtitle],
    srt_path: Path,
) -> None:
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    blocks: list[str] = []

    for index, subtitle in enumerate(subtitles, start=1):
        start = subtitle.start
        end = max(subtitle.end, start + TIMESTAMP_EPSILON)
        blocks.append(
            f"{index}\n"
            f"{format_srt_timestamp(start)} --> "
            f"{format_srt_timestamp(end)}\n"
            f"{subtitle.text}\n"
        )

    content = "\n".join(blocks)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        suffix=".srt",
        prefix="subtitle_",
        dir=str(srt_path.parent),
        delete=False,
    ) as temporary_file:
        temporary_path = Path(temporary_file.name)
        try:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    try:
        temporary_path.replace(srt_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def load_scripts(input_path: Path) -> list[dict[str, Any]]:
    if not input_path.exists():
        raise FileNotFoundError(f"Input JSON file not found: {input_path}")

    try:
        with input_path.open("r", encoding="utf-8-sig") as json_file:
            data = json.load(json_file)
    except Exception as exc:
        raise ValueError(f"Invalid JSON in {input_path}: {exc}") from exc

    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get("shorts"), list):
        return [item for item in data["shorts"] if isinstance(item, dict)]

    raise ValueError(f"Expected a list of script objects in {input_path}")


def _resolve_texts(item: dict[str, Any]) -> tuple[str, str]:
    display_text = str(
        item.get("narration_display")
        or item.get("narration")
        or item.get("narration_tts")
        or ""
    ).strip()
    tts_text = str(
        item.get("narration_tts")
        or item.get("narration")
        or item.get("narration_display")
        or ""
    ).strip()
    return display_text, tts_text


async def process_item(item: dict[str, Any], out_dir: Path, force: bool = False) -> None:
    raw_id = item.get("id")
    if raw_id is None or str(raw_id).strip() == "":
        raise ValueError("Script item is missing 'id'.")
    sid = str(raw_id).strip()

    display_text, tts_text = _resolve_texts(item)
    if not tts_text:
        raise ValueError(f"Short {sid} has empty narration text.")
    if not display_text:
        display_text = tts_text

    display_words = extract_words(display_text)
    if not display_words:
        raise ValueError(f"Short {sid} has no extractable display words.")

    audio_path = out_dir / f"short_{sid}.mp3"
    srt_path = out_dir / f"short_{sid}.srt"

    if not force and audio_path.is_file() and audio_path.stat().st_size > 0 and srt_path.is_file():
        existing_dur = probe_media_duration(audio_path)
        if existing_dur is not None and existing_dur >= MIN_EXPECTED_AUDIO_SECONDS:
            logger.info("Short %s already has audio and subtitles; skipping.", sid)
            return

    boundaries, probed_dur = await synthesize_audio(tts_text, audio_path)
    final_dur = probed_dur
    if audio_path is not None and audio_path.is_file() and final_dur is None:
        final_dur = probe_media_duration(audio_path)

    word_times = align_display_words(
        display_words=display_words,
        boundaries=boundaries,
        audio_path=audio_path,
        known_duration=final_dur,
    )
    subtitles = build_subtitles(
        display_words=display_words,
        word_times=word_times,
        max_duration=final_dur,
    )
    write_srt(subtitles, srt_path)
    logger.info(
        "Short %s ready: audio=%s (%.2fs), subs=%d cues -> %s",
        sid,
        audio_path.name,
        final_dur or 0.0,
        len(subtitles),
        srt_path.name,
    )


def _select_items(items: list[dict[str, Any]], target_id: Optional[str]) -> list[dict[str, Any]]:
    if not target_id:
        return items
    wanted = target_id.strip()
    return [item for item in items if str(item.get("id", "")).strip() == wanted]


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate voiceover audio and SRT subtitles.")
    parser.add_argument("--input", type=Path, default=INPUT_FILE, help="Path to scripts JSON")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR, help="Output directory for audio & SRT")
    parser.add_argument("--id", type=str, default=None, help="Process only a specific short ID")
    parser.add_argument("--only", type=str, default=None, help="Alias for --id (process only a specific short ID)")
    parser.add_argument("--force", action="store_true", help="Regenerate even if output already exists")
    return parser.parse_args(argv)


async def _run_async(args: argparse.Namespace) -> int:
    items = load_scripts(args.input)
    target_id = args.id or args.only
    selected = _select_items(items, target_id)
    if not selected:
        logger.error("No matching script items found (id=%r).", target_id)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ok = 0
    for item in selected:
        try:
            await process_item(item, args.output_dir, force=args.force)
            ok += 1
        except Exception as exc:
            logger.error("Failed processing short id=%r: %s", item.get("id"), exc)

    return 0 if ok > 0 else 1


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    return asyncio.run(_run_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
