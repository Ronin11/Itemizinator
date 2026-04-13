#!/usr/bin/env python3
"""
Itemizer - Process Google Photos album through Gemini and catalog to Google Sheets.

Usage:
    python process.py <spreadsheet_id> [--album-url <google_photos_share_url>]

If no album URL is given, lists your Google Photos albums to choose from.
"""

import io
import json
import os
import sys
import time
from pathlib import Path

import google.generativeai as genai
import requests as http_requests
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from PIL import Image

SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]

PHOTOS_API_BASE = "https://photoslibrary.googleapis.com/v1"

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
            # Load credentials - supports both "web" and "installed" client types
            creds_data = json.loads(creds_path.read_text())
            client_type = "web" if "web" in creds_data else "installed"

            if client_type == "web":
                # Convert web client to installed format for InstalledAppFlow
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


# ---- Google Photos ----

def photos_auth_header(creds):
    """Return auth header dict for Photos API requests."""
    return {"Authorization": f"Bearer {creds.token}"}


def list_shared_albums(creds):
    """List shared albums the user has joined or owns."""
    albums = []
    page_token = None
    while True:
        params = {"pageSize": 50}
        if page_token:
            params["pageToken"] = page_token
        resp = http_requests.get(
            f"{PHOTOS_API_BASE}/sharedAlbums",
            headers=photos_auth_header(creds),
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        albums.extend(data.get("sharedAlbums", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return albums


def list_albums(creds):
    """List the user's own albums."""
    albums = []
    page_token = None
    while True:
        params = {"pageSize": 50}
        if page_token:
            params["pageToken"] = page_token
        resp = http_requests.get(
            f"{PHOTOS_API_BASE}/albums",
            headers=photos_auth_header(creds),
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        albums.extend(data.get("albums", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return albums


def join_shared_album(creds, share_token):
    """Join a shared album by its share token."""
    resp = http_requests.post(
        f"{PHOTOS_API_BASE}/sharedAlbums:join",
        headers={**photos_auth_header(creds), "Content-Type": "application/json"},
        json={"shareToken": share_token},
    )
    resp.raise_for_status()
    return resp.json().get("album", {})


def get_album_media(creds, album_id):
    """Get all media items from an album."""
    items = []
    page_token = None
    while True:
        body = {"albumId": album_id, "pageSize": 100}
        if page_token:
            body["pageToken"] = page_token
        resp = http_requests.post(
            f"{PHOTOS_API_BASE}/mediaItems:search",
            headers={**photos_auth_header(creds), "Content-Type": "application/json"},
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        items.extend(data.get("mediaItems", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return items


def download_photo(media_item):
    """Download a photo from Google Photos and return as PIL Image."""
    # Append =d to baseUrl for full resolution, =w1024-h1024 for manageable size
    url = media_item["baseUrl"] + "=w1600-h1600"
    resp = http_requests.get(url)
    resp.raise_for_status()
    return Image.open(io.BytesIO(resp.content))


def find_album_by_url(creds, share_url):
    """Try to find/join an album from a Google Photos share URL."""
    # First, try listing shared albums to find a match
    shared = list_shared_albums(creds)
    for album in shared:
        share_info = album.get("shareInfo", {})
        if share_info.get("shareableUrl", "") in share_url or share_url in share_info.get("shareableUrl", ""):
            return album

    # Try listing own albums too
    own = list_albums(creds)
    for album in own:
        if album.get("productUrl", "") and album["productUrl"] in share_url:
            return album

    # If not found, it might need to be joined first - ask user
    print("Could not automatically find this album in your library.")
    print("Please open the shared link in your browser and click 'Join' first,")
    print("then run this script again.")
    print()
    print("Alternatively, choose from your available albums:")
    all_albums = shared + own
    return choose_album_interactive(all_albums)


def choose_album_interactive(albums):
    """Let user pick an album from a list."""
    if not albums:
        print("No albums found.")
        return None

    # Deduplicate by id
    seen = set()
    unique = []
    for a in albums:
        if a["id"] not in seen:
            seen.add(a["id"])
            unique.append(a)
    albums = unique

    print(f"\nFound {len(albums)} albums:\n")
    for i, album in enumerate(albums, 1):
        count = album.get("mediaItemsCount", "?")
        print(f"  {i}. {album.get('title', 'Untitled')} ({count} items)")

    print()
    while True:
        choice = input("Enter album number (or 'q' to quit): ").strip()
        if choice.lower() == "q":
            sys.exit(0)
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(albums):
                return albums[idx]
        except ValueError:
            pass
        print("Invalid choice, try again.")


# ---- Gemini ----

def analyze_image(model, image):
    """Send image to Gemini and get item descriptions."""
    response = model.generate_content([GEMINI_PROMPT, image])
    text = response.text.strip()

    # Strip markdown code fences if present
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
        ["Item", "Description", "Category", "Condition", "Photo Link",
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

    parser = argparse.ArgumentParser(description="Catalog items from Google Photos into Google Sheets")
    parser.add_argument("spreadsheet_id", help="Google Sheet ID (from the URL)")
    parser.add_argument("--album-url", help="Google Photos shared album URL")
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

    # Find the album
    if args.album_url:
        print(f"Looking for album from URL...")
        album = find_album_by_url(creds, args.album_url)
    else:
        print("No album URL provided. Listing your albums...")
        all_albums = list_shared_albums(creds) + list_albums(creds)
        album = choose_album_interactive(all_albums)

    if not album:
        print("No album selected.")
        sys.exit(1)

    album_title = album.get("title", "Untitled")
    album_id = album["id"]
    print(f"\nProcessing album: {album_title}")

    # Get all photos
    print("Fetching photos from album...")
    media_items = get_album_media(creds, album_id)
    # Filter to images only
    media_items = [m for m in media_items if m.get("mimeType", "").startswith("image/")]
    print(f"Found {len(media_items)} photos")

    if not media_items:
        print("No photos found in this album.")
        sys.exit(1)

    # Set up spreadsheet
    print("Setting up spreadsheet...")
    setup_sheet(sheets, args.spreadsheet_id)

    # Process each photo
    total_items = 0
    rows = []

    for i, media_item in enumerate(media_items, 1):
        filename = media_item.get("filename", "unknown")
        print(f"  [{i}/{len(media_items)}] {filename}...", end=" ", flush=True)

        try:
            image = download_photo(media_item)
            items = analyze_image(model, image)
            photo_url = media_item.get("productUrl", "")

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

            print(f"{len(items)} items found")
            total_items += len(items)

        except Exception as e:
            print(f"ERROR: {e}")

        # Rate limit: Gemini free tier is 15 RPM
        time.sleep(4)

        # Batch write every 20 photos
        if len(rows) >= 50 or i == len(media_items):
            if rows:
                append_rows(sheets, args.spreadsheet_id, rows)
                print(f"  -> Wrote {len(rows)} items to spreadsheet")
                rows = []

    print(f"\nDone! {total_items} total items cataloged.")
    print(f"View spreadsheet: https://docs.google.com/spreadsheets/d/{args.spreadsheet_id}/edit")


if __name__ == "__main__":
    main()
