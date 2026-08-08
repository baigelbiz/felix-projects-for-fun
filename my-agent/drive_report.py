"""One-off Drive organization report for both Google accounts.
Read-only metadata scan — does not move, rename, or delete anything.
"""
import json
from collections import defaultdict
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

CRED_DIR = Path.home() / ".gmail-mcp"
CLIENT_FILE = CRED_DIR / "gcp-oauth.keys.json"


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
    # Credentials built without `expiry` are always reported as non-expired
    # by google-auth, so `.expired` never fires and the token goes stale
    # after ~1h. Refresh unconditionally instead of gating on `.expired`.
    creds.refresh(Request())
    return creds


# Generic names that show up legitimately many times inside different zips/app
# bundles — not real duplicates, just noise from how zip contents get indexed.
NOISE_NAMES = {
    "contents", "macos", "_codesignature", "resources", "info.plist",
    "ru.lproj", "en.lproj", "frameworks", "untitled document",
    "untitled spreadsheet", "untitled presentation",
}


def scan(account_label, token_file):
    svc = build("drive", "v3", credentials=creds_for(token_file))
    files = []
    page_token = None
    while True:
        res = svc.files().list(
            pageSize=1000,
            fields="nextPageToken, files(id,name,mimeType,size,md5Checksum,modifiedTime,createdTime,parents,trashed,webViewLink)",
            pageToken=page_token,
        ).execute()
        files.extend(res.get("files", []))
        page_token = res.get("nextPageToken")
        if not page_token:
            break

    active = [f for f in files if not f.get("trashed")]
    is_folder = lambda f: f.get("mimeType") == "application/vnd.google-apps.folder"
    total_size = sum(int(f.get("size", 0) or 0) for f in active)

    # Duplicate detection: same md5 checksum (real files), excluding folders and noise names
    by_key = defaultdict(list)
    for f in active:
        if is_folder(f) or f["name"].lower() in NOISE_NAMES:
            continue
        if f.get("md5Checksum"):
            key = ("md5", f["md5Checksum"])
        else:
            key = ("name_size", f["name"], f.get("size"))
        by_key[key].append(f)
    duplicates = {k: v for k, v in by_key.items() if len(v) > 1}

    largest = sorted(active, key=lambda f: int(f.get("size", 0) or 0), reverse=True)[:20]

    print(f"\n{'='*70}\n{account_label}\n{'='*70}")
    print(f"Total active files: {len(active)} | Total size: {total_size / (1024**3):.2f} GB")

    dup_wasted = 0
    print(f"\n--- DUPLICATES ({len(duplicates)} groups) ---")
    for key, group in sorted(duplicates.items(), key=lambda kv: -int(kv[1][0].get("size", 0) or 0)):
        size = int(group[0].get("size", 0) or 0)
        dup_wasted += size * (len(group) - 1)
        print(f"\n[{len(group)}x] {group[0]['name']}  ({size/(1024**2):.1f} MB each)")
        for g in group:
            print(f"    {g.get('webViewLink','(no link)')}")
    print(f"\nTotal space wasted by duplicates: {dup_wasted/(1024**2):.1f} MB")

    print(f"\n--- LARGEST FILES (top 20) ---")
    for f in largest:
        size_mb = int(f.get("size", 0) or 0) / (1024**2)
        print(f"{size_mb:8.1f} MB  {f['name']}\n    {f.get('webViewLink','(no link)')}")


if __name__ == "__main__":
    scan("PERSONAL (baigelbiz@gmail.com)", "drive_scan_personal_token.json")
    scan("BUSINESS (support@shefa.homes)", "drive_scan_business_token.json")
