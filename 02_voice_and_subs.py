#!/usr/bin/env python3
"""
Phase 2: Generate narration audio and synchronized word-group subtitles.

Aligns subtitle chunks to real speech segments (detected via FFmpeg silence
detection) whenever WordBoundary events are unavailable, preventing subtitles
from drifting ahead of pauses or pace changes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import edge_tts

BASE_DIR = Path(__file__).resolve().parent

INPUT_FILE = BASE_DIR / "data" / "scripts_batch_1.json"
OUTPUT_DIR = BASE_DIR / "output" / "audio"

VOICE = "en-US-BrianNeural"
RATE = "+0%"

MIN_WORDS_PER_SUBTITLE = 3
MAX_WORDS_PER_SUBTITLE = 5
MIN_SUBTITLE_DURATION = 0.45
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
        words[index:index + maximum]
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


async def synthesize_audio(
    narration: str,
    audio_path: Path,
) -> list[WordBoundary]:
    if not narration.strip():
        raise ValueError("narration_tts is empty.")

    boundaries: list[WordBoundary] = []

    communicate = edge_tts.Communicate(
        text=narration,
        voice=VOICE,
        rate=RATE,
    )

    with tempfile.NamedTemporaryFile(
        mode="wb",
        suffix=".mp3",
        prefix="edge_tts_",
        dir=str(audio_path.parent),
        delete=False,
    ) as temporary_file:
        temporary_path = Path(temporary_file.name)

    try:
        with temporary_path.open("wb") as audio_file:
            async for event in communicate.stream():
                event_type = event.get("type")

                if event_type == "audio":
                    audio_file.write(event["data"])

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

        if not temporary_path.exists() or temporary_path.stat().st_size == 0:
            raise RuntimeError("Edge TTS produced an empty audio file.")

        temporary_path.replace(audio_path)

    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    return boundaries


def probe_media_duration(audio_path: Path) -> Optional[float]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
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


def detect_speech_segments(audio_path: Path, total_duration: float) -> list[tuple[float, float]]:
    """Detect non-silent speech intervals inside the generated audio via FFmpeg silencedetect."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or total_duration <= 0.2:
        return [(0.0, max(total_duration, 0.2))]

    try:
        proc = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-nostats",
                "-i", str(audio_path),
                "-af", "silencedetect=noise=-34dB:d=0.18",
                "-f", "null",
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
) -> list[tuple[float, float]]:
    """Distribute word intervals across actual spoken segments so pauses keep pace with narration."""
    n_words = len(display_words)
    if n_words == 0:
        return []

    probed_dur = probe_media_duration(audio_path)
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

    # Weight words slightly by character length so longer words get natural airtime.
    weights = [max(2.0, float(len(normalize_word(w)) or len(w))) for w in display_words]
    total_weight = sum(weights)

    # Assign each word a target position inside total_speech_time, then map through speech segments.
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
) -> list[tuple[float, float]]:
    if not display_words:
        return []

    if not boundaries:
        if audio_path is not None and audio_path.is_file():
            return speech_paced_word_times(display_words, audio_path, boundaries)
        return proportional_word_times(
            len(display_words),
            max(len(display_words) * 0.35, 0.1),
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
            return speech_paced_word_times(display_words, audio_path, ordered_boundaries)
        estimated_duration = infer_audio_duration(ordered_boundaries)
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
) -> list[Subtitle]:
    if not display_words or not word_times:
        return []

    chunks = chunk_words(display_words)
    subtitles: list[Subtitle] = []

    word_index = 0
    previous_end = 0.0

    for chunk in chunks:
        chunk_length = len(chunk)
        start = word_times[word_index][0]
        end = word_times[word_index + chunk_length - 1][1]

        start = max(0.0, start, previous_end)
        end = max(end, start + MIN_SUBTITLE_DURATION)

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
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {input_path}: {exc}") from exc

    if not isinstance(data, list):
        raise ValueError("The input JSON must contain an array of script objects.")

    validated: list[dict[str, Any]] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            logger.warning("Skipping item %d: expected a JSON object.", index)
            continue

        missing_fields = [
            field
            for field in ("id", "narration_display", "narration_tts")
            if field not in item
        ]
        if missing_fields:
            logger.warning(
                "Skipping item %d: missing fields %s.",
                index,
                ", ".join(missing_fields),
            )
            continue

        if item["id"] is None or isinstance(item["id"], (dict, list)):
            logger.warning("Skipping item %d: invalid id.", index)
            continue

        if not isinstance(item["narration_display"], str):
            logger.warning("Skipping item %s: narration_display must be a string.", item["id"])
            continue

        if not isinstance(item["narration_tts"], str):
            logger.warning("Skipping item %s: narration_tts must be a string.", item["id"])
            continue

        validated.append(item)

    safe_items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for item in validated:
        item_id = str(item["id"]).strip()
        if not item_id:
            logger.warning("Skipping item with an empty id.")
            continue

        if not re.fullmatch(r"[A-Za-z0-9_-]+", item_id):
            logger.warning(
                "Skipping item with unsafe id %r. Use letters, digits, underscores, or hyphens.",
                item_id,
            )
            continue

        if item_id in seen_ids:
            logger.warning("Skipping duplicate script id %r.", item_id)
            continue

        seen_ids.add(item_id)
        item["_output_id"] = item_id
        safe_items.append(item)

    return safe_items


async def process_item(item: dict[str, Any], force: bool = False) -> bool:
    item_id = item["_output_id"]
    narration_tts = item["narration_tts"].strip()
    narration_display = item["narration_display"].strip()

    audio_path = OUTPUT_DIR / f"short_{item_id}.mp3"
    srt_path = OUTPUT_DIR / f"short_{item_id}.srt"

    if not force and audio_path.is_file() and srt_path.is_file() and audio_path.stat().st_size > 0:
        logger.info("[%s] Audio and SRT already exist (use --force to regenerate).", item_id)
        return True

    if not narration_tts:
        logger.error("[%s] Skipping: narration_tts is empty.", item_id)
        return False

    if not narration_display:
        logger.error("[%s] Skipping: narration_display is empty.", item_id)
        return False

    logger.info("[%s] Synthesizing audio with voice %s...", item_id, VOICE)

    try:
        boundaries = await synthesize_audio(
            narration=narration_tts,
            audio_path=audio_path,
        )

        logger.info(
            "[%s] Audio saved: %s (%d word boundaries)",
            item_id,
            audio_path.relative_to(BASE_DIR),
            len(boundaries),
        )

        display_words = extract_words(narration_display)
        if not display_words:
            logger.error("[%s] No subtitle words found in narration_display.", item_id)
            return False

        word_times = align_display_words(
            display_words=display_words,
            boundaries=boundaries,
            audio_path=audio_path,
        )

        subtitles = build_subtitles(
            display_words=display_words,
            word_times=word_times,
        )

        write_srt(
            subtitles=subtitles,
            srt_path=srt_path,
        )

        logger.info(
            "[%s] Subtitles saved: %s (%d entries)",
            item_id,
            srt_path.relative_to(BASE_DIR),
            len(subtitles),
        )

        return True

    except Exception:
        logger.exception("[%s] Failed to generate audio or subtitles.", item_id)
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 2: voice and subtitles.")
    parser.add_argument("--only", help="Process only this short id (e.g. 1, 2, 3)")
    parser.add_argument("--force", action="store_true", help="Regenerate even if output files already exist")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    logger.info("Starting Phase 2: voice and subtitles.")
    logger.info("Input: %s", INPUT_FILE)
    logger.info("Output directory: %s", OUTPUT_DIR)
    logger.info("Voice: %s | Rate: %s", VOICE, RATE)

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        scripts = load_scripts(INPUT_FILE)
    except (OSError, ValueError, FileNotFoundError) as exc:
        logger.error("%s", exc)
        return 1

    if args.only:
        scripts = [s for s in scripts if s["_output_id"] == args.only.strip()]

    if not scripts:
        logger.warning("No valid script items found. Nothing to process.")
        return 0

    total = len(scripts)
    successful = 0
    failed = 0

    for index, item in enumerate(scripts, start=1):
        logger.info("Processing item %d/%d (id=%s).", index, total, item["_output_id"])
        if await process_item(item, force=args.force):
            successful += 1
        else:
            failed += 1

    logger.info("=" * 60)
    logger.info("Phase 2 completed.")
    logger.info("Total: %d | Successful: %d | Failed: %d", total, successful, failed)
    logger.info("Output directory: %s", OUTPUT_DIR)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        exit_code = 130
    except Exception:
        logger.exception("Unexpected fatal error.")
        exit_code = 1

    sys.exit(exit_code)