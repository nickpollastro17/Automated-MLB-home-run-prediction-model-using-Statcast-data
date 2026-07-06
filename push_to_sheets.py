"""
HR Prop Model — Google Sheets Push
====================================
Pushes daily picks, wind conditions, best matchup,
and result tracking to Google Sheets automatically.

Uses service account credentials (no browser auth needed).

Setup:
    1. Place credentials.json in A:/hr_prop_model/
    2. Add GOOGLE_SHEET_ID to .env
    3. Share your Google Sheet with the service account email

Usage:
    python push_to_sheets.py              # push today's picks
    python push_to_sheets.py --track      # update result tracking
"""

import os
import sys
import json
import logging
import pandas as pd
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

GOOGLE_SHEET_ID  = os.getenv("GOOGLE_SHEET_ID", "")
CREDENTIALS_FILE = "credentials.json"
SEASON           = int(os.getenv("SEASON", 2026))

VERDICT_COLORS = {
    "STRONG":  {"red": 0.827, "green": 0.945, "blue": 0.827},
    "LEAN":    {"red": 1.0,   "green": 0.973, "blue": 0.804},
    "MONITOR": {"red": 0.800, "green": 0.898, "blue": 1.0},
    "FADE":    {"red": 0.961, "green": 0.961, "blue": 0.961},
}

HEADER_COLOR  = {"red": 0.204, "green": 0.227, "blue": 0.251}
WHITE         = {"red": 1.0,   "green": 1.0,   "blue": 1.0}
SUMMARY_COLOR = {"red": 0.890, "green": 0.949, "blue": 0.992}

PARK_LF_WIND = {
    "HOU":999,"MIA":999,"TOR":999,"TB":999,"AZ":999,
    "MIL":999,"TEX":999,"SEA":999,
    "CHC": 50,"COL":340,"NYY":270,"BOS":220,"PHI":250,
    "PIT":310,"CLE":225,"DET": 65,"STL":180,"WSH":150,
    "BAL":140,"LAD":210,"SF": 90,"SD": 180,"LAA":180,
    "ATH":200,"CWS": 50,"KC": 180,"ATL":185,"NYM":150,
    "MIN":270,"CIN":120,
}

DOME_PARKS = {"TB","TOR","MIA","HOU","MIL","AZ","TEX","SEA"}


# ==========================================================================
# HELPERS
# ==========================================================================

def safe_odds_str(val):
    try:
        if val is None or pd.isna(val):
            return ""
        v = int(val)
        return f"+{v}" if v > 0 else str(v)
    except Exception:
        return ""


def safe_float(val, default=0.0):
    try:
        if val is None:
            return default
        f = float(val)
        return default if pd.isna(f) else f
    except Exception:
        return default


def model_odds_str(score):
    prob = 0.04 + (safe_float(score) / 100) * 0.18
    prob = max(0.01, min(0.99, prob))
    if prob >= 0.5:
        return f"-{round(prob / (1 - prob) * 100)}"
    return f"+{round((1 - prob) / prob * 100)}"


def edge_str(score, implied_prob):
    try:
        if not implied_prob or pd.isna(float(implied_prob)):
            return ""
        model_prob = 0.04 + (safe_float(score) / 100) * 0.18
        edge = round((model_prob - float(implied_prob)) * 100, 1)
        return f"+{edge}%" if edge >= 0 else f"{edge}%"
    except Exception:
        return ""


# ==========================================================================
# CONNECTION
# ==========================================================================

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


def open_workbook(client):
    try:
        return client.open_by_key(GOOGLE_SHEET_ID)
    except Exception as e:
        log.error(f"Could not open sheet: {e}")
        log.error("Make sure you shared the sheet with your service account email")
        sys.exit(1)


def get_or_create_worksheet(wb, title, rows=300, cols=20):
    try:
        ws = wb.worksheet(title)
        ws.clear()
    except Exception:
        ws = wb.add_worksheet(title=title, rows=rows, cols=cols)
    return ws


def batch_format(ws, requests_list):
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        scopes  = ["https://www.googleapis.com/auth/spreadsheets"]
        creds   = Credentials.from_service_account_file(
            CREDENTIALS_FILE, scopes=scopes)
        service = build("sheets", "v4", credentials=creds,
                        cache_discovery=False)
        service.spreadsheets().batchUpdate(
            spreadsheetId=GOOGLE_SHEET_ID,
            body={"requests": requests_list}
        ).execute()
    except Exception as e:
        log.warning(f"  Formatting failed: {e}")


def header_format_request(sheet_id, num_cols):
    return {
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": HEADER_COLOR,
                    "textFormat": {"foregroundColor": WHITE, "bold": True, "fontSize": 11},
                    "horizontalAlignment": "CENTER",
                }
            },
            "fields": "userEnteredFormat",
        }
    }


def auto_resize_request(sheet_id, num_cols):
    return {
        "autoResizeDimensions": {
            "dimensions": {"sheetId": sheet_id, "dimension": "COLUMNS",
                           "startIndex": 0, "endIndex": num_cols}
        }
    }


def freeze_rows_request(sheet_id, count=1):
    return {
        "updateSheetProperties": {
            "properties": {"sheetId": sheet_id,
                           "gridProperties": {"frozenRowCount": count}},
            "fields": "gridProperties.frozenRowCount",
        }
    }


# ==========================================================================
# TAB 1 — DAILY PICKS
# ==========================================================================

def push_daily_picks(picks_df, wb):
    log.info("  Pushing daily picks...")

    today_str = date.today().strftime("%b %d %Y")
    ws        = get_or_create_worksheet(wb, f"Picks {today_str}")

    headers = [
        "Verdict","Score","Player","Team","Pitcher",
        "Brl%","EV50","HH%","Form","Park","Wind",
        "SP","BP","Model Odds","Book Odds","Edge","Platoon"
    ]

    all_rows   = [headers]
    row_colors = {}

    for verdict in ["STRONG","LEAN","MONITOR"]:
        group = picks_df[picks_df["verdict"] == verdict]
        if group.empty:
            continue

        sep = [f"── {verdict} ── {len(group)} picks"] + [""] * (len(headers) - 1)
        all_rows.append(sep)

        for _, r in group.iterrows():
            plat = "★" if r.get("platoon") else ""
            row  = [
                r["verdict"],
                round(safe_float(r["score"]), 1),
                str(r["name"]) + plat,
                str(r["team"]),
                str(r["pitcher"]),
                round(safe_float(r["barrel_pct"]), 1),
                round(safe_float(r["ev50"]), 1),
                round(safe_float(r["hard_hit_pct"]), 1),
                str(r.get("form_pct", "0%")),
                int(safe_float(r["park_factor"], 100)),
                str(r.get("wind", "")),
                round(safe_float(r["sp_score"]), 1),
                round(safe_float(r["bullpen_score"]), 1),
                model_odds_str(r["score"]),
                safe_odds_str(r.get("best_odds")),
                edge_str(r["score"], r.get("implied_prob")),
                "YES" if r.get("platoon") else "",
            ]
            all_rows.append(row)
            row_colors[len(all_rows)] = verdict

        all_rows.append([""] * len(headers))

    ws.update(all_rows, "A1")
    import time
    time.sleep(2)

    fmt_requests = [
        header_format_request(ws.id, len(headers)),
        freeze_rows_request(ws.id, 1),
        auto_resize_request(ws.id, len(headers)),
    ]
    for row_num, verdict in row_colors.items():
        bg = VERDICT_COLORS.get(verdict, WHITE)
        fmt_requests.append({
            "repeatCell": {
                "range": {"sheetId": ws.id, "startRowIndex": row_num - 1, "endRowIndex": row_num},
                "cell": {"userEnteredFormat": {"backgroundColor": bg}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        })

    batch_format(ws, fmt_requests)
    log.info(f"  OK  {len(all_rows)} rows pushed to '{ws.title}'")


# ==========================================================================
# TAB 2 — WIND CONDITIONS
# ==========================================================================

def push_wind_conditions(weather_map, games, wb):
    log.info("  Pushing wind conditions...")
    import time

    ws = get_or_create_worksheet(wb, "Wind Conditions")

    headers = [
        "Park","Temp","Wind MPH","Direction",
        "Condition","LHB Effect","RHB Effect","Notes"
    ]

    all_rows    = [headers]
    today_parks = set(g["home_team"] for g in games)

    for park in sorted(today_parks):
        w = weather_map.get(park, {})
        if not w:
            continue

        is_dome  = w.get("is_dome", False)
        temp_f   = safe_float(w.get("temp_f", 72))
        wind_mph = safe_float(w.get("wind_speed_mph", 0))
        wind_deg = int(safe_float(w.get("wind_deg", 0)))
        cond     = w.get("condition", "unknown")

        dirs = ["N","NE","E","SE","S","SW","W","NW","N"]
        card = dirs[round(wind_deg / 45) % 8]

        if is_dome:
            lhb   = rhb = "DOME — Neutral"
            notes = "Retractable or fixed dome"
        else:
            def effect(hand):
                lf   = PARK_LF_WIND.get(park, 270)
                if lf == 999:
                    return "DOME"
                tgt  = lf if hand == "R" else (lf + 180) % 360
                diff = min(abs(wind_deg - tgt) % 360, 360 - abs(wind_deg - tgt) % 360)
                if diff <= 30:    return f"OUT {wind_mph}mph"
                elif diff <= 60:  return f"Favorable {wind_mph}mph"
                elif diff <= 90:  return f"Crosswind {wind_mph}mph"
                elif diff <= 150: return f"Unfavorable {wind_mph}mph"
                else:             return f"IN {wind_mph}mph"

            lhb   = effect("L")
            rhb   = effect("R")
            notes = f"{temp_f}F  Gust {w.get('wind_gust_mph', 0)}mph"

        all_rows.append([
            park, f"{temp_f}F", f"{wind_mph} mph", f"{card} ({wind_deg})",
            cond, lhb, rhb, notes
        ])

    ws.update(all_rows, "A1")
    time.sleep(2)
    batch_format(ws, [
        header_format_request(ws.id, len(headers)),
        auto_resize_request(ws.id, len(headers)),
    ])
    log.info(f"  OK  Wind for {len(today_parks)} parks")


# ==========================================================================
# TAB 3 — BEST MATCHUP
# ==========================================================================

def push_best_matchup(picks_df, wb):
    log.info("  Pushing best matchup...")
    import time

    ws = get_or_create_worksheet(wb, "Best Matchup")

    if not picks_df.empty and "matchup_score" in picks_df.columns:
        best = picks_df.loc[picks_df["matchup_score"].idxmax()]
    else:
        best = picks_df.iloc[0]

    today_str = date.today().strftime("%B %d, %Y")

    all_rows = [
        [f"BEST MATCHUP OF THE DAY  |  {today_str}", ""],
        ["", ""],
        ["Player",        str(best["name"])],
        ["Team",          str(best["team"])],
        ["Vs Pitcher",    str(best["pitcher"])],
        ["Overall Score", f"{safe_float(best['score']):.1f}"],
        ["Verdict",       str(best["verdict"])],
        ["Matchup Score", f"{safe_float(best.get('matchup_score', 0)):.1f} / 8.0"],
        ["Barrel%",       f"{safe_float(best['barrel_pct']):.1f}%"],
        ["EV50",          f"{safe_float(best['ev50']):.1f} mph"],
        ["Hard Hit%",     f"{safe_float(best['hard_hit_pct']):.1f}%"],
        ["SP Proneness",  f"{safe_float(best['sp_score']):.1f} / 10"],
        ["Bullpen Score", f"{safe_float(best['bullpen_score']):.1f} / 10"],
        ["Wind",          str(best.get("wind", "N/A"))],
        ["Park Factor",   str(int(safe_float(best["park_factor"], 100)))],
        ["Model Odds",    model_odds_str(best["score"])],
        ["Platoon Edge",  "YES ★" if best.get("platoon") else "No"],
        ["Form",          str(best.get("form_pct", "0%"))],
        ["", ""],
        ["Note: Best matchup = highest pitch arsenal compatibility score across entire slate.", ""],
    ]

    ws.update(all_rows, "A1")
    time.sleep(2)
    batch_format(ws, [
        header_format_request(ws.id, 2),
        auto_resize_request(ws.id, 2),
    ])
    log.info(f"  OK  Best matchup: {best['name']}")


# ==========================================================================
# TAB 4 — RESULT TRACKING (summary pinned at top)
# ==========================================================================

def push_result_tracking(picks_df, wb):
    """
    Layout:
        Row 1 — column headers
        Row 2 — season summary (always recalculated, pinned under header)
        Row 3+ — pick rows, newest appended at bottom
    """
    log.info("  Pushing result tracking...")
    import time

    today_str = date.today().strftime("%Y-%m-%d")
    headers   = [
        "Date","Player","Team","Pitcher","Score",
        "Verdict","Book Odds","Model Odds","Model Prob%",
        "Implied Prob%","Edge","Hit HR?","Profit/Loss","Notes"
    ]

    is_new = False
    try:
        ws = wb.worksheet("Result Tracking")
    except Exception:
        ws = wb.add_worksheet(title="Result Tracking", rows=500, cols=15)
        is_new = True

    if is_new:
        # headers + placeholder summary row
        ws.update([headers, ["Season: No resolved picks yet"] + [""]*(len(headers)-1)], "A1")
        time.sleep(2)
        batch_format(ws, [
            header_format_request(ws.id, len(headers)),
            freeze_rows_request(ws.id, 2),
        ])
        existing_data_rows = []
    else:
        all_existing = ws.get_all_values()
        # data rows are everything from row 3 onward (row 1=header, row 2=summary)
        existing_data_rows = all_existing[2:] if len(all_existing) > 2 else []

    dates_present = [r[0] for r in existing_data_rows if r]
    if today_str in dates_present:
        log.info("  Today already in tracking — skipping")
        return

    strong_lean = picks_df[picks_df["verdict"].isin(["STRONG","LEAN"])].head(25)

    new_rows = []
    for _, r in strong_lean.iterrows():
        impl     = safe_float(r.get("implied_prob"), 0)
        impl_str = f"{round(impl * 100, 1)}%" if impl else ""
        new_rows.append([
            today_str,
            str(r["name"]),
            str(r["team"]),
            str(r["pitcher"]),
            round(safe_float(r["score"]), 1),
            str(r["verdict"]),
            safe_odds_str(r.get("best_odds")),
            model_odds_str(r["score"]),
            f"{round((0.04 + safe_float(r['score'])/100*0.18)*100, 1)}%",
            impl_str,
            edge_str(r["score"], r.get("implied_prob")),
            "PENDING",
            "",
            "",
        ])

    if new_rows:
        next_row = len(existing_data_rows) + 3  # +2 header/summary, +1 to get next empty row
        ws.update(new_rows, f"A{next_row}")
        time.sleep(2)

    # Recalculate summary from all data rows (existing + new)
    all_rows_for_summary = existing_data_rows + new_rows
    resolved = [r for r in all_rows_for_summary
                if r and len(r) >= 12 and r[11] in ("YES","NO")]
    total = len(resolved)
    hits  = len([r for r in resolved if r[11] == "YES"])

    if total > 0:
        rate    = round(hits / total * 100, 1)
        summary = (f"Season: {hits}/{total} hits  ({rate}% hit rate)  "
                   f"{total} resolved picks (DNP excluded)")
    else:
        summary = "Season: No resolved picks yet"

    ws.update([[summary] + [""]*(len(headers)-1)], "A2")

    log.info(f"  OK  {len(new_rows)} picks added to tracking")
    log.info(f"  {summary}")


# ==========================================================================
# MAIN
# ==========================================================================

def push_all(picks_df=None, weather_map=None, games=None):
    if not GOOGLE_SHEET_ID:
        log.warning("  GOOGLE_SHEET_ID not set in .env — skipping")
        return

    if not Path(CREDENTIALS_FILE).exists():
        log.warning("  credentials.json not found — skipping")
        return

    today_str = date.today().strftime("%Y-%m-%d")

    if picks_df is None:
        csv_file = f"data/hr_picks_{today_str}.csv"
        if not Path(csv_file).exists():
            log.error(f"  No picks file: {csv_file}")
            return
        picks_df = pd.read_csv(csv_file)

    if weather_map is None:
        wf = f"data/weather_{today_str}.json"
        weather_map = json.load(open(wf)) if Path(wf).exists() else {}

    if games is None:
        gf = f"data/schedule_{today_str}.json"
        games = json.load(open(gf)) if Path(gf).exists() else []

    log.info("\n" + "="*50)
    log.info("PUSHING TO GOOGLE SHEETS")
    log.info("="*50)

    client = get_sheets_client()
    wb     = open_workbook(client)

    try:
        push_daily_picks(picks_df, wb)
    except Exception as e:
        log.error(f"  Picks push failed: {e}")

    try:
        push_wind_conditions(weather_map, games, wb)
    except Exception as e:
        log.error(f"  Wind push failed: {e}")

    try:
        push_best_matchup(picks_df, wb)
    except Exception as e:
        log.error(f"  Matchup push failed: {e}")

    try:
        push_result_tracking(picks_df, wb)
    except Exception as e:
        log.error(f"  Tracking push failed: {e}")

    log.info("\n  All tabs pushed")
    log.info(f"  https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--track", action="store_true")
    args = ap.parse_args()

    if args.track:
        today_str = date.today().strftime("%Y-%m-%d")
        csv_file  = f"data/hr_picks_{today_str}.csv"
        if Path(csv_file).exists():
            df     = pd.read_csv(csv_file)
            client = get_sheets_client()
            wb     = open_workbook(client)
            push_result_tracking(df, wb)
        else:
            log.error(f"No picks file for today: {csv_file}")
    else:
        push_all()
