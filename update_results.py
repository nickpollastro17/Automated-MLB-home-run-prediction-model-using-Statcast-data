"""
HR Prop Model — Automated Result Tracking
============================================
Resolves PENDING picks in the Google Sheets Result Tracking tab
by checking actual MLB game logs. Marks each pick as:

    YES — player hit a home run that game
    NO  — player played but did not hit a home run
    DNP — player did not play (scratched, rained out, etc.)

Sheet layout expected:
    Row 1 — column headers
    Row 2 — season summary line (auto-recalculated)
    Row 3+ — pick rows

Run this once daily (recommended: right before midnight matchup cache)
to keep your tracking history current. Safe to run multiple times —
already-resolved rows are skipped.

Usage:
    python update_results.py            # resolve all PENDING rows
    python update_results.py --date 2026-05-15   # resolve only one date
"""

import os
import sys
import time
import logging
import argparse
from datetime import date
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests

GOOGLE_SHEET_ID  = os.getenv("GOOGLE_SHEET_ID", "")
CREDENTIALS_FILE = "credentials.json"

_player_id_cache = {}


def get_player_id(name):
    if name in _player_id_cache:
        return _player_id_cache[name]

    try:
        r = requests.get(
            "https://statsapi.mlb.com/api/v1/people/search"
            f"?names={requests.utils.quote(name)}",
            timeout=8)
        r.raise_for_status()
        people = r.json().get("people", [])
        if people:
            pid = people[0]["id"]
            _player_id_cache[name] = pid
            return pid
    except Exception as e:
        log.warning(f"  Player lookup failed for {name}: {e}")

    _player_id_cache[name] = None
    return None


def check_player_result(mlbam_id, game_date):
    if not mlbam_id:
        return "DNP"

    season = game_date[:4]
    try:
        r = requests.get(
            f"https://statsapi.mlb.com/api/v1/people/{mlbam_id}"
            f"/stats?stats=gameLog&group=hitting&season={season}"
            "&sportId=1",
            timeout=10)
        r.raise_for_status()
        splits = r.json().get("stats", [{}])[0].get("splits", [])
    except Exception as e:
        log.warning(f"  Game log fetch failed for {mlbam_id}: {e}")
        return None

    for split in splits:
        if split.get("date") == game_date:
            hr = split.get("stat", {}).get("homeRuns", 0)
            return "YES" if int(hr) > 0 else "NO"

    return "DNP"


def get_sheets_client():
    import gspread
    from google.oauth2.service_account import Credentials

    if not Path(CREDENTIALS_FILE).exists():
        log.error(f"credentials.json not found in {os.getcwd()}")
        sys.exit(1)

    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(
        CREDENTIALS_FILE, scopes=scopes)
    return gspread.authorize(creds)


def update_results(target_date=None):
    if not GOOGLE_SHEET_ID:
        log.error("GOOGLE_SHEET_ID not set in .env")
        return

    log.info("="*55)
    log.info("RESULT TRACKING — AUTO RESOLVE")
    log.info("="*55)

    client = get_sheets_client()
    try:
        wb = client.open_by_key(GOOGLE_SHEET_ID)
        ws = wb.worksheet("Result Tracking")
    except Exception as e:
        log.error(f"Could not open Result Tracking tab: {e}")
        return

    all_data = ws.get_all_values()
    if len(all_data) < 3:
        log.info("No pick rows found yet (need header + summary + data).")
        return

    headers = all_data[0]
    try:
        date_col   = headers.index("Date")
        player_col = headers.index("Player")
        hit_col    = headers.index("Hit HR?")
    except ValueError:
        log.error("Expected columns (Date, Player, Hit HR?) not found")
        return

    # Row 1 = headers, Row 2 = summary, data starts row 3 (index 2)
    data_rows = all_data[2:]

    today_str = date.today().strftime("%Y-%m-%d")
    updates   = []
    resolved  = 0
    skipped   = 0

    for offset, row in enumerate(data_rows):
        row_num = offset + 3  # actual sheet row number

        if len(row) <= hit_col:
            continue

        row_date = row[date_col]
        player   = row[player_col]
        current  = row[hit_col]

        if not row_date or not player:
            continue

        if current in ("YES", "NO", "DNP"):
            skipped += 1
            continue

        if row_date >= today_str:
            continue

        if target_date and row_date != target_date:
            continue

        pid = get_player_id(player)
        time.sleep(0.05)

        result = check_player_result(pid, row_date)
        if result is None:
            continue

        updates.append((row_num, result))
        resolved += 1
        log.info(f"  {row_date}  {player:<22} -> {result}")

    if not updates:
        log.info(f"\nNothing to resolve. ({skipped} rows already resolved)")
        return

    log.info(f"\nWriting {len(updates)} results to sheet...")
    cell_list = []
    for row_num, value in updates:
        cell_list.append({
            "range": f"{chr(65 + hit_col)}{row_num}",
            "values": [[value]],
        })

    try:
        ws.batch_update(cell_list)
    except Exception as e:
        log.warning(f"Batch update failed, trying one by one: {e}")
        for row_num, value in updates:
            try:
                ws.update_cell(row_num, hit_col + 1, value)
                time.sleep(0.5)
            except Exception as e2:
                log.warning(f"  Failed to update row {row_num}: {e2}")

    _update_summary(ws, data_rows, updates, hit_col, len(headers))

    log.info(f"\nDone. {resolved} picks resolved, {skipped} already had results.")


def _update_summary(ws, data_rows, updates, hit_col, num_cols):
    """Recompute and write season hit rate summary into row 2."""
    try:
        results_map = {row_num: val for row_num, val in updates}
        total = 0
        hits  = 0

        for offset, row in enumerate(data_rows):
            row_num = offset + 3
            if row_num in results_map:
                val = results_map[row_num]
            elif len(row) > hit_col:
                val = row[hit_col]
            else:
                continue

            if val in ("YES", "NO"):
                total += 1
                if val == "YES":
                    hits += 1
            # DNP excluded entirely

        if total > 0:
            rate = round(hits / total * 100, 1)
            summary = (f"Season: {hits}/{total} hits  ({rate}% hit rate)  "
                       f"{total} resolved picks (DNP excluded)")
        else:
            summary = "Season: No resolved picks yet"

        ws.update([[summary] + [""] * (num_cols - 1)], "A2")
        log.info(f"\n  {summary}")
    except Exception as e:
        log.warning(f"  Summary update failed: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", type=str, default=None,
                    help="Resolve only this date (YYYY-MM-DD)")
    args = ap.parse_args()

    update_results(target_date=args.date)
