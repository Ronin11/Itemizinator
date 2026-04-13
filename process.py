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
            creds.refresh(Request())
        else:
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

def list_drive_images(drive, folder_id):
    """List all image files in a Drive folder (recursively)."""
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
                files.append(f)
            elif f["mimeType"] == "application/vnd.google-apps.folder":
                files.extend(list_drive_images(drive, f["id"]))

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


# ---- Gemini ----

def analyze_image(model, image_bytes, mime_type):
    """Send image to Gemini and get item description."""
    image_part = {"mime_type": mime_type, "data": image_bytes}
    response = model.generate_content([GEMINI_PROMPT, image_part])
    text = response.text.strip()

    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines[1:] if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        items = json.loads(text)
        if isinstance(items, list):
            return items
    except json.JSONDecodeError:
        print(f"  WARNING: Could not parse Gemini response as JSON")
        print(f"  Response: {text[:200]}")
    return []


# ---- Google Sheets ----

def setup_sheet(sheets, spreadsheet_id):
    """Set up the header row in the spreadsheet."""
    headers = [
        ["Item", "Description", "Category", "Condition", "Photo",
         "Claimed By", "Priority (1-3)", "Notes"]
    ]

    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range="Sheet1!A1:H1",
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
        range="Sheet1!A:H",
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
    args = parser.parse_args()

    load_env()

    # Set up Gemini
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

    print(f"Found {len(image_files)} photos")

    # Make sure folder is publicly viewable for photo URLs
    ensure_folder_shared(drive, args.drive_folder_id)

    # Set up spreadsheet
    print("Setting up spreadsheet...")
    setup_sheet(sheets, args.spreadsheet_id)

    # Process each photo
    total_items = 0
    rows = []

    for i, img in enumerate(image_files, 1):
        print(f"  [{i}/{len(image_files)}] {img['name']}...", end=" ", flush=True)

        try:
            image_bytes = download_drive_file(drive, img["id"])
            items = analyze_image(model, image_bytes, img["mimeType"])
            photo_url = make_photo_url(img["id"])

            for item in items:
                rows.append([
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

        # Rate limit: Gemini free tier is 15 RPM
        time.sleep(4)

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
