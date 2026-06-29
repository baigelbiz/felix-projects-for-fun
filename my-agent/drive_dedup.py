"""Delete confirmed duplicate files (matched by MD5 checksum) across both
Drive accounts, keeping the oldest copy of each. Files only matched by
name+size (no checksum — typically native Google Docs/Sheets/Slides) are
NOT deleted, just reported, since same size doesn't guarantee same content.
Moves to Trash (recoverable 30 days), never permanent delete.
"""
import json
from collections import defaultdict
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

CRED_DIR = Path.home() / ".gmail-mcp"
CLIENT_FILE = CRED_DIR / "gcp-oauth.keys.json"

NOISE_NAMES = {
    "contents", "macos", "_codesignature", "resources", "info.plist",
    "ru.lproj", "en.lproj", "frameworks", "untitled document",
    "untitled spreadsheet", "untitled presentation",
}


def creds_for(token_file):
    raw = json.loads((CRED_DIR / token_file).read_text())
    client = json.loads(CLIENT_FILE.read_text())["installed"]
    creds = Credentials(
        token=raw["access_token"],
        refresh_token=raw["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client["client_id"],
        client_secret=client["client_secret"],
        scopes=raw["scope"].split(),
    )
    if creds.expired:
        creds.refresh(Request())
        raw["access_token"] = creds.token
        (CRED_DIR / token_file).write_text(json.dumps(raw))
    return creds


def process(account_label, write_token_file):
    svc = build("drive", "v3", credentials=creds_for(write_token_file))
    files = []
    page_token = None
    while True:
        res = svc.files().list(
            pageSize=1000,
            fields="nextPageToken, files(id,name,mimeType,size,md5Checksum,modifiedTime,createdTime,trashed,shared,permissions)",
            pageToken=page_token,
        ).execute()
        files.extend(res.get("files", []))
        page_token = res.get("nextPageToken")
        if not page_token:
            break

    active = [f for f in files if not f.get("trashed")]
    is_folder = lambda f: f.get("mimeType") == "application/vnd.google-apps.folder"

    confirmed = defaultdict(list)  # md5 -> [files]
    uncertain = defaultdict(list)  # (name,size) -> [files], no checksum
    for f in active:
        if is_folder(f) or f["name"].lower() in NOISE_NAMES:
            continue
        if f.get("md5Checksum"):
            confirmed[f["md5Checksum"]].append(f)
        else:
            uncertain[(f["name"], f.get("size"))].append(f)

    confirmed_dupes = {k: v for k, v in confirmed.items() if len(v) > 1}
    uncertain_dupes = {k: v for k, v in uncertain.items() if len(v) > 1}

    print(f"\n{'='*70}\n{account_label}\n{'='*70}")
    print(f"Confirmed duplicate groups (will delete extras): {len(confirmed_dupes)}")
    print(f"Uncertain groups (name+size only, NOT deleting): {len(uncertain_dupes)}")

    deleted_count = 0
    freed_bytes = 0
    skipped = []
    for md5, group in confirmed_dupes.items():
        group_sorted = sorted(group, key=lambda f: f.get("createdTime", ""))
        keep, extras = group_sorted[0], group_sorted[1:]
        trashed_this_group = 0
        for f in extras:
            try:
                svc.files().update(fileId=f["id"], body={"trashed": True}).execute()
                deleted_count += 1
                trashed_this_group += 1
                freed_bytes += int(f.get("size", 0) or 0)
            except Exception as e:
                skipped.append((f["name"], str(e)[:150]))
        print(f"  Kept '{keep['name']}' ({keep['id']}), trashed {trashed_this_group}/{len(extras)} duplicate(s)")

    if skipped:
        print(f"\n--- SKIPPED (no permission to delete — not owned by this account) ---")
        for name, err in skipped:
            print(f"  ! {name}")

    print(f"\nDeleted {deleted_count} duplicate files, freed {freed_bytes/(1024**3):.2f} GB")

    if uncertain_dupes:
        print(f"\n--- NEEDS MANUAL REVIEW (same name+size, content not verified) ---")
        for (name, size), group in sorted(uncertain_dupes.items(), key=lambda kv: -int(kv[1][0].get("size") or 0))[:20]:
            print(f"  [{len(group)}x] {name}")

    return active


def check_weird(account_label, files):
    weird = []
    for f in files:
        if f.get("shared") and any(
            p.get("type") == "anyone" for p in f.get("permissions", []) or []
        ):
            weird.append(f"PUBLIC LINK SHARING: '{f['name']}' is shared with 'anyone with the link'")
    if weird:
        print(f"\n--- WEIRD/FLAGGED ({account_label}) ---")
        for w in weird:
            print(f"  ! {w}")


if __name__ == "__main__":
    personal_files = process("PERSONAL (baigelbiz@gmail.com)", "drive_write_personal_token.json")
    check_weird("PERSONAL", personal_files)
    business_files = process("BUSINESS (support@shefa.homes)", "drive_write_business_token.json")
    check_weird("BUSINESS", business_files)
