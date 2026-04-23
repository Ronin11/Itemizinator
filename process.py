#!/usr/bin/env python3
"""
Itemizer - Process photos from Google Drive through Gemini into Google Sheets.

Usage:
    python process.py <spreadsheet_id> <drive_folder_id>

Upload photos to a Google Drive folder, then point this script at it.
Each image is sent to Gemini to identify the item, then results
are written to the Google Sheet with photo URLs.
"""

import io
import json
import os
import sys
import time
from pathlib import Path

import google.generativeai as genai
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

IMAGE_MIMES = {"image/jpeg", "image/png", "image/heic", "image/heif", "image/webp", "image/bmp", "image/tiff"}

# Gemini 429 backoff schedule (seconds). After these, fall back to Anthropic if configured.
RETRY_DELAYS = [5, 15, 45]

ANTHROPIC_MODEL = "claude-opus-4-7"
# Anthropic image input supports JPEG/PNG/GIF/WEBP only.
ANTHROPIC_SUPPORTED_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

GEMINI_PROMPT = """You are helping catalog household items from an estate.
Each photo is of ONE specific item someone wants to catalog. Identify the main item
that is the focus of the photo. Do NOT list background items, furniture the item is
sitting on, or other incidental things in the frame.

Provide:
1. name: A short, clear name for the item
2. description: A brief description (color, material, style, brand if visible)
3. category: One of: Furniture, Electronics, Kitchen, Decor, Clothing, Books/Media, Tools, Jewelry/Accessories, Art, Collectibles, Appliances, Linens/Textiles, Other
4. condition: One of: Excellent, Good, Fair, Poor

Return a JSON array with exactly one object. Example:
[
  {"name": "Oak Dining Table", "description": "Solid oak, seats 6, minor scratches on surface", "category": "Furniture", "condition": "Good"}
]

If you cannot identify the item clearly, return an empty array: []
Return ONLY the JSON array, no other text."""


def load_env():
    """Load .env file from script directory."""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def get_google_creds():
    """Get or refresh Google OAuth2 credentials."""
    token_path = Path(__file__).parent / "token.json"
    creds_path = Path(__file__).parent / "credentials.json"

    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except ValueError:
            token_path.unlink()
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                print(f"Token refresh failed ({e}); re-authenticating...")
                token_path.unlink(missing_ok=True)
                creds = None
        if not creds or not creds.valid:
            if not creds_path.exists():
                print("ERROR: credentials.json not found.")
                print("Download it from Google Cloud Console > APIs & Credentials > OAuth 2.0 Client IDs")
                print(f"Save it as: {creds_path}")
                sys.exit(1)
            creds_data = json.loads(creds_path.read_text())
            client_type = "web" if "web" in creds_data else "installed"

            if client_type == "web":
                web_config = creds_data["web"]
                installed_config = {
                    "installed": {
                        "client_id": web_config["client_id"],
                        "client_secret": web_config["client_secret"],
                        "auth_uri": web_config["auth_uri"],
                        "token_uri": web_config["token_uri"],
                        "redirect_uris": ["http://localhost:8888"],
                    }
                }
                import tempfile
                tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
                json.dump(installed_config, tmp)
                tmp.close()
                flow = InstalledAppFlow.from_client_secrets_file(tmp.name, SCOPES)
                os.unlink(tmp.name)
                port = 8888
            else:
                flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
                port = 0

            creds = flow.run_local_server(port=port)

        token_path.write_text(creds.to_json())

    return creds


# ---- Google Drive ----

def list_drive_images(drive, folder_id, location=""):
    """List all image files in a Drive folder (recursively).

    Each returned file is tagged with a 'location' based on its subfolder path.
    Files directly in the root folder get location="" (empty).
    """
    files = []
    page_token = None
    while True:
        resp = drive.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType)",
            orderBy="name",
            pageSize=100,
            pageToken=page_token,
        ).execute()

        for f in resp.get("files", []):
            if f["mimeType"] in IMAGE_MIMES:
                f["location"] = location
                files.append(f)
            elif f["mimeType"] == "application/vnd.google-apps.folder":
                # Build nested path like "Living Room" or "Living Room / Bookshelf"
                sub_location = f["name"] if not location else f"{location} / {f['name']}"
                files.extend(list_drive_images(drive, f["id"], sub_location))

        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def download_drive_file(drive, file_id):
    """Download a file from Drive into memory and return bytes."""
    request = drive.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buffer.getvalue()


def make_photo_url(file_id):
    """Direct image URL for a publicly readable Drive file."""
    return f"https://lh3.googleusercontent.com/d/{file_id}"


def ensure_folder_shared(drive, folder_id):
    """Make sure the folder is viewable by anyone with link (for photo URLs)."""
    try:
        drive.permissions().create(
            fileId=folder_id,
            body={"type": "anyone", "role": "reader"},
        ).execute()
        print("  Drive folder set to 'Anyone with link can view'")
    except Exception:
        # May fail if already shared or insufficient permissions — that's fine
        pass


# ---- Model analysis ----

def parse_items_response(text):
    """Parse a JSON-array item list response, tolerating code fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines[1:] if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        items = json.loads(text)
        if isinstance(items, list):
            return items
    except json.JSONDecodeError:
        print(f"  WARNING: Could not parse model response as JSON")
        print(f"  Response: {text[:200]}")
    return []


def analyze_image(model, image_bytes, mime_type, anthropic_client=None):
    """Gemini path with 5/15/45s backoff on 429; falls back to Anthropic if provided."""
    image_part = {"mime_type": mime_type, "data": image_bytes}

    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            response = model.generate_content([GEMINI_PROMPT, image_part])
            return parse_items_response(response.text)
        except Exception as e:
            msg = str(e)
            is_rate_limit = "429" in msg or "Resource exhausted" in msg or "RESOURCE_EXHAUSTED" in msg
            if not is_rate_limit:
                raise
            if attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt]
                print(f"rate limited, waiting {delay}s (attempt {attempt + 1}/{len(RETRY_DELAYS)})...", end=" ", flush=True)
                time.sleep(delay)
                continue
            # Gemini retries exhausted — try Anthropic if we have a client
            if anthropic_client is not None:
                print("falling back to Anthropic...", end=" ", flush=True)
                return analyze_image_anthropic(anthropic_client, image_bytes, mime_type)
            raise

    return []


def analyze_image_anthropic(client, image_bytes, mime_type):
    """Send image to Claude and parse the item-list response."""
    if mime_type not in ANTHROPIC_SUPPORTED_MIMES:
        print(f"  WARNING: Anthropic does not support {mime_type}, skipping")
        return []

    import base64
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    message = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime_type, "data": b64},
                },
                {"type": "text", "text": GEMINI_PROMPT},
            ],
        }],
    )

    text = next((b.text for b in message.content if b.type == "text"), "")
    return parse_items_response(text)


# ---- Google Sheets ----

EXPECTED_HEADERS = [
    "Location", "Item", "Description", "Category", "Condition", "Photo",
    "Claimed By", "Priority (1-3)", "Notes"
]


def get_existing_photo_ids(sheets, spreadsheet_id):
    """Return the set of Drive file IDs already in the sheet's Photo column."""
    try:
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range="Sheet1!F:F",
        ).execute()
    except Exception:
        return set()

    values = resp.get("values", [])
    ids = set()
    for row in values[1:]:  # skip header
        if not row:
            continue
        url = row[0].strip()
        # URL format: https://lh3.googleusercontent.com/d/{FILE_ID}
        if "/d/" in url:
            file_id = url.split("/d/", 1)[1].split("/", 1)[0].split("?", 1)[0]
            if file_id:
                ids.add(file_id)
    return ids


def sheet_has_headers(sheets, spreadsheet_id):
    """Check if the sheet already has the expected headers."""
    try:
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range="Sheet1!A1:I1",
        ).execute()
        values = resp.get("values", [])
        if not values:
            return False
        return values[0][:len(EXPECTED_HEADERS)] == EXPECTED_HEADERS
    except Exception:
        return False


def setup_sheet(sheets, spreadsheet_id):
    """Set up the header row in the spreadsheet (only if not already set)."""
    if sheet_has_headers(sheets, spreadsheet_id):
        return

    headers = [EXPECTED_HEADERS]

    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range="Sheet1!A1:I1",
        valueInputOption="RAW",
        body={"values": headers},
    ).execute()

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "repeatCell": {
                        "range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {
                            "userEnteredFormat": {
                                "textFormat": {"bold": True},
                                "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9},
                            }
                        },
                        "fields": "userEnteredFormat(textFormat,backgroundColor)",
                    }
                },
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": 0, "gridProperties": {"frozenRowCount": 1}},
                        "fields": "gridProperties.frozenRowCount",
                    }
                },
            ]
        },
    ).execute()


def append_rows(sheets, spreadsheet_id, rows):
    """Append rows to the spreadsheet."""
    if not rows:
        return
    sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range="Sheet1!A:I",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


# ---- Main ----

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Catalog items from Google Drive photos into Google Sheets")
    parser.add_argument("spreadsheet_id", help="Google Sheet ID (from the URL)")
    parser.add_argument("drive_folder_id", help="Google Drive folder ID containing photos")
    parser.add_argument("--force", action="store_true",
                        help="Re-process all photos even if they're already in the sheet")
    parser.add_argument("--anthropic", action="store_true",
                        help="Use Anthropic (Claude) instead of Gemini for all photos")
    args = parser.parse_args()

    load_env()

    # Optional Anthropic client — used as Gemini fallback, or as primary with --anthropic
    anthropic_client = None
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key:
        from anthropic import Anthropic
        anthropic_client = Anthropic(api_key=anthropic_key)
    elif args.anthropic:
        print("ERROR: --anthropic set but ANTHROPIC_API_KEY is not. Add it to .env or export it.")
        sys.exit(1)

    # Gemini setup (skipped in Anthropic-only mode)
    model = None
    if not args.anthropic:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print("ERROR: GEMINI_API_KEY not set. Add it to .env or export it.")
            sys.exit(1)

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.0-flash")

    # Set up Google APIs
    print("Authenticating with Google...")
    creds = get_google_creds()
    sheets = build("sheets", "v4", credentials=creds)
    drive = build("drive", "v3", credentials=creds)

    # List images in Drive folder
    print("Scanning Drive folder for images...")
    image_files = list_drive_images(drive, args.drive_folder_id)
    if not image_files:
        print("No image files found in the Drive folder.")
        print("Upload photos to the folder and try again.")
        sys.exit(1)

    print(f"Found {len(image_files)} photos in Drive")

    # Make sure folder is publicly viewable for photo URLs
    ensure_folder_shared(drive, args.drive_folder_id)

    # Set up spreadsheet (skips if headers already match)
    print("Setting up spreadsheet...")
    setup_sheet(sheets, args.spreadsheet_id)

    # Skip photos already in the sheet (unless --force)
    if not args.force:
        existing_ids = get_existing_photo_ids(sheets, args.spreadsheet_id)
        if existing_ids:
            skipped = len(image_files)
            image_files = [f for f in image_files if f["id"] not in existing_ids]
            skipped -= len(image_files)
            if skipped > 0:
                print(f"Skipping {skipped} photos already in the sheet")
            if not image_files:
                print("All photos already processed. Use --force to re-process.")
                sys.exit(0)
            print(f"Processing {len(image_files)} new photos")

    # Process each photo
    total_items = 0
    rows = []

    for i, img in enumerate(image_files, 1):
        print(f"  [{i}/{len(image_files)}] {img['name']}...", end=" ", flush=True)

        try:
            image_bytes = download_drive_file(drive, img["id"])
            if args.anthropic:
                items = analyze_image_anthropic(anthropic_client, image_bytes, img["mimeType"])
            else:
                items = analyze_image(model, image_bytes, img["mimeType"], anthropic_client=anthropic_client)
            photo_url = make_photo_url(img["id"])

            for item in items:
                rows.append([
                    img.get("location", ""),
                    item.get("name", "Unknown"),
                    item.get("description", ""),
                    item.get("category", "Other"),
                    item.get("condition", ""),
                    photo_url,
                    "",  # Claimed By
                    "",  # Priority
                    "",  # Notes
                ])

            print(f"{len(items)} item{'s' if len(items) != 1 else ''}")
            total_items += len(items)

        except Exception as e:
            print(f"ERROR: {e}")

        # Rate limit: Gemini free tier is 15 RPM; skip the sleep in Anthropic-only mode
        if not args.anthropic:
            time.sleep(5)

        # Batch write every 50 rows
        if len(rows) >= 50 or i == len(image_files):
            if rows:
                append_rows(sheets, args.spreadsheet_id, rows)
                print(f"  -> Wrote {len(rows)} items to spreadsheet")
                rows = []

    print(f"\nDone! {total_items} total items cataloged.")
    print(f"View spreadsheet: https://docs.google.com/spreadsheets/d/{args.spreadsheet_id}/edit")


if __name__ == "__main__":
    main()
