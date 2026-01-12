import os
import sys
import json
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()
if not os.environ.get("SPREADSHEET_ID"):
    load_dotenv(dotenv_path=os.path.abspath(".env.example"), override=False)

# Use env vars if available; fall back to sensible defaults for local dev
SA_PATH = os.environ.get("GOOGLE_SA_FILE") or os.path.expanduser("~/Desktop/service-account.json")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
RANGE = os.environ.get("TEST_RANGE", "Sheet1!A1:E10")
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

# Helpful errors
if not os.path.exists(os.path.expanduser(SA_PATH)):
    print(f"ERROR: Service account file not found: {SA_PATH}", file=sys.stderr)
    print("Set GOOGLE_SA_FILE to the path of your service-account.json or place it at ~/Desktop/service-account.json", file=sys.stderr)
    sys.exit(2)

if not SPREADSHEET_ID:
    print("ERROR: SPREADSHEET_ID not set. Export SPREADSHEET_ID=<id> or add it to your .env file.", file=sys.stderr)
    sys.exit(3)

# Load creds and call APIs
sa = json.load(open(os.path.expanduser(SA_PATH)))
creds = service_account.Credentials.from_service_account_info(sa, scopes=SCOPES)

drive = build("drive", "v3", credentials=creds)
print("Drive files:", drive.files().list(pageSize=5, fields="files(id,name)").execute().get("files", []))

sheets = build("sheets", "v4", credentials=creds)

# Helper: list sheet titles
meta = sheets.spreadsheets().get(spreadsheetId=SPREADSHEET_ID, fields="sheets(properties(title))").execute()
sheet_titles = [s['properties']['title'] for s in meta.get('sheets', [])]
print(f"Available sheets: {sheet_titles}")

# Determine target sheet and range
TARGET_SHEET = os.environ.get('SEARCH_SHEET') or sheet_titles[0]
print(f"Target sheet: {TARGET_SHEET}")
READ_RANGE = os.environ.get('SEARCH_RANGE') or f"'{TARGET_SHEET}'!A1:Z1000"
print(f"Reading range: {READ_RANGE}")

# Fetch rows
resp = sheets.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range=READ_RANGE).execute()
rows = resp.get('values', [])
print(f"Fetched {len(rows)} rows from sheet '{TARGET_SHEET}'")

# Search for row key and column header
SEARCH_ROW_KEY = os.environ.get('SEARCH_ROW_KEY', 'Paypal AUD Account')
SEARCH_COL_HEADER = os.environ.get('SEARCH_COL_HEADER', 'Nov. 2025')

# Normalize helper
import re

def norm(s):
    return re.sub(r"[^\w]", "", s).lower() if s is not None else ""

# Find header row (first non-empty row) and column index
header_row_idx = None
for i, r in enumerate(rows[:10]):
    if any(cell.strip() for cell in r):
        header_row_idx = i
        break

if header_row_idx is None:
    print("No header row found in first 10 rows")
else:
    header = rows[header_row_idx]
    print(f"Using header row {header_row_idx}: {header}")
    target_col_idx = None
    search_col_norm = norm(SEARCH_COL_HEADER)
    for j, h in enumerate(header):
        if search_col_norm in norm(h):
            target_col_idx = j
            break
    if target_col_idx is None:
        print(f"Column header containing '{SEARCH_COL_HEADER}' not found in header")
    else:
        print(f"Found column '{header[target_col_idx]}' at index {target_col_idx}")

# Find the row that has the SEARCH_ROW_KEY
row_idx = None
search_row_norm = norm(SEARCH_ROW_KEY)
for i, r in enumerate(rows):
    for cell in r:
        if search_row_norm in norm(cell):
            row_idx = i
            break
    if row_idx is not None:
        break

if row_idx is None:
    print(f"Row containing '{SEARCH_ROW_KEY}' not found")
else:
    print(f"Found row at index {row_idx}: {rows[row_idx]}")
    # Read intersection value if both found
    if 'target_col_idx' in locals() and target_col_idx is not None:
        row = rows[row_idx]
        if target_col_idx < len(row):
            val = row[target_col_idx]
            print(f"Value at row '{SEARCH_ROW_KEY}' and column '{SEARCH_COL_HEADER}': {val}")
        else:
            print(f"Row found but no value in column index {target_col_idx}")

