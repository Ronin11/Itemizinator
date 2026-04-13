# Itemizer

Catalog estate items from photos using AI. People photograph items by room, the script identifies everything via Gemini, and results go into a Google Sheet that powers a simple browsing website.

## How it works

1. **Photograph** - Walk through the house, take photos of items. Upload to Google Drive in folders named by room (e.g., `Kitchen/`, `Living Room/`, `Garage/`).
2. **Process** - Run the Python script. It pulls each photo, sends it to Gemini to identify items, and writes everything to a Google Sheet.
3. **Browse & Claim** - Open the website (or the Sheet directly). Family members filter by room/category, view photos, and add their name to claim items.

## Setup

### 1. Google Cloud Project

You need a Google Cloud project with Drive and Sheets APIs enabled:

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or use an existing one)
3. Enable these APIs:
   - Google Drive API
   - Google Sheets API
4. Go to **APIs & Services > Credentials**
5. Create an **OAuth 2.0 Client ID** — you need **two**:
   - **Desktop application** — download the JSON, save as `credentials.json` (used by the Python script)
   - **Web application** — add your site's URL to Authorized JavaScript Origins (e.g., `http://localhost:8000` for local dev). Copy the Client ID for `config.js`
6. Under **OAuth consent screen**, add the email addresses of family members as test users (or publish the app)

### 2. Google Drive

Create a folder in Google Drive with this structure:

```
Estate Items/
├── Kitchen/
│   ├── photo1.jpg
│   ├── photo2.jpg
├── Living Room/
│   ├── photo1.jpg
├── Bedroom/
│   ├── photo1.jpg
└── Garage/
    ├── photo1.jpg
```

Note the **folder ID** from the URL: `drive.google.com/drive/folders/THIS_PART`

### 3. Google Sheet

1. Create a new Google Sheet
2. Note the **spreadsheet ID** from the URL: `docs.google.com/spreadsheets/d/THIS_PART/edit`
3. Share it as **"Anyone with the link can view"** (the website reads it this way)
4. Also share it as **Editor** with family members' Google accounts (so the Sheets API can write claims)

### 4. Install & Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python process.py <drive_folder_id> <spreadsheet_id>
```

On first run, a browser window will open for Google OAuth. After that, credentials are cached in `token.json`.

### 5. Website

The site is a single HTML file that reads directly from the Google Sheet.

**Option A - Local:**
```bash
cd docs
python -m http.server 8000
# Open http://localhost:8000?sheet=YOUR_SPREADSHEET_ID
```

**Option B - Configure and host anywhere:**
Edit `docs/config.js` and set your `SHEET_ID` and `GOOGLE_CLIENT_ID`. Then host the `docs/` folder anywhere (GitHub Pages, Netlify drop, etc.) — it's just static files.

The spreadsheet must be shared as **"Anyone with the link can view"** for the website to read it.

**Important:** Add your hosting URL to the OAuth Web Client's **Authorized JavaScript Origins** in Google Cloud Console (e.g., `http://localhost:8000`, `https://yoursite.github.io`).

### How Claiming Works

1. Family member visits the site and clicks **Sign in with Google**
2. They browse items and click **Claim** on what they want
3. Their Google email is written to the "Claimed By" column in the Sheet
4. They can **Unclaim** their own items — they can't unclaim someone else's
5. Items they've claimed show in blue; other claimed items show in green

## Photo Tips

- **One item per photo** when possible — AI is most accurate this way
- For shelves/groups, try to get close-ups of individual items too
- Good lighting helps with identification
- Include any labels, brand names, or markings in the shot

## Spreadsheet Columns

| Column | Description |
|--------|-------------|
| Room | Which room the item was found in |
| Item | AI-identified item name |
| Description | Color, material, style, brand |
| Category | Furniture, Electronics, Kitchen, etc. |
| Condition | Excellent / Good / Fair / Poor |
| Photo Link | Link to the original photo in Drive |
| Claimed By | Family member's name (fill this in!) |
| Priority (1-3) | How much you want it: 1=must have, 3=nice to have |
| Notes | Any notes or comments |
