"""
HR Prop Model v4 — Optimized Data Pipeline
============================================
Major efficiency improvements over v3:
  - SQLite replaces raw Statcast CSV (fast date queries)
  - Batch MLB API roster calls replace individual player lookups
  - Async weather fetch (all parks simultaneously)
  - 24-hour cache on all leaderboard pulls
  - Retry logic on all external API calls
  - Single startup data load passed through all functions

Target runtime: under 60 seconds daily

Usage:
  python setup_and_pull.py --no-arsenal            (~45 sec)
  python setup_and_pull.py --no-arsenal --no-matchup  (~20 sec)
"""

import os
import sys
import json
import time
import sqlite3
import asyncio
import argparse
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from functools import wraps

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

WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "")
ODDS_API_KEY    = os.getenv("ODDS_API_KEY", "")
SEASON          = int(os.getenv("SEASON", 2026))

if sys.version_info < (3, 9):
    print("ERROR: Python 3.9+ required")
    sys.exit(1)

TEAM_ID_MAP = {
    108:"LAA", 109:"AZ",  110:"BAL", 111:"BOS", 112:"CHC",
    113:"CIN", 114:"CLE", 115:"COL", 116:"DET", 117:"HOU",
    118:"KC",  119:"LAD", 120:"WSH", 121:"NYM", 133:"ATH",
    134:"PIT", 135:"SD",  136:"SEA", 137:"SF",  138:"STL",
    139:"TB",  140:"TEX", 141:"TOR", 142:"MIN", 143:"PHI",
    144:"ATL", 145:"CWS", 146:"MIA", 147:"NYY", 158:"MIL",
}

PARK_COORDS = {
    "COL":(39.7559,-104.9942), "PHI":(39.9057,-75.1665),
    "AZ": (33.4453,-112.0667), "BOS":(42.3467,-71.0972),
    "DET":(42.3390,-83.0485),  "CHC":(41.9484,-87.6553),
    "LAD":(34.0739,-118.2400), "HOU":(29.7573,-95.3555),
    "MIN":(44.9817,-93.2781),  "CIN":(39.0974,-84.5082),
    "NYY":(40.8296,-73.9262),  "STL":(38.6226,-90.1928),
    "KC": (39.0517,-94.4803),  "CLE":(41.4962,-81.6852),
    "WSH":(38.8730,-77.0074),  "PIT":(40.4469,-80.0057),
    "TB": (27.7683,-82.6534),  "BAL":(39.2838,-76.6218),
    "ATH":(37.7516,-122.2005), "CWS":(41.8300,-87.6339),
    "NYM":(40.7571,-73.8458),  "MIL":(43.0280,-87.9712),
    "TOR":(43.6414,-79.3894),  "LAA":(33.8003,-117.8827),
    "TEX":(32.7512,-97.0832),  "ATL":(33.8908,-84.4678),
    "SF": (37.7786,-122.3893), "SD": (32.7076,-117.1570),
    "SEA":(47.5914,-122.3325), "MIA":(25.7781,-80.2197),
}

DOME_PARKS = {"TB","TOR","MIA","HOU","MIL","AZ","TEX","SEA"}

TOMORROW_CODES = {
    1000:"clear sky",1100:"mostly clear",1101:"partly cloudy",
    1102:"mostly cloudy",1001:"cloudy",2000:"fog",
    4000:"drizzle",4001:"rain",4200:"light rain",
    4201:"heavy rain",5000:"snow",8000:"thunderstorm",
}


def with_retry(max_attempts=3, delay=2.0):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            wait = delay
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_error = e
                    if attempt < max_attempts - 1:
                        time.sleep(wait)
                        wait *= 2.0
            log.warning(f"{func.__name__} failed: {last_error}")
            return None
        return wrapper
    return decorator


def cache_fresh(filepath, hours=24):
    if not Path(filepath).exists():
        return False
    age = datetime.now() - datetime.fromtimestamp(
        Path(filepath).stat().st_mtime)
    return age.total_seconds() < hours * 3600


# ==========================================================================
# STEP 1 — VERIFY IMPORTS
# ==========================================================================

def verify_imports():
    print("\n" + "-"*50)
    print("STEP 1 — Checking packages")
    print("-"*50)
    required = {
        "pybaseball":"pip install pybaseball",
        "pandas":    "pip install pandas",
        "numpy":     "pip install numpy",
        "requests":  "pip install requests",
        "dotenv":    "pip install python-dotenv",
        "schedule":  "pip install schedule",
        "openpyxl":  "pip install openpyxl",
        "aiohttp":   "pip install aiohttp",
    }
    missing = []
    for pkg, cmd in required.items():
        try:
            __import__("dotenv" if pkg == "dotenv" else pkg)
            print(f"  OK  {pkg}")
        except ImportError:
            print(f"  MISSING  {pkg}  ->  {cmd}")
            missing.append(pkg)
    if missing:
        print(f"\n  Install missing: pip install -r requirements.txt\n")
        sys.exit(1)
    print("  All packages OK")


# ==========================================================================
# STEP 2 — MLB SCHEDULE
# ==========================================================================

@with_retry()
def pull_mlb_schedule(save=True):
    import requests

    print("\n" + "-"*50)
    print("STEP 2 — Today's MLB schedule")
    print("-"*50)

    today = date.today().strftime("%Y-%m-%d")
    r = requests.get(
        "https://statsapi.mlb.com/api/v1/schedule"
        f"?sportId=1&date={today}&hydrate=probablePitcher,team",
        timeout=15)
    r.raise_for_status()

    games = []
    for gd in r.json().get("dates", []):
        for g in gd.get("games", []):
            home      = g["teams"]["home"]
            away      = g["teams"]["away"]
            home_abbr = TEAM_ID_MAP.get(
                home["team"]["id"],
                home["team"].get("abbreviation","???"))
            away_abbr = TEAM_ID_MAP.get(
                away["team"]["id"],
                away["team"].get("abbreviation","???"))
            hp = home.get("probablePitcher",{})
            ap = away.get("probablePitcher",{})
            games.append({
                "game_pk":           g["gamePk"],
                "game_time_utc":     g.get("gameDate",""),
                "home_team":         home_abbr,
                "away_team":         away_abbr,
                "home_team_id":      home["team"]["id"],
                "away_team_id":      away["team"]["id"],
                "home_pitcher":      hp.get("fullName","TBD"),
                "home_pitcher_id":   hp.get("id"),
                "home_pitcher_hand": None,
                "away_pitcher":      ap.get("fullName","TBD"),
                "away_pitcher_id":   ap.get("id"),
                "away_pitcher_hand": None,
            })

    print(f"  OK  {len(games)} games")
    for g in games:
        print(f"       {g['away_team']:>3} @ {g['home_team']:<3}  "
              f"{g['away_pitcher']:<22} vs {g['home_pitcher']}")

    if save and games:
        Path("data").mkdir(exist_ok=True)
        with open(f"data/schedule_{today}.json","w") as f:
            json.dump(games, f, indent=2)
    return games


# ==========================================================================
# STEP 3 — BATCH PLAYER INFO
# ==========================================================================

def pull_all_player_info(games):
    """
    One batch roster call per team instead of one per player.
    Replaces hundreds of individual MLB API calls.
    """
    import requests

    print("\n" + "-"*50)
    print("STEP 3 — Batch player info")
    print("-"*50)

    pitcher_hands = {}
    batter_info   = {}

    team_ids    = set()
    pitcher_ids = set()
    for g in games:
        team_ids.add(g["home_team_id"])
        team_ids.add(g["away_team_id"])
        if g["home_pitcher_id"]:
            pitcher_ids.add(g["home_pitcher_id"])
        if g["away_pitcher_id"]:
            pitcher_ids.add(g["away_pitcher_id"])

    # Pitcher hands
    print(f"  Fetching {len(pitcher_ids)} pitcher hands...")
    for pid in pitcher_ids:
        try:
            r = requests.get(
                f"https://statsapi.mlb.com/api/v1/people/{pid}",
                timeout=8)
            r.raise_for_status()
            p    = r.json().get("people",[{}])[0]
            hand = p.get("pitchHand",{}).get("code","R")
            pitcher_hands[pid] = hand
        except Exception:
            pitcher_hands[pid] = "R"
        time.sleep(0.03)

    # Full rosters — one call per team
    print(f"  Fetching {len(team_ids)} team rosters...")
    for team_id in team_ids:
        team_abbr = TEAM_ID_MAP.get(team_id,"???")
        try:
            r = requests.get(
                f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster"
                "?rosterType=active"
                "&hydrate=person(batSide,currentTeam)",
                timeout=10)
            r.raise_for_status()
            roster = r.json().get("roster",[])
            added  = 0
            for player in roster:
                pid  = player["person"]["id"]
                name = player["person"]["fullName"]
                pos  = player.get("position",{}).get("abbreviation","")
                if pos == "P":
                    continue
                hand = (player["person"]
                        .get("batSide",{})
                        .get("code","R"))
                batter_info[pid] = {
                    "name": name,
                    "team": team_abbr,
                    "hand": hand,
                }
                added += 1
            print(f"  {team_abbr}: {added} batters")
        except Exception as e:
            log.warning(f"  {team_abbr} roster failed: {e}")
        time.sleep(0.05)

    # Update pitcher hands in games
    for i, g in enumerate(games):
        games[i]["home_pitcher_hand"] = pitcher_hands.get(
            g["home_pitcher_id"],"R")
        games[i]["away_pitcher_hand"] = pitcher_hands.get(
            g["away_pitcher_id"],"R")

    print(f"\n  OK  {len(pitcher_hands)} pitchers, "
          f"{len(batter_info)} batters")
    return games, pitcher_hands, batter_info


# ==========================================================================
# STEP 4 — BATTER STATCAST WITH 24H CACHE + SQLITE
# ==========================================================================

DB_PATH = "data/statcast.db"


def get_db():
    Path("data").mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=10000")
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS batted_balls (
            game_date    TEXT,
            batter       INTEGER,
            stand        TEXT,
            bb_type      TEXT,
            hc_x         REAL,
            events       TEXT,
            type         TEXT,
            inning       INTEGER,
            launch_speed REAL,
            launch_angle REAL,
            PRIMARY KEY (game_date, batter, hc_x, launch_speed)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_date "
        "ON batted_balls(game_date)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_batter "
        "ON batted_balls(batter)")
    conn.commit()
    conn.close()


def pull_batter_statcast(season=SEASON, save=True,
                          batter_info=None):
    import pandas as pd
    import numpy as np
    from pybaseball import (
        statcast_batter_exitvelo_barrels,
        statcast_batter_expected_stats,
        cache,
    )
    cache.enable()

    print("\n" + "-"*50)
    print(f"STEP 4 — Batter Statcast ({season})")
    print("-"*50)

    ev_cache   = f"data/cache_ev_barrels_{season}.json"
    exp_cache  = f"data/cache_expected_{season}.json"
    form_cache = f"data/cache_rolling_form_{season}.json"

    # Exit velocity + barrels (24h cache)
    if cache_fresh(ev_cache, 24):
        print("  Exit velocity: 24h cache")
        ev_df = pd.read_json(ev_cache)
    else:
        print("  Pulling exit velocity + barrel data...")
        try:
            ev_df = statcast_batter_exitvelo_barrels(season, minBBE=25)
            ev_df.to_json(ev_cache, orient="records")
            print(f"  OK  {len(ev_df)} batters — cached")
        except Exception as e:
            print(f"  ERROR: {e}")
            return pd.DataFrame()

    # Expected stats (24h cache)
    if cache_fresh(exp_cache, 24):
        print("  Expected stats: 24h cache")
        exp_df = pd.read_json(exp_cache)
    else:
        print("  Pulling expected stats...")
        try:
            exp_df = statcast_batter_expected_stats(season, minPA=25)
            exp_df.to_json(exp_cache, orient="records")
            print(f"  OK  {len(exp_df)} batters — cached")
        except Exception as e:
            print(f"  WARNING: {e}")
            exp_df = pd.DataFrame()

    # Fix name
    def fix_name(df):
        for c in df.columns:
            if "last_name" in c.lower():
                df["name"] = df[c].apply(
                    lambda x: " ".join(str(x).split(", ")[::-1])
                    if ", " in str(x) else str(x))
                break
        return df

    ev_df  = fix_name(ev_df)
    if not exp_df.empty:
        exp_df = fix_name(exp_df)

    ev_rename = {
        "brl_percent":"barrel_pct","brl_pa":"barrel_pa_pct",
        "avg_hit_speed":"ev_avg","ev50":"ev50",
        "ev95percent":"hard_hit_pct","barrels":"barrels_raw",
        "attempts":"bbe","player_id":"mlbam_id",
    }
    ev_df = ev_df.rename(columns={
        k:v for k,v in ev_rename.items() if k in ev_df.columns})

    if not exp_df.empty and "player_id" in exp_df.columns:
        exp_df = exp_df.rename(columns={"player_id":"mlbam_id"})
        if "slg" in exp_df.columns and "woba" in exp_df.columns:
            exp_df["ops_approx"] = (
                exp_df["woba"]/1.15 + exp_df["slg"]).round(3)
        keep = [c for c in
                ["mlbam_id","pa","est_ba","est_slg",
                 "est_woba","ops_approx"]
                if c in exp_df.columns]
        ev_df = ev_df.merge(exp_df[keep], on="mlbam_id", how="left")

    # SQLite rates
    batted_stats = pull_batted_ball_rates_sqlite(season)
    if not batted_stats.empty:
        ev_df = ev_df.merge(batted_stats, on="mlbam_id", how="left")
    else:
        ev_df["hr_fb_rate"]    = np.nan
        ev_df["pull_air_rate"] = np.nan
        ev_df["fb_pct"]        = np.nan

    # Rolling form (4h cache)
    if cache_fresh(form_cache, 4):
        print("  Rolling form: 4h cache")
        form_df = pd.read_json(form_cache)
    else:
        form_df = compute_rolling_form_sqlite(season)
        if not form_df.empty:
            form_df.to_json(form_cache, orient="records")

    if not form_df.empty:
        ev_df = ev_df.merge(form_df, on="mlbam_id", how="left")
    else:
        ev_df["form_score"] = 0.0
        ev_df["form_pct"]   = "0%"

    # Team + hand from batch batter_info (zero API calls)
    if batter_info:
        print("  Applying team/hand from batch roster...")
        ev_df["team"] = ev_df["mlbam_id"].apply(
            lambda x: batter_info.get(int(x),{}).get("team","???")
            if not pd.isna(x) else "???")
        ev_df["hand"] = ev_df["mlbam_id"].apply(
            lambda x: batter_info.get(int(x),{}).get("hand","R")
            if not pd.isna(x) else "R")
    else:
        ev_df["team"] = "???"
        ev_df["hand"] = "R"

    for col in ["barrel_pct","hard_hit_pct",
                "hr_fb_rate","pull_air_rate"]:
        if col in ev_df.columns:
            ev_df[col] = pd.to_numeric(ev_df[col], errors="coerce")

    print(f"\n  FINAL: {len(ev_df)} batters")

    if save:
        Path("data").mkdir(exist_ok=True)
        ev_df.to_csv(f"data/batters_{season}.csv", index=False)
        print(f"  Saved -> data/batters_{season}.csv")

    return ev_df


def pull_batted_ball_rates_sqlite(season=SEASON):
    import pandas as pd
    import numpy as np
    from pybaseball import statcast, cache
    cache.enable()

    init_db()
    season_start = f"{season}-03-20"
    today_str    = date.today().strftime("%Y-%m-%d")

    print("\n  Updating SQLite Statcast DB...")

    conn     = get_db()
    last_row = conn.execute(
        "SELECT MAX(game_date) FROM batted_balls"
    ).fetchone()[0]
    conn.close()

    if last_row:
        fetch_from = (
            datetime.strptime(last_row,"%Y-%m-%d") +
            timedelta(days=1)
        ).strftime("%Y-%m-%d")
        if fetch_from > today_str:
            print("  DB current")
            return _compute_rates_sqlite(season)
        print(f"  Fetching from {fetch_from}...")
    else:
        fetch_from = season_start
        print("  Empty DB — fetching full season (~5 min)...")

    try:
        new_data = statcast(start_dt=fetch_from, end_dt=today_str)
    except Exception as e:
        print(f"  WARNING: {e}")
        return pd.DataFrame()

    if new_data.empty:
        return _compute_rates_sqlite(season)

    keep = ["game_date","batter","stand","bb_type","hc_x",
            "events","type","inning","launch_speed","launch_angle"]
    new_data = new_data[[c for c in keep if c in new_data.columns]]

    conn = get_db()
    rows = 0
    for _, row in new_data.iterrows():
        try:
            conn.execute("""
                INSERT OR IGNORE INTO batted_balls
                (game_date,batter,stand,bb_type,hc_x,
                 events,type,inning,launch_speed,launch_angle)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                str(row.get("game_date",""))[:10],
                int(row["batter"]) if not pd.isna(
                    row.get("batter")) else None,
                str(row.get("stand","")),
                str(row.get("bb_type","")),
                float(row["hc_x"]) if not pd.isna(
                    row.get("hc_x")) else None,
                str(row.get("events","")),
                str(row.get("type","")),
                int(row["inning"]) if not pd.isna(
                    row.get("inning")) else None,
                float(row["launch_speed"]) if not pd.isna(
                    row.get("launch_speed")) else None,
                float(row["launch_angle"]) if not pd.isna(
                    row.get("launch_angle")) else None,
            ))
            rows += 1
        except Exception:
            continue
    conn.commit()
    conn.close()
    print(f"  DB updated ({rows} rows)")
    return _compute_rates_sqlite(season)


def _compute_rates_sqlite(season=SEASON):
    import pandas as pd
    import numpy as np

    conn = get_db()
    df   = pd.read_sql("""
        SELECT batter,stand,bb_type,hc_x,events,launch_speed
        FROM batted_balls
        WHERE type='X' AND game_date >= ?
    """, conn, params=(f"{season}-03-20",))
    conn.close()

    if df.empty:
        return pd.DataFrame()

    fly   = df[df["bb_type"]=="fly_ball"]
    hrs   = df[df["events"]=="home_run"]
    fb_c  = fly.groupby("batter").size().rename("fb_count")
    hr_c  = hrs.groupby("batter").size().rename("hr_count")
    bbe_c = df.groupby("batter").size().rename("bbe_count")
    rates = pd.concat([bbe_c,fb_c,hr_c],axis=1).fillna(0)
    rates["hr_fb_rate"] = (
        rates["hr_count"]/rates["fb_count"].replace(0,np.nan)*100
    ).round(1)
    rates["fb_pct"] = (
        rates["fb_count"]/rates["bbe_count"]*100).round(1)

    df["hc_x"] = pd.to_numeric(df["hc_x"],errors="coerce")
    df["is_pull_air"] = (
        df["bb_type"].isin(["fly_ball","line_drive"]) &
        df["hc_x"].notna() &
        (
            ((df["stand"]=="R")&(df["hc_x"]<100)) |
            ((df["stand"]=="L")&(df["hc_x"]>150))
        )
    )
    pull_c = (df[df["is_pull_air"]].groupby("batter")
              .size().rename("pull_air_count"))
    rates  = rates.join(pull_c)
    rates["pull_air_rate"] = (
        rates["pull_air_count"].fillna(0) /
        rates["bbe_count"]*100).round(1)

    result = rates.reset_index().rename(
        columns={"batter":"mlbam_id"})
    print(f"  OK  Rates for {len(result)} batters")
    return result[["mlbam_id","hr_fb_rate","fb_pct","pull_air_rate"]]


def compute_rolling_form_sqlite(season=SEASON):
    import pandas as pd
    import numpy as np

    today     = pd.Timestamp(date.today())
    c15       = today - timedelta(days=15)
    c7        = today - timedelta(days=7)

    print("  Computing rolling form from SQLite...")

    conn = get_db()
    df   = pd.read_sql("""
        SELECT batter,game_date,bb_type,events,launch_speed
        FROM batted_balls
        WHERE type='X' AND game_date >= ?
    """, conn, params=(f"{season}-03-20",))
    conn.close()

    if df.empty:
        return pd.DataFrame()

    df["game_date"]    = pd.to_datetime(df["game_date"])
    df["launch_speed"] = pd.to_numeric(df["launch_speed"],errors="coerce")
    has_ls = df["launch_speed"].notna().sum() > 100

    results = []
    for bid, bdf in df.groupby("batter"):
        n = len(bdf)
        if n < 10:
            continue
        s_hr = (bdf["events"]=="home_run").sum()/n*100
        s_hh = ((bdf["launch_speed"]>=95).sum()/n*100
                if has_ls else None)

        r15   = bdf[bdf["game_date"]>=c15]
        n15   = len(r15)
        d15   = (round((r15["events"]=="home_run").sum()/n15*100-s_hr,2)
                 if n15>=10 else 0.0)

        r7    = bdf[bdf["game_date"]>=c7]
        n7    = len(r7)
        d7    = (round((r7["launch_speed"]>=95).sum()/n7*100-s_hh,2)
                 if has_ls and n7>=6 and s_hh else 0.0)

        fs   = max(-10.0, min(10.0,
               (d15*0.60)+((d7/3)*0.40)))
        sign = "+" if fs>=0 else ""
        results.append({
            "mlbam_id":       bid,
            "form_delta_15d": d15,
            "form_delta_7d":  d7,
            "form_score":     round(fs,2),
            "form_pct":       f"{sign}{fs:.1f}%",
        })

    result_df = pd.DataFrame(results)
    if not result_df.empty:
        print(f"  OK  Rolling form for {len(result_df)} batters")
        batter_csv = f"data/batters_{season}.csv"
        name_map   = {}
        if Path(batter_csv).exists():
            bdf2 = pd.read_csv(batter_csv)
            if "mlbam_id" in bdf2.columns and "name" in bdf2.columns:
                name_map = dict(zip(
                    bdf2["mlbam_id"].astype(str), bdf2["name"]))
        def gn(mid):
            return name_map.get(str(int(mid)),f"ID {int(mid)}")
        print("  HOTTEST:")
        for _,r in result_df.nlargest(5,"form_score").iterrows():
            print(f"    {gn(r['mlbam_id']):<24} {r['form_pct']:>7}")
        print("  COLDEST:")
        for _,r in result_df.nsmallest(5,"form_score").iterrows():
            print(f"    {gn(r['mlbam_id']):<24} {r['form_pct']:>7}")
    return result_df


# ==========================================================================
# STEP 5 — BULLPEN
# ==========================================================================

def pull_bullpen_stats(season=SEASON, save=True):
    import pandas as pd
    import requests
    from pybaseball import statcast_pitcher_exitvelo_barrels, cache
    cache.enable()

    print("\n" + "-"*50)
    print("STEP 5 — Bullpen vulnerability + fatigue")
    print("-"*50)

    cache_file = f"data/bullpen_{season}.json"
    if cache_fresh(cache_file, 6):
        with open(cache_file) as f:
            data = json.load(f)
        print(f"  Loaded from cache ({len(data)} teams)")
        return data

    # 24h cache on pitcher leaderboard
    pev_cache = f"data/cache_pitcher_ev_{season}.json"
    if cache_fresh(pev_cache, 24):
        print("  Pitcher EV: 24h cache")
        df = pd.read_json(pev_cache)
    else:
        print("  Pulling pitcher Statcast data...")
        df = statcast_pitcher_exitvelo_barrels(season, minBBE=1)
        df.to_json(pev_cache, orient="records")
        print(f"  OK  {len(df)} pitchers — cached")

    name_col = next((c for c in df.columns
                     if "last_name" in c.lower()), None)
    if name_col:
        df["name"] = df[name_col].apply(
            lambda x: " ".join(str(x).split(", ")[::-1])
            if ", " in str(x) else str(x))

    relievers = df[df["attempts"] < 40].copy()
    print(f"  {len(relievers)} relievers")

    if relievers.empty:
        return {}

    teams = []
    for pid in relievers["player_id"]:
        try:
            r = requests.get(
                f"https://statsapi.mlb.com/api/v1/people/{pid}"
                "?hydrate=currentTeam", timeout=8)
            r.raise_for_status()
            person  = r.json().get("people",[{}])[0]
            team_id = person.get("currentTeam",{}).get("id")
            teams.append(TEAM_ID_MAP.get(team_id,"???"))
        except Exception:
            teams.append("???")
        time.sleep(0.03)

    relievers        = relievers.copy()
    relievers["team"] = teams
    relievers         = relievers[relievers["team"] != "???"]

    fatigue_map = _pull_bullpen_fatigue(relievers)
    bullpen_map = {}

    for team, grp in relievers.groupby("team"):
        import numpy as np
        ba = pd.to_numeric(grp["brl_percent"],errors="coerce").mean()
        ha = pd.to_numeric(grp["ev95percent"],errors="coerce").mean()
        ea = pd.to_numeric(grp["avg_hit_speed"],errors="coerce").mean()
        bs = round(min(10.0,
            min(4.0,max(0,(ba-4)/8*4)) +
            min(3.0,max(0,(ha-30)/20*3)) +
            min(3.0,max(0,(ea-86)/6*3))),2)
        fb = fatigue_map.get(team,0.0)
        bullpen_map[team] = {
            "bullpen_barrel_pct": round(float(ba or 0),1),
            "bullpen_hh_pct":     round(float(ha or 0),1),
            "bullpen_ev_avg":     round(float(ea or 0),1),
            "bullpen_base_score": bs,
            "bullpen_fatigue":    round(fb,2),
            "bullpen_score":      round(min(10.0,bs+fb),2),
            "reliever_count":     len(grp),
        }

    if save:
        Path("data").mkdir(exist_ok=True)
        with open(cache_file,"w") as f:
            json.dump(bullpen_map, f, indent=2)

    print(f"  OK  {len(bullpen_map)} teams")
    return bullpen_map


def _pull_bullpen_fatigue(relievers_df):
    import requests

    today       = date.today()
    fatigue_map = {}

    for team in relievers_df["team"].unique():
        pids          = relievers_df[
            relievers_df["team"]==team
        ]["player_id"].tolist()
        total_f = 0.0
        total_w = 0.0

        for pid in pids[:15]:
            try:
                r = requests.get(
                    f"https://statsapi.mlb.com/api/v1/people/{pid}"
                    f"/stats?stats=gameLog&group=pitching"
                    f"&season={today.year}&sportId=1",
                    timeout=8)
                r.raise_for_status()
                splits = (r.json().get("stats",[{}])[0]
                           .get("splits",[]))
            except Exception:
                continue

            recent_ip = 0.0
            late_inn  = False
            days_list = []

            for split in splits[-10:]:
                try:
                    gd    = datetime.strptime(
                        split["date"],"%Y-%m-%d").date()
                    delta = (today-gd).days
                    if delta > 3:
                        continue
                    ip_str = str(split.get("stat",{}).get(
                        "inningsPitched","0"))
                    ip_p   = ip_str.split(".")
                    ip     = float(ip_p[0])+(
                        float(ip_p[1])/3 if len(ip_p)>1 else 0)
                    recent_ip += ip
                    days_list.append(delta)
                    inn = split.get("stat",{}).get("inning",0)
                    if inn and int(inn) >= 7:
                        late_inn = True
                except Exception:
                    continue

            if not days_list:
                continue
            rec = {0:1.0,1:0.9,2:0.5}.get(min(days_list),0.2)
            rf  = rec * min(1.0, recent_ip/2.0)
            w   = 2.0 if late_inn else 1.0
            total_f += rf * w
            total_w += w
            time.sleep(0.03)

        fatigue_map[team] = (round(min(2.0,(total_f/total_w)*3),2)
                             if total_w > 0 else 0.0)
    return fatigue_map


# ==========================================================================
# STEP 6 — STARTER PRONENESS
# ==========================================================================

def pull_starter_hr_proneness(games, season=SEASON, save=True):
    import pandas as pd
    import requests
    from pybaseball import (
        statcast_pitcher_exitvelo_barrels,
        statcast_pitcher,
        cache,
    )
    cache.enable()

    print("\n" + "-"*50)
    print("STEP 6 — Starter HR proneness")
    print("-"*50)

    cache_file = f"data/starter_proneness_{season}.json"
    if cache_fresh(cache_file, 6):
        with open(cache_file) as f:
            data = json.load(f)
        print(f"  Loaded from cache ({len(data)} starters)")
        return data

    # 24h cache
    pev_cache = f"data/cache_pitcher_ev_{season}.json"
    if cache_fresh(pev_cache, 24):
        df = pd.read_json(pev_cache)
    else:
        df = statcast_pitcher_exitvelo_barrels(season, minBBE=1)
        df.to_json(pev_cache, orient="records")

    name_col = next((c for c in df.columns
                     if "last_name" in c.lower()), None)
    if name_col:
        df["name"] = df[name_col].apply(
            lambda x: " ".join(str(x).split(", ")[::-1])
            if ", " in str(x) else str(x))

    starters = df[df["attempts"] >= 40].copy()

    today_pids = set()
    for g in games:
        if g.get("home_pitcher_id"):
            today_pids.add(int(g["home_pitcher_id"]))
        if g.get("away_pitcher_id"):
            today_pids.add(int(g["away_pitcher_id"]))

    teams = []
    for pid in starters["player_id"]:
        try:
            r = requests.get(
                f"https://statsapi.mlb.com/api/v1/people/{pid}"
                "?hydrate=currentTeam", timeout=8)
            r.raise_for_status()
            person  = r.json().get("people",[{}])[0]
            team_id = person.get("currentTeam",{}).get("id")
            teams.append(TEAM_ID_MAP.get(team_id,"???"))
        except Exception:
            teams.append("???")
        time.sleep(0.04)

    starters        = starters.copy()
    starters["team"] = teams
    starters         = starters[starters["team"] != "???"]

    def fip(pid, stat_type="season"):
        try:
            stat_param = ("season" if stat_type=="season"
                          else "gameLog")
            r = requests.get(
                f"https://statsapi.mlb.com/api/v1/people/{pid}"
                f"/stats?stats={stat_param}&group=pitching"
                f"&season={season}&sportId=1", timeout=8)
            r.raise_for_status()
            splits = (r.json().get("stats",[{}])[0]
                      .get("splits",[]))
            if not splits:
                return None
            if stat_type == "gameLog":
                starts = []
                for sp in reversed(splits):
                    s      = sp.get("stat",{})
                    ip_str = str(s.get("inningsPitched","0"))
                    ip_p   = ip_str.split(".")
                    ip     = float(ip_p[0])+(
                        float(ip_p[1])/3 if len(ip_p)>1 else 0)
                    if ip >= 3:
                        starts.append(s)
                        if len(starts) >= 3:
                            break
                if not starts:
                    return None
                agg = {
                    "homeRuns":    sum(float(s.get("homeRuns",0))    for s in starts),
                    "baseOnBalls": sum(float(s.get("baseOnBalls",0)) for s in starts),
                    "hitByPitch":  sum(float(s.get("hitByPitch",0))  for s in starts),
                    "strikeOuts":  sum(float(s.get("strikeOuts",0))  for s in starts),
                }
                ip_tot = 0.0
                for s in starts:
                    ip_str = str(s.get("inningsPitched","0"))
                    ip_p   = ip_str.split(".")
                    ip_tot += float(ip_p[0])+(
                        float(ip_p[1])/3 if len(ip_p)>1 else 0)
                s = agg
                ip = ip_tot
            else:
                s  = splits[0].get("stat",{})
                ip_str = str(s.get("inningsPitched","0"))
                ip_p   = ip_str.split(".")
                ip = float(ip_p[0])+(
                    float(ip_p[1])/3 if len(ip_p)>1 else 0)
            if ip < 1:
                return None
            hr  = float(s.get("homeRuns",    0))
            bb  = float(s.get("baseOnBalls", 0))
            hbp = float(s.get("hitByPitch",  0))
            k   = float(s.get("strikeOuts",  0))
            return round(((13*hr+3*(bb+hbp)-2*k)/ip)+3.10, 2)
        except Exception:
            return None

    def recency(pid):
        season_start = f"{season}-03-20"
        today_str    = date.today().strftime("%Y-%m-%d")
        try:
            pf = statcast_pitcher(season_start, today_str,
                                   player_id=pid)
        except Exception:
            return {}
        if pf.empty or len(pf) < 10:
            return {}
        pf["game_date"] = pd.to_datetime(pf["game_date"],errors="coerce")
        cnts = (pf.groupby("game_date").size()
                  .reset_index(name="pitches"))
        sdates = (cnts[cnts["pitches"]>=15]["game_date"]
                  .sort_values(ascending=False)
                  .head(3).tolist())
        if not sdates:
            return {}
        recent = pf[pf["game_date"].isin(sdates)].copy()
        batted = recent[recent["type"]=="X"].copy()
        n      = len(batted)
        if n < 5:
            return {}
        fb_c   = (batted["bb_type"]=="fly_ball").sum()
        hr_c   = (batted["events"]=="home_run").sum()
        hr_fb  = round(hr_c/max(fb_c,1)*100,1)
        hh_r   = brl_r = 0.0
        if "launch_speed" in batted.columns:
            ls    = pd.to_numeric(batted["launch_speed"],errors="coerce")
            hh_r  = round((ls>=95).sum()/max(n,1)*100,1)
            hfb   = ((batted["bb_type"]=="fly_ball")&(ls>=95)).sum()
            brl_r = round(hfb/max(n,1)*100,1)
        vt = 0.0
        if "release_speed" in pf.columns:
            sv = pd.to_numeric(pf["release_speed"],errors="coerce").mean()
            rv = pd.to_numeric(recent["release_speed"],errors="coerce").mean()
            if not pd.isna(sv) and not pd.isna(rv):
                vt = round(float(rv)-float(sv),1)
        return {"hr_fb_recent":hr_fb,"hh_recent":hh_r,
                "brl_recent":brl_r,"velo_trend":vt}

    def hs_splits(pid):
        season_start = f"{season}-03-20"
        today_str    = date.today().strftime("%Y-%m-%d")
        try:
            pf = statcast_pitcher(season_start, today_str,
                                   player_id=pid)
        except Exception:
            return {}
        if pf.empty or "stand" not in pf.columns:
            return {}
        res = {}
        for hand in ["L","R"]:
            grp    = pf[pf["stand"]==hand]
            batted = grp[grp["type"]=="X"].copy()
            n      = len(batted)
            if n < 20:
                res[hand] = {"n_bbe":n,"sufficient":False}
                continue
            gb_r  = round((batted["bb_type"]=="ground_ball").sum()/n*100,1)
            fb_c  = (batted["bb_type"]=="fly_ball").sum()
            hr_c  = (batted["events"]=="home_run").sum()
            hr_fb = round(hr_c/max(fb_c,1)*100,1)
            hh_r  = brl_r = 0.0
            if "launch_speed" in batted.columns:
                ls    = pd.to_numeric(
                    batted["launch_speed"],errors="coerce")
                hh_r  = round((ls>=95).sum()/n*100,1)
                hfb   = ((batted["bb_type"]=="fly_ball")&(ls>=95)).sum()
                brl_r = round(hfb/n*100,1)
            res[hand] = {
                "n_bbe":n,"sufficient":True,
                "gb_rate":gb_r,"hr_fb_rate":hr_fb,
                "hh_rate":hh_r,"brl_rate":brl_r,
            }
        return res

    def fip_splits(pid):
        try:
            r = requests.get(
                f"https://statsapi.mlb.com/api/v1/people/{pid}"
                f"/stats?stats=statSplits&group=pitching"
                f"&season={season}&sportId=1&sitCodes=vl,vr",
                timeout=8)
            r.raise_for_status()
            splits = (r.json().get("stats",[{}])[0]
                      .get("splits",[]))
        except Exception:
            return {}
        res = {}
        for sp in splits:
            code = sp.get("split",{}).get("code","")
            if code not in ("vl","vr"):
                continue
            hand = "L" if code=="vl" else "R"
            s    = sp.get("stat",{})
            try:
                hr  = float(s.get("homeRuns",    0))
                bb  = float(s.get("baseOnBalls", 0))
                hbp = float(s.get("hitByPitch",  0))
                k   = float(s.get("strikeOuts",  0))
                ip_str = str(s.get("inningsPitched","0"))
                ip_p   = ip_str.split(".")
                ip = float(ip_p[0])+(
                    float(ip_p[1])/3 if len(ip_p)>1 else 0)
                if ip >= 1:
                    res[hand] = round(
                        ((13*hr+3*(bb+hbp)-2*k)/ip)+3.10,2)
            except Exception:
                continue
        return res

    def ss(brl,ev50,hr_fb,f,gb):
        return round(min(10.0,
            min(2.5,max(0,(brl-4)/10*2.5)) +
            min(2.5,max(0,(ev50-83)/10*2.5)) +
            min(2.0,max(0,(hr_fb-8)/14*2.0)) +
            min(1.5,max(0,(f-3.20)/2.40*1.5)) +
            min(1.5,max(0,(50-gb)/50*1.5))),2)

    def sr(rec,sfip,rfip):
        if not rec:
            return 5.0
        h  = min(2.5,max(0,(rec.get("hr_fb_recent",12)-8)/14*2.5))
        ft = (min(2.5,max(0,(rfip-sfip+0.5)/2.0*2.5))
              if rfip and sfip else 5.0)
        hh = min(2.0,max(0,(rec.get("hh_recent",35)-30)/20*2.0))
        bl = min(1.5,max(0,(rec.get("brl_recent",7)-4)/10*1.5))
        vl = min(1.5,max(0,(-rec.get("velo_trend",0)+1.5)/3.0*1.5))
        return round(min(10.0,h+ft+hh+bl+vl),2)

    def shs(split,fip_h,overall,n_bbe):
        if not split or not split.get("sufficient",False):
            return overall
        sc = round(min(10.0,
            min(3.0,max(0,(split["brl_rate"]-4)/10*3.0)) +
            min(2.5,max(0,(split["hh_rate"]-30)/20*2.5)) +
            min(2.5,max(0,(split["hr_fb_rate"]-8)/14*2.5)) +
            min(1.5,max(0,(50-split["gb_rate"])/50*1.5)) +
            (min(0.5,max(0,(fip_h-3.20)/2.40*0.5))
             if fip_h else 0.0)),2)
        n = split.get("n_bbe",0)
        w = 1.0 if n>=40 else ((n-20)/20 if n>=20 else 0.0)
        return round(sc*w + overall*(1-w),2)

    proneness_map = {}
    print(f"  Scoring {len(starters)} starters...")

    for _,row in starters.iterrows():
        pid  = int(row["player_id"])
        name = str(row.get("name","Unknown"))
        team = row["team"]

        brl  = float(row.get("brl_percent") or 0)
        ev50 = float(row.get("ev50")        or 0)
        sf   = fip(pid,"season") or 4.20
        time.sleep(0.04)

        ac = f"data/arsenal_{pid}_{season}.json"
        hr_fb_r = 10.0
        gb_r    = 45.0
        if Path(ac).exists():
            try:
                with open(ac) as f2:
                    acd = json.load(f2)
                gb_r    = acd.get("gb_rate",45.0)
                hr_fb_r = round(min(25.0,
                    acd.get("fb_rate",35.0)*0.35),1)
            except Exception:
                pass

        s_score = ss(brl,ev50,hr_fb_r,sf,gb_r)

        if pid in today_pids:
            rec  = recency(pid)
            rfip = fip(pid,"gameLog")
            hss  = hs_splits(pid)
            fps  = fip_splits(pid)
            time.sleep(0.04)
        else:
            rec,rfip,hss,fps = {},None,{},{}

        r_score  = sr(rec,sf,rfip)
        sp_score = round((s_score*0.65)+(r_score*0.35),2)
        sp_L = shs(hss.get("L",{}),fps.get("L"),sp_score,
                   hss.get("L",{}).get("n_bbe",0))
        sp_R = shs(hss.get("R",{}),fps.get("R"),sp_score,
                   hss.get("R",{}).get("n_bbe",0))

        proneness_map[str(pid)] = {
            "pitcher_id":    pid,
            "pitcher_name":  name,
            "team":          team,
            "brl_allowed":   round(brl,1),
            "ev50_allowed":  round(ev50,1),
            "hr_fb_rate":    round(hr_fb_r,1),
            "gb_rate":       round(gb_r,1),
            "fip":           round(sf,2),
            "recent_fip":    round(rfip,2) if rfip else None,
            "velo_trend":    rec.get("velo_trend",0),
            "hh_recent":     rec.get("hh_recent",0),
            "season_score":  s_score,
            "recency_score": r_score,
            "sp_score":      sp_score,
            "sp_score_vs_L": sp_L,
            "sp_score_vs_R": sp_R,
            "n_bbe_vs_L":    hss.get("L",{}).get("n_bbe",0),
            "n_bbe_vs_R":    hss.get("R",{}).get("n_bbe",0),
        }

    today_starters = {p:v for p,v in proneness_map.items()
                      if int(p) in today_pids}
    print(f"\n  TODAY'S STARTER PRONENESS:")
    print(f"  {'Name':<24} {'SP':>4} {'vsL':>5} "
          f"{'vsR':>5} {'FIP':>5} {'rFIP':>5}")
    print("  "+"-"*52)
    for p,s in sorted(today_starters.items(),
                       key=lambda x: x[1]["sp_score"],
                       reverse=True):
        rf = (f"{s['recent_fip']:.2f}"
              if s.get("recent_fip") else "  N/A")
        print(f"  {s['pitcher_name']:<24} "
              f"{s['sp_score']:>4.1f} "
              f"{s['sp_score_vs_L']:>5.1f} "
              f"{s['sp_score_vs_R']:>5.1f} "
              f"{s['fip']:>5.2f} {rf:>5}")

    if save:
        Path("data").mkdir(exist_ok=True)
        with open(cache_file,"w") as f:
            json.dump(proneness_map, f, indent=2)
    print(f"\n  OK  {len(proneness_map)} starters")
    return proneness_map


# ==========================================================================
# STEP 7 — ASYNC WEATHER
# ==========================================================================

async def _fetch_one_park(session, home, game_time):
    import aiohttp

    if home in DOME_PARKS:
        return home, {
            "team":home,"temp_f":72.0,"wind_speed_mph":0.0,
            "wind_deg":0,"condition":"dome/retractable","is_dome":True,
        }
    if home not in PARK_COORDS:
        return home, None

    lat,lon = PARK_COORDS[home]
    url = (
        "https://api.tomorrow.io/v4/weather/forecast"
        f"?location={lat},{lon}"
        f"&apikey={WEATHER_API_KEY}"
        "&timesteps=1h&units=imperial"
    )
    try:
        async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return home, None
            data   = await resp.json()
            hourly = data.get("timelines",{}).get("hourly",[])
    except Exception:
        return home, None

    if not hourly:
        return home, None

    if game_time:
        try:
            target = datetime.fromisoformat(
                game_time.replace("Z","+00:00")
            ).replace(tzinfo=None)
            entry = min(hourly, key=lambda x: abs(
                datetime.fromisoformat(
                    x["time"].replace("Z","")) - target))
        except Exception:
            entry = hourly[0]
    else:
        entry = hourly[0]

    v        = entry.get("values",{})
    wind_mph = round(float(v.get("windSpeed",    5.0)),1)
    wind_deg = round(float(v.get("windDirection",180)),0)
    temp_f   = round(float(v.get("temperature",  72.0)),1)
    gust_mph = round(float(v.get("windGust",wind_mph)),1)
    code     = v.get("weatherCode",1000)
    cond     = TOMORROW_CODES.get(code,f"code {code}")

    return home, {
        "team":home,"temp_f":temp_f,
        "wind_speed_mph":wind_mph,"wind_deg":int(wind_deg),
        "wind_gust_mph":gust_mph,
        "condition":cond,"is_dome":False,
    }


def pull_weather_all_games(games, save=True):
    import aiohttp
    import requests

    print("\n" + "-"*50)
    print("STEP 7 — Weather (async)")
    print("-"*50)

    today      = date.today().strftime("%Y-%m-%d")
    cache_file = f"data/weather_{today}.json"

    if cache_fresh(cache_file, 2):
        with open(cache_file) as f:
            data = json.load(f)
        print(f"  Using cached weather ({len(data)} parks)")
        return data

    if not WEATHER_API_KEY:
        print("  No WEATHER_API_KEY — neutral defaults")
        return {}

    try:
        test = requests.get(
            "https://api.tomorrow.io/v4/weather/realtime"
            "?location=40.8296,-73.9262"
            f"&apikey={WEATHER_API_KEY}&units=imperial",
            timeout=10)
        if test.status_code == 401:
            print("  ERROR: Invalid API key")
            return {}
        if test.status_code == 429:
            print("  Rate limit — using cache")
            if Path(cache_file).exists():
                with open(cache_file) as f:
                    return json.load(f)
            return {}
        if test.status_code >= 500:
            print(f"  Server error ({test.status_code}) — using cache")
            if Path(cache_file).exists():
                with open(cache_file) as f:
                    return json.load(f)
            return {}
        test.raise_for_status()
        print("  OK  Tomorrow.io key valid")
    except Exception as e:
        print(f"  ERROR: {e}")
        return {}

    parks = {}
    for g in games:
        home = g["home_team"]
        if home not in parks:
            parks[home] = g["game_time_utc"]

    async def fetch_all():
        async with aiohttp.ClientSession() as session:
            tasks = [
                _fetch_one_park(session, home, gt)
                for home, gt in parks.items()
            ]
            return await asyncio.gather(*tasks)

    try:
        results = asyncio.run(fetch_all())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        results = loop.run_until_complete(fetch_all())

    weather_data = {}
    for home, data in results:
        if data:
            weather_data[home] = data
            if data.get("is_dome"):
                print(f"  {home}: dome")
            else:
                dirs = ["N","NE","E","SE","S","SW","W","NW","N"]
                card = dirs[round(int(data["wind_deg"])/45)%8]
                print(f"  {home}: {data['temp_f']}F  "
                      f"{data['wind_speed_mph']}mph {card}  "
                      f"{data['condition']}")

    if save and weather_data:
        Path("data").mkdir(exist_ok=True)
        with open(cache_file,"w") as f:
            json.dump(weather_data, f, indent=2)
        print(f"\n  Saved -> {cache_file}")

    print(f"\n  OK  Weather for {len(weather_data)} parks")
    return weather_data


# ==========================================================================
# STEP 8 — ODDS
# ==========================================================================

def pull_hr_prop_odds(books="draftkings,fanduel", save=True):
    import requests

    print("\n" + "-"*50)
    print("STEP 8 — Sportsbook odds")
    print("-"*50)

    if not ODDS_API_KEY:
        print("  Skipped — add ODDS_API_KEY to .env")
        return {}

    try:
        r = requests.get(
            "https://api.the-odds-api.com/v4/sports/"
            f"baseball_mlb/events?apiKey={ODDS_API_KEY}",
            timeout=10)
        r.raise_for_status()
        events = r.json()
    except Exception as e:
        print(f"  ERROR: {e}")
        return {}

    today        = date.today().strftime("%Y-%m-%d")
    today_events = [e for e in events
                    if e.get("commence_time","").startswith(today)]
    all_odds     = {}

    for event in today_events:
        eid  = event["id"]
        home = event.get("home_team","")
        away = event.get("away_team","")
        try:
            r = requests.get(
                f"https://api.the-odds-api.com/v4/sports/"
                f"baseball_mlb/events/{eid}/odds"
                f"?apiKey={ODDS_API_KEY}"
                f"&markets=batter_home_runs"
                f"&bookmakers={books}"
                f"&oddsFormat=american",
                timeout=10)
            r.raise_for_status()
            for book in r.json().get("bookmakers",[]):
                for market in book.get("markets",[]):
                    if market["key"] != "batter_home_runs":
                        continue
                    for o in market.get("outcomes",[]):
                        player = o["name"]
                        ov     = o["price"]
                        impl   = (100/(ov+100) if ov>0
                                  else abs(ov)/(abs(ov)+100))
                        if (player not in all_odds or
                                ov > all_odds[player]["best_odds"]):
                            all_odds[player] = {
                                "best_odds":    ov,
                                "best_book":    book["key"],
                                "implied_prob": round(impl,4),
                                "game":         f"{away}@{home}",
                            }
        except Exception:
            pass
        time.sleep(0.1)

    if all_odds:
        print(f"  OK  {len(all_odds)} HR prop lines")
    if save and all_odds:
        Path("data").mkdir(exist_ok=True)
        with open(f"data/odds_{today}.json","w") as f:
            json.dump(all_odds, f, indent=2)
    return all_odds


# ==========================================================================
# MASTER RUNNER
# ==========================================================================

def pull_all(skip_arsenal=False, skip_odds=False,
             skip_matchup=False):
    today = date.today().strftime("%Y-%m-%d")
    Path("data").mkdir(exist_ok=True)

    print("\n" + "="*55)
    print("HR PROP MODEL v4 — OPTIMIZED PIPELINE")
    print(f"Date: {today}")
    print("="*55)

    verify_imports()

    games = pull_mlb_schedule(save=True)
    if not games:
        print("\n  No games today.\n")
        return {}

    games, pitcher_hands, batter_info = pull_all_player_info(games)
    batters_df = pull_batter_statcast(
        season=SEASON, save=True, batter_info=batter_info)
    bullpen    = pull_bullpen_stats(season=SEASON, save=True)
    starters   = pull_starter_hr_proneness(games, season=SEASON)
    weather    = pull_weather_all_games(games, save=True)

    matchup_count = 0
    if not skip_matchup:
        mf = f"data/matchup_cache_{today}.json"
        if Path(mf).exists():
            with open(mf) as f:
                mc = json.load(f)
            matchup_count = len(mc)
            print(f"\n  Matchup cache: {matchup_count} (cached)")
        else:
            print("\n  Building matchup cache...")
            try:
                from cache_matchups import build_matchup_cache
                mc = build_matchup_cache(games, today, season=SEASON)
                matchup_count = len(mc)
            except Exception as e:
                print(f"  Matchup cache failed: {e}")
    else:
        print("\n  Skipping matchup (--no-matchup)")

    odds = {}
    if not skip_odds and ODDS_API_KEY:
        odds = pull_hr_prop_odds(save=True)

    batter_count = len(batters_df) if not batters_df.empty else 0
    ev50_ok = ("ev50" in batters_df.columns and
                batters_df["ev50"].notna().sum() > 10
                if not batters_df.empty else False)

    print("\n" + "="*55)
    print("PIPELINE COMPLETE")
    print("="*55)
    print(f"  Games:         {len(games)}")
    print(f"  Batters:       {batter_count}")
    print(f"  Bullpen teams: {len(bullpen)}")
    print(f"  Weather parks: {len(weather)}")
    print(f"  Matchups:      {matchup_count}")
    print(f"  Odds lines:    {len(odds)}")
    print()
    print(f"  EV50:          {'YES' if ev50_ok else 'NO'}")
    print(f"  Matchup data:  {'YES' if matchup_count>0 else 'NO'}")
    print(f"  Weather:       {'YES' if weather else 'NO'}")
    print()
    print("  NEXT: python hr_prop_model.py --no-arsenal")
    print("="*55+"\n")

    return {
        "games":games,"batters":batters_df,
        "bullpen":bullpen,"starters":starters,
        "weather":weather,"odds":odds,
        "batter_info":batter_info,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-arsenal",  action="store_true")
    ap.add_argument("--no-odds",     action="store_true")
    ap.add_argument("--no-matchup",  action="store_true")
    ap.add_argument("--season",      type=int, default=SEASON)
    args = ap.parse_args()

    pull_all(
        skip_arsenal=args.no_arsenal,
        skip_odds=args.no_odds,
        skip_matchup=args.no_matchup,
    )
