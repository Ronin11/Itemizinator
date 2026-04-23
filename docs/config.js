// Paste your Google Spreadsheet ID here.
// It's the long string in the URL: docs.google.com/spreadsheets/d/THIS_PART/edit
const SHEET_ID = "14BJ0SP5CfG1dmnL6H50ATRCxTAQVHctQm6hnFPmWZq8";

// Your Google OAuth Client ID (Web application type) from Google Cloud Console
// APIs & Services > Credentials > OAuth 2.0 Client IDs
const GOOGLE_CLIENT_ID = "394650427422-fo3mof852bdl7edml55539vjdjs4v3t6.apps.googleusercontent.com";

// Optional: customize the page title
const SHEET_NAME = "Grandma's Estate Items";

// Apps Script Web App /exec URL that powers the LLM search tab.
// Get it from: Apps Script editor > Deploy > New Deployment > Web App > copy URL
// Looks like: https://script.google.com/macros/s/AKfyc.../exec
// Leave empty ("") to keep the search tab on the mock responses.
const SEARCH_URL = "https://script.google.com/macros/s/AKfycbyJAC3SRGpC1t3OtAzIe5AkzlEPqDvCIj43geR6Y6ff8uKA9rY302x1dRdNHLIUGNRDFg/exec";
