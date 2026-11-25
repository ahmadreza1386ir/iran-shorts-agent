#!/usr/bin/env python3
"""
Phase 1: High-retention hook + tightly matched visual plan generator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
from pathlib import Path
from typing import Any

from common import INPUT_JSON, PLAN_JSON, safe_script_id, setup_logging

log = setup_logging("proverb_gen")

MOTIONS = ["push_in", "pan_right", "drift_up", "pull_out", "pan_left", "drift_down"]

PROVERB_BANK: list[dict[str, Any]] = [
    {
        "proverb_fa": "آب که سر بالا بره، قورباغه ابوعطا می‌خونه",
        "proverb_en": "Ab ke sar bala barad, ghorbagheh Abu Ata mikhanad",
        "category": "Proverb",
        "theme": "stream_confidence",
        "hook_en": "Why does the smallest voice suddenly act like a king the second pressure disappears?",
        "narration": (
            "Why does the smallest voice suddenly act like a king the second pressure disappears? "
            "In Iran, people expose that exact attitude with one sharp line: "
            "Ab ke sar bala barad, ghorbagheh Abu Ata mikhanad. "
            "When water rises high, even a tiny frog starts singing a classical melody. "
            "Look at a quiet stream at dusk. Low water means silence and discipline. "
            "The moment fresh water swells the stream, a tiny frog hops onto a stone and croaks like a master. "
            "We have all seen someone act big only because conditions got easy. "
            "Sudden comfort or a little attention inflates weak confidence fast. "
            "Stay grounded—let real action speak long before the spotlight hits."
        ),
    },
    {
        "proverb_fa": "قطره قطره جمع گردد وانگهی دریا شود",
        "proverb_en": "Ghatreh ghatreh jam gardad van gahi darya shavad",
        "category": "Proverb",
        "theme": "drops_to_ocean",
        "hook_en": "One tiny habit looks laughably small on day one—until a year later it owns the room.",
        "narration": (
            "One tiny habit looks laughably small on day one—until a year later it owns the room. "
            "A timeless Persian verse says: Ghatreh ghatreh jam gardad van gahi darya shavad. "
            "Drop by drop it gathers, and in time becomes an ocean. "
            "A single grain of wheat in your palm looks like almost nothing. "
            "One cold night freezing a shallow pool looks minor. "
            "Yet repeated small steps compound into unstoppable momentum. "
            "You do not need a dramatic leap today. "
            "Show up, keep the pace steady, and let consistency do the heavy lifting."
        ),
    },
    {
        "proverb_fa": "علاج واقعه قبل از وقوع باید کرد",
        "proverb_en": "Elaj-e vaghe'eh ghabl az voghu bayad kard",
        "category": "Proverb",
        "theme": "early_preparation",
        "hook_en": "Cleaning up a disaster after it hits costs ten times more than stopping it early.",
        "narration": (
            "Cleaning up a disaster after it hits costs ten times more than stopping it early. "
            "An old Persian rule puts it bluntly: Elaj-e vaghe'eh ghabl az voghu bayad kard. "
            "Cure the problem before it ever happens. "
            "Desert builders did not wait for summer heat to figure out cooling. "
            "They packed winter ice deep underground while freezing cold was right outside. "
            "Preparation looks boring when everything is calm. "
            "Do the preventative work now while you still have control."
        ),
    },
    {
        "proverb_fa": "نابرده رنج گنج میسر نمی‌شود",
        "proverb_en": "Nabardeh ranj ganj moyassar nemishavad",
        "category": "Proverb",
        "theme": "patient_effort",
        "hook_en": "Everyone wants the treasure at the end, yet almost nobody wants the climb.",
        "narration": (
            "Everyone wants the treasure at the end, yet almost nobody wants the climb. "
            "Persian wisdom reminds us: Nabardeh ranj ganj moyassar nemishavad. "
            "No real treasure comes without toil. "
            "A grain sack never fills itself while you only wish for harvest. "
            "Every step through the bazaar and every measure on the scale takes real work. "
            "Shortcuts break the moment real weight is tested. "
            "Put in honest effort today so your results actually hold."
        ),
    },
]

CONCEPT_MAP: dict[str, list[tuple[str, str, list[str], str, list[list[Any]]]]] = {
    "frog": [
        ("frog_on_rock", "frog rock pond", ["statue", "sculpture", "castle", "flower", "flowers"], "story_sunset", [["sun", {"x": 0.65, "y": 0.24, "r": 0.085}], ["water", {"y0": 0.54}], ["frog_on_stone", {"x": 0.50, "y": 0.67, "s": 0.27}], ["reeds", {"side": "right", "y": 0.60, "n": 6}]]),
        ("pond_frog_water", "frog water lily", ["statue", "sculpture", "castle", "flower", "flowers"], "bazaar_gold", [["clouds", {"y": 0.20, "n": 3}], ["water", {"y0": 0.56}], ["frog_on_stone", {"x": 0.44, "y": 0.66, "s": 0.25}], ["reeds", {"side": "left", "y": 0.61, "n": 6}]]),
    ],
    "stream": [
        ("flowing_river", "river stream water", ["ice", "blocks", "castle", "flower", "flowers"], "story_sunset", [["clouds", {"y": 0.20, "n": 3}], ["sun", {"x": 0.30, "y": 0.27, "r": 0.095}], ["water", {"y0": 0.54}], ["ripples", {"x": 0.50, "y": 0.68, "n": 6, "r": 0.12}]]),
        ("water_ripples", "water ripples stream", ["ice", "blocks", "castle", "flower", "flowers"], "story_sunset", [["sun", {"x": 0.50, "y": 0.28, "r": 0.09}], ["water", {"y0": 0.58}], ["ripples", {"x": 0.45, "y": 0.70, "n": 4, "r": 0.09}]]),
    ],
    "grain": [
        ("wheat_grains", "wheat grain harvest", ["castle", "flower", "flowers", "ice"], "bazaar_gold", [["sun", {"x": 0.36, "y": 0.24, "r": 0.09}], ["hand", {"x": 0.50, "y": 0.52, "s": 0.34, "grains": True, "action": "scoop"}]]),
        ("wheat_field", "golden wheat field", ["castle", "flower", "flowers", "ice"], "bazaar_gold", [["clouds", {"y": 0.20, "n": 3}], ["sun", {"x": 0.50, "y": 0.24, "r": 0.095}], ["ground", {"y": 0.72}], ["wheat", {"n": 15, "y": 0.96, "h": 0.32}]]),
    ],
    "ice": [
        ("frozen_water", "frozen pond winter", ["castle", "flower", "flowers"], "night_ice", [["stars", {"n": 115}], ["moon", {"x": 0.74, "y": 0.16, "r": 0.06}], ["ground", {"y": 0.66}], ["pools", {"y": 0.76}]]),
        ("winter_ice", "winter ice frost", ["castle", "flower", "flowers"], "night_ice", [["stars", {"n": 90}], ["ground", {"y": 0.68}], ["ice_blocks", {"x": 0.50, "y": 0.82, "cols": 3, "rows": 2, "s": 0.075}]]),
    ],
    "scale": [
        ("brass_balance", "balance scale brass", ["castle", "flower", "flowers", "ice"], "bazaar_gold", [["clouds", {"y": 0.19, "n": 3}], ["sun", {"x": 0.50, "y": 0.23, "r": 0.09}], ["scales", {"x": 0.50, "y": 0.78, "s": 0.48}]]),
        ("grain_sack", "burlap sack grain", ["castle", "flower", "flowers", "ice"], "bazaar_gold", [["sun", {"x": 0.50, "y": 0.25, "r": 0.09}], ["sack", {"x": 0.50, "y": 0.88, "w": 0.72}], ["grain_fall", {"x": 0.50, "y1": 0.50, "y2": 0.78, "n": 40}]]),
    ],
    "dusk": [
        ("evening_lantern", "lantern night light", ["castle", "flower", "flowers", "ice"], "cool_dusk", [["stars", {"n": 85}], ["ground", {"y": 0.74}], ["lantern", {"x": 0.52, "y": 0.45, "s": 0.24}]]),
        ("calm_horizon", "sunset river horizon", ["castle", "flower", "flowers", "ice"], "cool_dusk", [["stars", {"n": 80}], ["moon", {"x": 0.26, "y": 0.18, "r": 0.065}], ["ground", {"y": 0.74}], ["figure", {"x": 0.50, "y": 0.85, "h": 0.43, "facing": 1, "walk": 0.14}]]),
    ],
}


def _split_into_exact_scenes(narration: str, target_scenes: int = 15) -> list[str]:
    raw_parts = [
        p.strip(" ,;:-—")
        for p in re.split(r"(?<=[.!?,;:])\s+|—|--", narration.strip())
        if p.strip(" ,;:-—")
    ]
    if not raw_parts:
        raw_parts = [narration.strip()]
    parts = list(raw_parts)
    while len(parts) < target_scenes:
        longest_idx = max(range(len(parts)), key=lambda i: len(parts[i].split()))
        words = parts[longest_idx].split()
        if len(words) < 4:
            parts.append(parts[longest_idx])
        else:
            mid = len(words) // 2
            parts[longest_idx : longest_idx + 1] = [" ".join(words[:mid]).strip(), " ".join(words[mid:]).strip()]
    while len(parts) > target_scenes:
        shortest_idx = min(range(len(parts) - 1), key=lambda i: len(parts[i]) + len(parts[i + 1]))
        parts[shortest_idx] = f"{parts[shortest_idx]} {parts[shortest_idx + 1]}".strip()
        parts.pop(shortest_idx + 1)
    return parts


def _anchor_from_segment(seg: str, idx: int) -> str | None:
    if idx == 0:
        return None
    words = re.findall(r"[A-Za-z0-9']+", seg)
    return " ".join(words[: min(3, len(words))]) if words else None


def _pick_concept(seg: str, theme: str, used_sigs: list[str], rng: random.Random) -> tuple[str, str, list[str], str, list[list[Any]]]:
    low = seg.lower()
    buckets: list[str] = []
    if any(w in low for w in ("frog", "ghorbagheh", "croaks", "singing", "melody")):
        buckets.append("frog")
    if any(w in low for w in ("water", "stream", "swells", "flow", "drop", "darya", "ocean", "pool")):
        buckets.append("stream")
    if any(w in low for w in ("wheat", "grain", "palm", "harvest", "habit", "momentum")):
        buckets.append("grain")
    if any(w in low for w in ("winter", "ice", "cold", "freezing", "heat")):
        buckets.append("ice")
    if any(w in low for w in ("sack", "scale", "bazaar", "weight", "treasure", "hardship", "reward")):
        buckets.append("scale")
    if not buckets:
        buckets = ["stream", "frog", "dusk"] if theme == "stream_confidence" else ["grain", "ice", "scale", "dusk"]

    cands: list[tuple[str, str, list[str], str, list[list[Any]]]] = []
    for b in buckets:
        cands.extend(CONCEPT_MAP.get(b, []))
    rng.shuffle(cands)
    recent = set(used_sigs[-3:])
    for c in cands:
        if c[0] not in recent:
            return c
    return cands[0]


def _compose_scene(seg: str, idx: int, theme: str, proverb_key: str, used_sigs: list[str]) -> tuple[dict[str, Any], str]:
    rng = random.Random(int(hashlib.sha256(f"{proverb_key}:{idx}:{seg}".encode("utf-8")).hexdigest()[:8], 16))
    sig, query, rejects, palette, layers = _pick_concept(seg, theme, used_sigs, rng)
    motion = MOTIONS[(idx + rng.randint(0, 2)) % len(MOTIONS)]
    return {
        "desc": f"[{sig} #{idx + 1}] {seg}",
        "query": query,
        "rejects": rejects,
        "anchor": _anchor_from_segment(seg, idx),
        "motion": motion,
        "prompt": f"{query}, {seg}, no text",
        "palette": palette,
        "layers": layers,
    }, sig


def _next_numeric_id(existing_ids: list[str]) -> str:
    nums = [int(x) for x in existing_ids if x.isdigit()]
    return str((max(nums) if nums else 0) + 1)


def _backup_if_exists(path: Path, dry_run: bool) -> None:
    if path.is_file() and not dry_run:
        shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate high-hook, tightly matched Persian proverb scripts & plans.")
    ap.add_argument("--count", type=int, default=2)
    ap.add_argument("--min-scenes", type=int, default=15)
    ap.add_argument("--replace", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    scripts: list[dict[str, Any]] = []
    if INPUT_JSON.is_file():
        loaded = json.loads(INPUT_JSON.read_text(encoding="utf-8-sig"))
        if isinstance(loaded, list):
            scripts = loaded

    existing_ids: list[str] = []
    existing_index_by_key: dict[str, int] = {}
    for idx, item in enumerate(scripts):
        if isinstance(item, dict):
            sid = safe_script_id(item.get("id"))
            if sid:
                existing_ids.append(sid)
            p_key = str(item.get("proverb_en") or item.get("hook_en") or "").strip().lower()
            if p_key:
                existing_index_by_key[p_key] = idx

    plan_data: dict[str, Any] = {"shorts": []}
    if PLAN_JSON.is_file():
        raw_plan = json.loads(PLAN_JSON.read_text(encoding="utf-8-sig"))
        if isinstance(raw_plan, dict) and isinstance(raw_plan.get("shorts"), list):
            plan_data = raw_plan
        elif isinstance(raw_plan, list):
            plan_data = {"shorts": raw_plan}

    plan_shorts: list[dict[str, Any]] = plan_data["shorts"]
    plan_index_by_id = {str(s.get("id")): i for i, s in enumerate(plan_shorts) if isinstance(s, dict) and s.get("id") is not None}

    touched = 0
    for entry in PROVERB_BANK:
        if touched >= max(0, args.count):
            break
        key = entry["proverb_en"].strip().lower()
        already_idx = existing_index_by_key.get(key)
        if already_idx is not None and not args.replace:
            continue

        target_id = (
            safe_script_id(scripts[already_idx].get("id")) or _next_numeric_id(existing_ids + list(plan_index_by_id))
            if (already_idx is not None and args.replace)
            else _next_numeric_id(existing_ids + list(plan_index_by_id))
        )
        if already_idx is None:
            existing_ids.append(target_id)

        segments = _split_into_exact_scenes(entry["narration"], target_scenes=max(15, args.min_scenes))
        built_scenes: list[dict[str, Any]] = []
        used_sigs: list[str] = []
        for i, seg in enumerate(segments):
            sc_obj, sig = _compose_scene(seg, i, entry.get("theme", "stream_confidence"), key, used_sigs)
            built_scenes.append(sc_obj)
            used_sigs.append(sig)

        scene_descs = [sc["desc"] for sc in built_scenes]
        new_script = {
            "id": int(target_id) if target_id.isdigit() else target_id,
            "category": entry["category"],
            "proverb_fa": entry["proverb_fa"],
            "proverb_en": entry["proverb_en"],
            "word_count": len(entry["narration"].split()),
            "hook_en": entry["hook_en"],
            "narration": entry["narration"],
            "narration_alt": entry["narration"],
            "narration_display": entry["narration"],
            "narration_tts": entry["narration"],
            "visual_scenes": scene_descs,
            "scenes": scene_descs,
        }
        new_plan_entry = {
            "id": target_id,
            "style": "coherent visual storytelling, no text, no watermark",
            "scenes": [
                {
                    "desc": sc["desc"],
                    "query": sc["query"],
                    "anchor": sc["anchor"],
                    "motion": sc["motion"],
                    "order": ["commons", "card"],
                    "commons": {
                        "queries": [sc["query"], " ".join(sc["query"].split()[:2])],
                        "reject": sc["rejects"] + ["map", "diagram", "logo", "cartoon", "clipart"],
                    },
                    "prompt": sc["prompt"],
                    "card": {"palette": sc["palette"], "layers": sc["layers"]},
                }
                for sc in built_scenes
            ],
        }

        if not args.dry_run:
            if already_idx is not None and args.replace:
                scripts[already_idx] = new_script
            else:
                scripts.append(new_script)
                existing_index_by_key[key] = len(scripts) - 1
            if target_id in plan_index_by_id:
                plan_shorts[plan_index_by_id[target_id]] = new_plan_entry
            else:
                plan_shorts.append(new_plan_entry)
                plan_index_by_id[target_id] = len(plan_shorts) - 1
        touched += 1

    if not args.dry_run and touched > 0:
        _backup_if_exists(INPUT_JSON, args.dry_run)
        _backup_if_exists(PLAN_JSON, args.dry_run)
        INPUT_JSON.write_text(json.dumps(scripts, ensure_ascii=False, indent=2), encoding="utf-8")
        plan_data["shorts"] = plan_shorts
        PLAN_JSON.write_text(json.dumps(plan_data, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Updated %s and %s (%d proverb(s) processed).", INPUT_JSON.name, PLAN_JSON.name, touched)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())