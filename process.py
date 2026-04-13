#!/usr/bin/env python3
"""
Itemizer - Process photos through Gemini and catalog to Google Sheets.

Usage:
    python process.py <spreadsheet_id> <photos_dir>

photos_dir should contain image files (jpg, png, heic, etc.).
Each image is analyzed by Gemini to identify items, then results
are written to the Google Sheet.
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
from PIL import Image

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".bmp", ".tiff", ".tif"}

GEMINI_PROMPT = """You are helping catalog household items from an estate.
Look at this photo and identify each distinct item you can see.

For EACH item, provide:
1. name: A short, clear name for the item
2. description: A brief description (color, material, style, brand if visible)
3. category: One of: Furniture, Electronics, Kitchen, Decor, Clothing, Books/Media, Tools, Jewelry/Accessories, Art, Collectibles, Appliances, Linens/Textiles, Other
4. condition: One of: Excellent, Good, Fair, Poor

Return a JSON array of objects. Example:
[
  {"name": "Oak Dining Table", "description": "Solid oak, seats 6, minor scratches on surface", "category": "Furniture", "condition": "Good"},
  {"name": "Blue Table Lamp", "description": "Ceramic base, blue glaze, white shade", "category": "Decor", "condition": "Excellent"}
]

If you cannot identify any items clearly, return an empty array: []
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
            # Token missing refresh_token or malformed — re-auth
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


def extract_zips(photos_dir):
    """Extract any zip files found in the directory."""
    import zipfile
    photos_path = Path(photos_dir)
    for f in list(photos_path.iterdir()):
        if f.is_file() and f.suffix.lower() == ".zip":
            print(f"Extracting {f.name}...")
            with zipfile.ZipFile(f, "r") as zf:
                zf.extractall(photos_path)
            f.unlink()


def get_image_files(photos_dir):
    """Get all image files from a directory (recursively), sorted by name."""
    photos_path = Path(photos_dir)
    if not photos_path.is_dir():
        print(f"ERROR: {photos_dir} is not a directory")
        sys.exit(1)

    extract_zips(photos_path)

    files = []
    for f in sorted(photos_path.rglob("*")):
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
            files.append(f)
    return files


MIME_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".heic": "image/heic",
    ".heif": "image/heif", ".webp": "image/webp",
    ".bmp": "image/bmp", ".tiff": "image/tiff", ".tif": "image/tiff",
}


def load_image(path):
    """Load an image file for Gemini. Returns PIL Image or raw bytes for HEIC."""
    suffix = path.suffix.lower()
    if suffix in (".heic", ".heif"):
        # Gemini accepts raw HEIC — send as inline data
        data = path.read_bytes()
        return {"mime_type": MIME_TYPES[suffix], "data": data}
    return Image.open(path)


def analyze_image(model, image):
    """Send image to Gemini and get item descriptions."""
    response = model.generate_content([GEMINI_PROMPT, image])
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


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Catalog items from photos into Google Sheets")
    parser.add_argument("spreadsheet_id", help="Google Sheet ID (from the URL)")
    parser.add_argument("photos_dir", help="Directory containing photos to process")
    args = parser.parse_args()

    load_env()

    # Set up Gemini
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set. Add it to .env or export it.")
        sys.exit(1)

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")

    # Get photos
    image_files = get_image_files(args.photos_dir)
    if not image_files:
        print(f"No image files found in {args.photos_dir}")
        print(f"Supported formats: {', '.join(sorted(IMAGE_EXTENSIONS))}")
        sys.exit(1)

    print(f"Found {len(image_files)} photos in {args.photos_dir}")

    # Set up Google Sheets
    print("Authenticating with Google...")
    creds = get_google_creds()
    sheets = build("sheets", "v4", credentials=creds)

    print("Setting up spreadsheet...")
    setup_sheet(sheets, args.spreadsheet_id)

    # Process each photo
    total_items = 0
    rows = []

    for i, image_path in enumerate(image_files, 1):
        print(f"  [{i}/{len(image_files)}] {image_path.name}...", end=" ", flush=True)

        try:
            image = load_image(image_path)
            items = analyze_image(model, image)

            for item in items:
                rows.append([
                    item.get("name", "Unknown"),
                    item.get("description", ""),
                    item.get("category", "Other"),
                    item.get("condition", ""),
                    image_path.name,
                    "",  # Claimed By
                    "",  # Priority
                    "",  # Notes
                ])

            print(f"{len(items)} items found")
            total_items += len(items)

        except Exception as e:
            print(f"ERROR: {e}")

        # Rate limit: Gemini free tier is 15 RPM
        time.sleep(4)

        # Batch write every 20 photos
        if len(rows) >= 50 or i == len(image_files):
            if rows:
                append_rows(sheets, args.spreadsheet_id, rows)
                print(f"  -> Wrote {len(rows)} items to spreadsheet")
                rows = []

    print(f"\nDone! {total_items} total items cataloged.")
    print(f"View spreadsheet: https://docs.google.com/spreadsheets/d/{args.spreadsheet_id}/edit")


if __name__ == "__main__":
    main()
