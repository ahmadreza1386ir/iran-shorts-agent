#!/usr/bin/env python3
"""Phase 4: Approved YouTube Shorts uploader."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from common import BASE_DIR, INPUT_JSON, OUTPUT_DIR, safe_script_id, setup_logging

log = setup_logging("yt_uploader")
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
UPLOADED_LOG_FILE = BASE_DIR / "data" / "uploaded_shorts.json"
APPROVED_DIR = BASE_DIR / "output" / "approved"

def _load_uploaded_log() -> dict[str, Any]:
    if not UPLOADED_LOG_FILE.is_file():
        return {"uploaded": {}}
    try:
        data = json.loads(UPLOADED_LOG_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("uploaded"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"uploaded": {}}

def _save_uploaded_log(data: dict[str, Any]) -> None:
    UPLOADED_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    UPLOADED_LOG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def _find_script_meta(sid: str) -> dict[str, Any]:
    if not INPUT_JSON.is_file():
        return {}
    try:
        raw = json.loads(INPUT_JSON.read_text(encoding="utf-8-sig"))
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and safe_script_id(item.get("id")) == sid:
                    return item
    except (OSError, ValueError):
        pass
    return {}

def _clean_tags(raw_tags: list[str]) -> list[str]:
    out, seen, total = [], set(), 0
    for t in raw_tags:
        c = re.sub(r"[<>]", "", str(t or "")).strip()[:30]
        k = c.lower()
        if not c or k in seen:
            continue
        if total + len(c) + 1 > 450:
            break
        seen.add(k)
        out.append(c)
        total += len(c) + 1
    return out

def _build_metadata(sid: str, meta: dict[str, Any]) -> tuple[str, str, list[str]]:
    hook = str(meta.get("hook_en") or "").strip()
    proverb_en = str(meta.get("proverb_en") or "").strip()
    proverb_fa = str(meta.get("proverb_fa") or "").strip()
    category = str(meta.get("category") or "Iranian Wisdom").strip()
    narration = str(meta.get("narration_display") or meta.get("narration") or "").strip()

    raw_title = hook or proverb_en or f"Iranian {category} #{sid}"
    title = raw_title[:95] if "#shorts" in raw_title.lower() else f"{raw_title[:86].rstrip(^)} #Shorts"

    lines = []
    if proverb_fa:
        lines.append(f"Persian: {proverb_fa}")
    if proverb_en:
        lines.append(f"Phrase / Theme: {proverb_en}")
    if narration:
        lines.extend(["", narration[:500]])
    lines.extend(["", "#Shorts #Iran #PersianWisdom #Storytelling #History"])
    tags = _clean_tags(["Shorts", "YouTube Shorts", "Iran", "Iranian culture", "Persian proverb", "Iranian history", "storytelling", category, proverb_en])
    return title, "\n".join(lines).strip()[:4900], tags

def _get_youtube_service(client_secrets_path: Path, token_path: Path):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not client_secrets_path.is_file():
                raise FileNotFoundError(f"OAuth client file not found: {client_secrets_path}")
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds)

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--approve", action="store_true")
    ap.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private")
    ap.add_argument("--client-secrets", default=str(BASE_DIR / "client_secret.json"))
    ap.add_argument("--token", default=str(BASE_DIR / "token.json"))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sid = safe_script_id(args.id)
    if not sid:
        log.error("Invalid short id: r", args.id)
        return 1

    store = _load_uploaded_log()
    if sid in store["uploaded"] and not args.force and not args.dry_run:
        log.info("[s] Already uploaded.", sid)
        return 0

    pending_vid = OUTPUT_DIR / f"short_{sid}.mp4"
    approved_vid = APPROVED_DIR / f"short_{sid}.mp4"
    if args.approve and pending_vid.is_file():
        APPROVED_DIR.mkdir(parents=True, exist_ok=True)
        approved_vid.write_bytes(pending_vid.read_bytes())

    target = approved_vid if approved_vid.is_file() else None
    if not target:
        log.error("[s] Approved video not found.", sid)
        return 1

    title, desc, tags = _build_metadata(sid, _find_script_meta(sid))
    log.info("[  s", sid, title, args.privacy)
    if args.dry_run:
        log.info("[s] DRY-RUN OK.", sid)
        return 0

    from googleapiclient.http import MediaFileUpload
    yt = _get_youtube_service(Path(args.client_secrets), Path(args.token))
    body = {"snippet": {"title": title, "description": desc, "tags": tags, "categoryId": "27"}, "status": {"privacyStatus": args.privacy, "selfDeclaredMadeForKids": False}}
    media = MediaFileUpload(str(target), mimetype="video/mp4", resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid = str(resp.get("id") or "")
    store["uploaded"][sid] = {"video_id": vid, "privacy": args.privacy}
    _save_uploaded_log(store)
    log.info("[//youtu.be/s", sid, vid)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
