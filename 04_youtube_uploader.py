

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
BASE_DIR = Path(__file__).resolve().parent
SCRIPTS_FILE = BASE_DIR / "data" / "scripts_batch_1.json"
UPLOAD_LOG_FILE = BASE_DIR / "data" / "uploaded_shorts.json"
CLIENT_SECRET_FILE = BASE_DIR / "client_secret.json"
TOKEN_FILE = BASE_DIR / "token.json"


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def find_short_record(short_id: str) -> dict:
    records = load_json(SCRIPTS_FILE, [])
    if not isinstance(records, list):
        raise ValueError("data/scripts_batch_1.json must contain a list.")
    target = str(short_id).strip()
    for item in records:
        if isinstance(item, dict) and str(item.get("id", "")).strip() == target:
            return item
    raise FileNotFoundError(f"Short ID '{short_id}' not found in {SCRIPTS_FILE}.")


def find_video_file(short_id: str) -> Path:
    candidates = [
        BASE_DIR / "output" / "approved" / f"short_{short_id}.mp4",
        BASE_DIR / "output" / "pending_approval" / f"short_{short_id}.mp4",
        BASE_DIR / "output" / f"short_{short_id}.mp4",
    ]
    for path in candidates:
        if path.exists() and path.stat().st_size > 0:
            return path
    raise FileNotFoundError(
        f"Video file for short '{short_id}' not found in output/approved or output/pending_approval."
    )


def get_youtube_service(allow_local_auth: bool = False):
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    elif not creds or not creds.valid:
        if not allow_local_auth:
            raise RuntimeError(
                "Valid token.json not found or cannot be refreshed in non-interactive mode."
            )
        if not CLIENT_SECRET_FILE.exists():
            raise FileNotFoundError(
                f"Missing {CLIENT_SECRET_FILE}. Download OAuth Desktop client JSON from Google Cloud."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_FILE), SCOPES)
        creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    return build("youtube", "v3", credentials=creds)


def _clean_tag(raw_tag: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 ]+", "", str(raw_tag)).strip()
    return cleaned[:30]


def build_metadata(record: dict, short_id: str) -> tuple[str, str, list[str]]:
    raw_title = (
        record.get("youtube_title")
        or record.get("title")
        or record.get("proverb")
        or record.get("topic")
        or f"Daily Insight #{short_id}"
    )
    title = re.sub(r"\s+", " ", str(raw_title)).strip()
    if "#shorts" not in title.lower():
        suffix = " #Shorts"
        max_base_len = 100 - len(suffix)
        title = f"{title[:max_base_len].rstrip()}{suffix}"
    else:
        title = title[:100]

    hook = str(record.get("hook") or record.get("summary") or record.get("moral") or "").strip()
    proverb = str(record.get("proverb") or "").strip()
    moral = str(record.get("moral") or "").strip()

    parts = []
    if hook:
        parts.append(hook)
    if proverb and proverb.lower() not in hook.lower():
        parts.append(f"Proverb: {proverb}")
    if moral and moral.lower() not in hook.lower():
        parts.append(f"Takeaway: {moral}")

    body_text = "\n\n".join(parts).strip()
    footer = (
        f"PulseShorts Daily — Episode #{short_id}\n"
        "#Shorts #Stories #Proverbs #Insights #Wisdom"
    )
    description = f"{body_text}\n\n{footer}".strip() if body_text else footer
    description = description[:4900]

    base_tags = ["Shorts", "PulseShorts", "Stories", "Proverbs", "Insights", "Wisdom"]
    extra_tags = record.get("tags")
    if isinstance(extra_tags, list):
        for t in extra_tags:
            ct = _clean_tag(t)
            if ct and ct.lower() not in {x.lower() for x in base_tags}:
                base_tags.append(ct)

    # YouTube total tags character limit is 500 chars
    final_tags: list[str] = []
    total_len = 0
    for tag in base_tags:
        if total_len + len(tag) + 1 <= 450:
            final_tags.append(tag)
            total_len += len(tag) + 1
    if not final_tags:
        final_tags = ["Shorts", "Stories", "Proverbs", "Insights"]

    return title, description, final_tags


def is_already_uploaded(short_id: str, logs: list) -> bool:
    target = str(short_id).strip()
    return any(
        isinstance(entry, dict)
        and str(entry.get("short_id", "")).strip() == target
        and bool(str(entry.get("video_id", "")).strip())
        for entry in logs
    )


def upload_video(
    short_id: str,
    privacy: str,
    allow_local_auth: bool,
    allow_duplicate: bool = False,
) -> dict:
    privacy = privacy.lower().strip()
    if privacy not in {"private", "unlisted", "public"}:
        raise ValueError("privacy must be one of: private, unlisted, public")

    existing_logs = load_json(UPLOAD_LOG_FILE, [])
    if not isinstance(existing_logs, list):
        existing_logs = []

    if not allow_duplicate and is_already_uploaded(short_id, existing_logs):
        raise RuntimeError(
            f"Short ID '{short_id}' is already recorded in {UPLOAD_LOG_FILE}. Skipping duplicate upload."
        )

    record = find_short_record(short_id)
    video_path = find_video_file(short_id)
    title, description, tags = build_metadata(record, short_id)

    youtube = get_youtube_service(allow_local_auth=allow_local_auth)

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": "22",  # People & Blogs
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        _, response = request.next_chunk()

    video_id = response.get("id", "")
    watch_url = f"https://www.youtube.com/shorts/{video_id}" if video_id else ""

    log_entry = {
        "short_id": str(short_id),
        "video_id": video_id,
        "url": watch_url,
        "title": title,
        "privacy": privacy,
        "uploaded_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    existing_logs.append(log_entry)
    save_json(UPLOAD_LOG_FILE, existing_logs)

    return log_entry


def main():
    parser = argparse.ArgumentParser(description="Upload generated short to YouTube.")
    parser.add_argument("--id", required=True, help="Short ID from scripts_batch_1.json")
    parser.add_argument(
        "--privacy",
        default="private",
        choices=["private", "unlisted", "public"],
        help="YouTube privacy status (default: private)",
    )
    parser.add_argument(
        "--auth-only",
        action="store_true",
        help="Run local OAuth flow to create token.json without uploading",
    )
    parser.add_argument(
        "--no-local-auth",
        action="store_true",
        help="Fail instead of opening a local browser if token is missing/invalid",
    )
    parser.add_argument(
        "--allow-duplicate",
        action="store_true",
        help="Allow uploading even if short_id already exists in uploaded_shorts.json",
    )
    args = parser.parse_args()

    if args.auth_only:
        get_youtube_service(allow_local_auth=True)
        print(f"OAuth token saved to {TOKEN_FILE}")
        return

    result = upload_video(
        short_id=args.id,
        privacy=args.privacy,
        allow_local_auth=not args.no_local_auth,
        allow_duplicate=args.allow_duplicate,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except HttpError as e:
        print(f"YouTube API HttpError: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Upload failed: {e}", file=sys.stderr)
        sys.exit(1)
