#!/usr/bin/env python3
"""
Itemizer - Process Google Drive photos through Gemini and catalog to Google Sheets.

Usage:
    python process.py <drive_folder_id> <spreadsheet_id>

The Drive folder should contain subfolders named by room (e.g., "Kitchen", "Living Room").
Each subfolder contains photos of items in that room.
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
from PIL import Image

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]

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
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                print("ERROR: credentials.json not found.")
                print("Download it from Google Cloud Console > APIs & Credentials > OAuth 2.0 Client IDs")
                print(f"Save it as: {creds_path}")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)

        token_path.write_text(creds.to_json())

    return creds


def get_drive_folders(drive, parent_id):
    """Get subfolders (rooms) from the parent Drive folder."""
    results = drive.files().list(
        q=f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id, name)",
        orderBy="name",
    ).execute()
    return results.get("files", [])


def get_drive_images(drive, folder_id):
    """Get image files from a Drive folder."""
    results = drive.files().list(
        q=f"'{folder_id}' in parents and mimeType contains 'image/' and trashed=false",
        fields="files(id, name, mimeType, webViewLink, webContentLink)",
        orderBy="name",
    ).execute()
    return results.get("files", [])


def download_image(drive, file_id):
    """Download an image from Drive and return as PIL Image."""
    request = drive.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buffer.seek(0)
    return Image.open(buffer)


def analyze_image(model, image):
    """Send image to Gemini and get item descriptions."""
    response = model.generate_content([GEMINI_PROMPT, image])
    text = response.text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last fence lines
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


def make_drive_link(file_id):
    """Create a viewable Drive link for an image."""
    return f"https://drive.google.com/file/d/{file_id}/view"


def setup_sheet(sheets, spreadsheet_id):
    """Set up the header row in the spreadsheet."""
    headers = [
        ["Room", "Item", "Description", "Category", "Condition", "Photo Link",
         "Claimed By", "Priority (1-3)", "Notes"]
    ]

    # Clear and write headers
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range="Sheet1!A1:I1",
        valueInputOption="RAW",
        body={"values": headers},
    ).execute()

    # Bold the header row and freeze it
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


def main():
    if len(sys.argv) != 3:
        print("Usage: python process.py <drive_folder_id> <spreadsheet_id>")
        print()
        print("  drive_folder_id  - ID of the Google Drive folder containing room subfolders")
        print("  spreadsheet_id   - ID of the Google Sheet to write results to")
        print()
        print("The Drive folder ID is in the URL: drive.google.com/drive/folders/<THIS_PART>")
        print("The Sheet ID is in the URL: docs.google.com/spreadsheets/d/<THIS_PART>/edit")
        sys.exit(1)

    drive_folder_id = sys.argv[1]
    spreadsheet_id = sys.argv[2]

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
    drive = build("drive", "v3", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)

    # Set up spreadsheet
    print("Setting up spreadsheet...")
    setup_sheet(sheets, spreadsheet_id)

    # Get room folders
    rooms = get_drive_folders(drive, drive_folder_id)
    if not rooms:
        print(f"No subfolders found in Drive folder {drive_folder_id}")
        print("Create subfolders named by room (e.g., 'Kitchen', 'Living Room') with photos inside.")
        sys.exit(1)

    print(f"Found {len(rooms)} rooms: {', '.join(r['name'] for r in rooms)}")

    total_items = 0
    for room in rooms:
        room_name = room["name"]
        print(f"\n--- {room_name} ---")

        images = get_drive_images(drive, room["id"])
        if not images:
            print(f"  No images found in {room_name}")
            continue

        print(f"  Found {len(images)} photos")
        rows = []

        for img_file in images:
            print(f"  Processing: {img_file['name']}...", end=" ", flush=True)

            try:
                image = download_image(drive, img_file["id"])
                items = analyze_image(model, image)
                photo_link = make_drive_link(img_file["id"])

                for item in items:
                    rows.append([
                        room_name,
                        item.get("name", "Unknown"),
                        item.get("description", ""),
                        item.get("category", "Other"),
                        item.get("condition", ""),
                        photo_link,
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

        # Batch append rows per room
        if rows:
            append_rows(sheets, spreadsheet_id, rows)
            print(f"  Added {len(rows)} items to spreadsheet")

    print(f"\nDone! {total_items} total items cataloged.")
    print(f"View spreadsheet: https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit")


if __name__ == "__main__":
    main()
