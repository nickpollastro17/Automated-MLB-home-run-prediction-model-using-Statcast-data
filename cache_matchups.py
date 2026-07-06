"""
HR Prop Model — Overnight Matchup Cache
========================================
Pre-caches batter vs pitcher matchup data for tomorrow's games.
Runs automatically at midnight via scheduler or manually anytime.

What it does:
    1. Pulls tomorrow's schedule + probable pitchers
    2. Projects unconfirmed starters from rotation order
    3. Pulls pitcher arsenal (pitch mix + usage) per starter
    4. Pulls batter pitch arsenal performance per opposing batter
    5. Pulls career batter vs pitcher history (2020+)
    6. Computes and stores matchup scores per batter-pitcher pair

Output:
    data/matchup_cache_YYYYMMDD.json

Usage:
    python cache_matchups.py              # cache for tomorrow
    python cache_matchups.py --today      # cache for today
    python cache_matchups.py --date 2026-05-01  # specific date
"""

import os
import sys
import json
import time
import logging
import argparse
from datetime import date, datetime, timedelta
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

SEASON = int(os.getenv("SEASON", 2026))

try:
    import pandas as pd
    import numpy as np
    from pybaseball import (
        statcast_batter_pitch_arsenal,
        statcast_batter,
        statcast_pitcher,
        cache as pb_cache,
    )
    pb_cache.enable()
except ImportError as e:
    log.error(f"Missing package: {e}")
    log.error("Run: pip install -r requirements.txt")
    sys.exit(1)

import requests

# Team ID map
TEAM_ID_MAP = {
    108:"LAA", 109:"AZ",  110:"BAL", 111:"BOS", 112:"CHC",
    113:"CIN", 114:"CLE", 115:"COL", 116:"DET", 117:"HOU",
    118:"KC",  119:"LAD", 120:"WSH", 121:"NYM", 133:"ATH",
    134:"PIT", 135:"SD",  136:"SEA", 137:"SF",  138:"STL",
    139:"TB",  140:"TEX", 141:"TOR", 142:"MIN", 143:"PHI",
    144:"ATL", 145:"CWS", 146:"MIA", 147:"NYY", 158:"MIL",
}


# ==========================================================================
# STEP 1 — GET SCHEDULE WITH PROJECTED STARTERS
# ==========================================================================

def get_schedule_with_starters(target_date_str):
    """
    Pull schedule for target date. For games without confirmed
    probable pitchers, project next starter from rotation order.

    Returns list of games with pitcher info and confirmed/projected flag.
    """
    log.info(f"Pulling schedule for {target_date_str}...")

    url = (
        "https://statsapi.mlb.com/api/v1/schedule"
        f"?sportId=1&date={target_date_str}"
        "&hydrate=probablePitcher,team"
    )
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
    except Exception as e:
        log.error(f"Schedule fetch failed: {e}")
        return []

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
            hp = home.get("probablePitcher", {})
            ap = away.get("probablePitcher", {})

            # Project starters if not confirmed
            home_pitcher_id   = hp.get("id")
            home_pitcher_name = hp.get("fullName","TBD")
            home_confirmed    = bool(home_pitcher_id)

            away_pitcher_id   = ap.get("id")
            away_pitcher_name = ap.get("fullName","TBD")
            away_confirmed    = bool(away_pitcher_id)

            if not home_confirmed:
                proj = project_starter(
                    home["team"]["id"], target_date_str)
                if proj:
                    home_pitcher_id   = proj["id"]
                    home_pitcher_name = proj["name"] + " [PROJECTED]"

            if not away_confirmed:
                proj = project_starter(
                    away["team"]["id"], target_date_str)
                if proj:
                    away_pitcher_id   = proj["id"]
                    away_pitcher_name = proj["name"] + " [PROJECTED]"

            games.append({
                "game_pk":            g["gamePk"],
                "home_team":          home_abbr,
                "away_team":          away_abbr,
                "home_team_id":       home["team"]["id"],
                "away_team_id":       away["team"]["id"],
                "home_pitcher_id":    home_pitcher_id,
                "home_pitcher_name":  home_pitcher_name,
                "home_confirmed":     home_confirmed,
                "away_pitcher_id":    away_pitcher_id,
                "away_pitcher_name":  away_pitcher_name,
                "away_confirmed":     away_confirmed,
            })

    log.info(f"  {len(games)} games found")
    for g in games:
        h_flag = "✓" if g["home_confirmed"] else "~"
        a_flag = "✓" if g["away_confirmed"] else "~"
        log.info(
            f"  {g['away_team']} @ {g['home_team']}  "
            f"{a_flag}{g['away_pitcher_name']:<25} vs "
            f"{h_flag}{g['home_pitcher_name']}")

    return games


def project_starter(team_id, target_date_str):
    """
    Project next starter for a team by looking at recent
    pitching game logs and identifying rotation order.
    Returns dict with id and name of projected starter.
    """
    try:
        # Get team roster — pitchers only
        r = requests.get(
            f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster"
            "?rosterType=active",
            timeout=10)
        r.raise_for_status()
        roster = r.json().get("roster", [])

        pitchers = [
            p for p in roster
            if p.get("position",{}).get("abbreviation") == "P"
        ]

        if not pitchers:
            return None

        target_dt = datetime.strptime(target_date_str, "%Y-%m-%d").date()
        season    = target_dt.year

        # For each pitcher get their last start date
        starter_last_start = []
        for p in pitchers[:15]:  # Limit to avoid too many API calls
            pid  = p["person"]["id"]
            name = p["person"]["fullName"]
            try:
                r2 = requests.get(
                    f"https://statsapi.mlb.com/api/v1/people/{pid}"
                    f"/stats?stats=gameLog&group=pitching"
                    f"&season={season}&sportId=1",
                    timeout=8)
                r2.raise_for_status()
                splits = (r2.json()
                           .get("stats",[{}])[0]
                           .get("splits",[]))

                # Find last start (IP >= 4)
                last_start = None
                for split in reversed(splits):
                    s      = split.get("stat",{})
                    ip_str = str(s.get("inningsPitched","0"))
                    try:
                        ip_parts = ip_str.split(".")
                        ip = float(ip_parts[0]) + (
                            float(ip_parts[1])/3
                            if len(ip_parts) > 1 else 0)
                        if ip >= 4:
                            last_start = datetime.strptime(
                                split["date"],"%Y-%m-%d").date()
                            break
                    except Exception:
                        continue

                if last_start:
                    days_since = (target_dt - last_start).days
                    starter_last_start.append({
                        "id":         pid,
                        "name":       name,
                        "last_start": last_start,
                        "days_since": days_since,
                    })
                time.sleep(0.05)
            except Exception:
                continue

        if not starter_last_start:
            return None

        # Project: pitcher with days_since closest to 5
        # (normal rotation = every 5 days)
        starter_last_start.sort(
            key=lambda x: abs(x["days_since"] - 5))

        projected = starter_last_start[0]
        log.info(
            f"    Projected: {projected['name']} "
            f"({projected['days_since']} days rest)")
        return {"id": projected["id"], "name": projected["name"]}

    except Exception as e:
        log.warning(f"  Rotation projection failed: {e}")
        return None


# ==========================================================================
# STEP 2 — PULL BATTER PITCH ARSENAL (season performance by pitch type)
# ==========================================================================

def pull_batter_pitch_arsenal(season=SEASON):
    """
    Pull batter performance vs each pitch type for the season.
    Uses statcast_batter_pitch_arsenal() confirmed working.

    Returns dict: mlbam_id -> {pitch_type -> performance_data}

    Key metric: run_value_per_100
        Positive = batter excels vs this pitch type
        Negative = pitcher wins vs this pitch type
    """
    cache_file = f"data/batter_pitch_arsenal_{season}.json"

    # Refresh daily
    if Path(cache_file).exists():
        age = datetime.now() - datetime.fromtimestamp(
            Path(cache_file).stat().st_mtime)
        if age.total_seconds() < 86400:  # 24 hours
            with open(cache_file) as f:
                data = json.load(f)
            log.info(f"  Batter pitch arsenal: loaded from cache "
                     f"({len(data)} batters)")
            return data

    log.info("  Pulling batter pitch arsenal data...")
    try:
        df = statcast_batter_pitch_arsenal(season, minPA=10)
        log.info(f"  OK  {len(df)} rows, "
                 f"{df['pitch_type'].nunique()} pitch types")
    except Exception as e:
        log.error(f"  Failed: {e}")
        return {}

    # Fix name format
    name_col = next((c for c in df.columns
                     if "last_name" in c.lower()), None)
    if name_col:
        df["name"] = df[name_col].apply(
            lambda x: " ".join(str(x).split(", ")[::-1])
            if ", " in str(x) else str(x))

    # Build nested dict: player_id -> pitch_type -> metrics
    arsenal_map = {}
    for _, row in df.iterrows():
        pid = str(int(row["player_id"]))
        pt  = str(row["pitch_type"])

        if pid not in arsenal_map:
            arsenal_map[pid] = {}

        arsenal_map[pid][pt] = {
            "run_value_per_100": float(row.get("run_value_per_100") or 0),
            "whiff_percent":     float(row.get("whiff_percent")     or 0),
            "hard_hit_percent":  float(row.get("hard_hit_percent")  or 0),
            "est_woba":          float(row.get("est_woba")          or 0),
            "pitch_usage":       float(row.get("pitch_usage")       or 0),
            "pa":                int(row.get("pa")                  or 0),
        }

    Path("data").mkdir(exist_ok=True)
    with open(cache_file, "w") as f:
        json.dump(arsenal_map, f, indent=2)
    log.info(f"  Saved -> {cache_file}")

    return arsenal_map


# ==========================================================================
# STEP 3 — PULL PITCHER ARSENAL (pitch mix + usage)
# ==========================================================================

def get_pitcher_arsenal(pitcher_id, season=SEASON):
    """
    Get pitcher's pitch mix from existing arsenal cache or
    pull fresh from statcast_pitcher().

    Returns dict: pitch_type -> {usage_pct, avg_location_x, avg_location_z}
    """
    arsenal_file = f"data/arsenal_{pitcher_id}_{season}.json"

    if Path(arsenal_file).exists():
        with open(arsenal_file) as f:
            data = json.load(f)
        pb = data.get("pitch_breakdown", {})
        if pb:
            return {
                pt: {
                    "usage_pct":  stats.get("usage_pct", 0),
                    "whiff_rate": stats.get("whiff_rate", 0),
                    "hr_rate":    stats.get("hr_rate", 0),
                }
                for pt, stats in pb.items()
            }

    # Pull fresh if no cache
    log.info(f"    Pulling pitcher arsenal for {pitcher_id}...")
    season_start = f"{season}-03-20"
    today_str    = date.today().strftime("%Y-%m-%d")

    try:
        df = statcast_pitcher(
            season_start, today_str,
            player_id=pitcher_id)
    except Exception as e:
        log.warning(f"    Arsenal pull failed: {e}")
        return {}

    if df.empty or "pitch_type" not in df.columns:
        return {}

    arsenal = {}
    for pt, grp in df.groupby("pitch_type"):
        if pd.isna(pt) or pt == "" or len(grp) < 5:
            continue
        usage = round(len(grp) / len(df) * 100, 1)

        # Average location
        loc_x, loc_z = None, None
        if "plate_x" in grp.columns:
            lx = pd.to_numeric(grp["plate_x"], errors="coerce").mean()
            if not pd.isna(lx):
                loc_x = round(float(lx), 2)
        if "plate_z" in grp.columns:
            lz = pd.to_numeric(grp["plate_z"], errors="coerce").mean()
            if not pd.isna(lz):
                loc_z = round(float(lz), 2)

        swings = grp[grp["description"].isin([
            "swinging_strike","swinging_strike_blocked",
            "foul","foul_tip","hit_into_play"])]
        whiffs = grp[grp["description"].isin([
            "swinging_strike","swinging_strike_blocked"])]
        whiff_rate = round(
            len(whiffs)/max(len(swings),1)*100, 1)

        arsenal[pt] = {
            "usage_pct":  usage,
            "whiff_rate": whiff_rate,
            "avg_loc_x":  loc_x,
            "avg_loc_z":  loc_z,
        }

    return arsenal


# ==========================================================================
# STEP 4 — PULL CAREER BATTER VS PITCHER HISTORY (2020+)
# ==========================================================================

def get_career_matchup(batter_id, pitcher_id, season=SEASON):
    """
    Pull career Statcast data for batter vs this specific pitcher
    going back to 2020.

    PA thresholds:
        < 15 PA  -> no career data, use pitch arsenal only
        15-30 PA -> partial weight (linear 20-80%)
        31+ PA   -> full weight

    Cached per pair for 7 days.
    """
    cache_file = f"data/matchup_{batter_id}_{pitcher_id}.json"

    if Path(cache_file).exists():
        age = datetime.now() - datetime.fromtimestamp(
            Path(cache_file).stat().st_mtime)
        if age.total_seconds() < 604800:  # 7 days
            with open(cache_file) as f:
                return json.load(f)

    try:
        df = statcast_batter(
            "2020-03-01",
            date.today().strftime("%Y-%m-%d"),
            player_id=int(batter_id))
    except Exception:
        return {"pa": 0, "has_history": False, "weight": 0.0}

    if df.empty:
        return {"pa": 0, "has_history": False, "weight": 0.0}

    # Filter to this specific pitcher
    if "pitcher" in df.columns:
        df = df[df["pitcher"] == int(pitcher_id)]

    pa = len(df)

    if pa < 15:
        result = {"pa": pa, "has_history": False, "weight": 0.0}
        Path("data").mkdir(exist_ok=True)
        with open(cache_file, "w") as f:
            json.dump(result, f)
        return result

    # PA-based confidence weight
    if pa >= 31:
        weight = 1.0
    else:
        weight = round(0.20 + (pa - 15) / 16 * 0.60, 3)

    # Contact quality
    contact = df[df["type"] == "X"]
    n_contact = max(len(contact), 1)

    # Barrel proxy (hard fly balls)
    brl_pct = 0.0
    if "launch_speed" in contact.columns and "bb_type" in contact.columns:
        ls = pd.to_numeric(contact["launch_speed"], errors="coerce")
        hard_fb = (
            (contact["bb_type"] == "fly_ball") & (ls >= 95)
        ).sum()
        brl_pct = round(hard_fb / n_contact * 100, 1)

    # Hard hit rate
    hh_pct = 0.0
    if "launch_speed" in contact.columns:
        ls     = pd.to_numeric(contact["launch_speed"], errors="coerce")
        hh_pct = round((ls >= 95).sum() / n_contact * 100, 1)

    # HR rate
    hrs    = df[df["events"] == "home_run"]
    hr_pct = round(len(hrs) / pa * 100, 1)

    # Run value proxy from xwOBA
    xwoba = None
    if "estimated_woba_using_speedangle" in contact.columns:
        raw = pd.to_numeric(
            contact["estimated_woba_using_speedangle"],
            errors="coerce").mean()
        if not pd.isna(raw):
            xwoba = round(float(raw), 3)

    result = {
        "pa":          pa,
        "has_history": True,
        "weight":      weight,
        "barrel_pct":  brl_pct,
        "hard_hit_pct":hh_pct,
        "hr_pct":      hr_pct,
        "xwoba":       xwoba,
    }

    Path("data").mkdir(exist_ok=True)
    with open(cache_file, "w") as f:
        json.dump(result, f)

    return result


# ==========================================================================
# STEP 5 — COMPUTE MATCHUP SCORE
# ==========================================================================

def compute_matchup_score(batter_id, pitcher_id,
                           batter_hand, pitcher_hand,
                           batter_arsenal, pitcher_arsenal,
                           ops_approx=0.750):
    """
    Compute matchup score 0-8 for one batter vs one pitcher.

    Components:
        Pitch type compatibility  2.5pts  run_value cross-reference
        Career barrel% vs pitcher 1.5pts  historical contact quality
        Career hard hit% vs ptchr 1.0pt   historical contact quality
        Career HR rate vs pitcher 1.0pt   ultimate outcome signal
        Whiff rate cross-reference 1.0pt  swing-and-miss signal
        OPS+ confidence modifier  applied as weight on career data

    Higher = better matchup for the batter.
    5.0 = perfectly neutral.
    """
    pitch_score   = 0.0
    career_score  = 0.0

    # ── Pitch type compatibility ──────────────────────────────────────
    # Cross-reference pitcher pitch mix against batter performance
    # on each pitch type using run_value_per_100

    batter_pt_data  = batter_arsenal.get(str(batter_id), {})
    weighted_rv     = 0.0
    weighted_whiff  = 0.0
    total_usage     = 0.0

    for pt, p_stats in pitcher_arsenal.items():
        usage = p_stats.get("usage_pct", 0) / 100
        if usage < 0.05:  # Skip pitch types thrown < 5%
            continue

        b_stats = batter_pt_data.get(pt, {})

        # Run value per 100: positive = batter wins, negative = pitcher wins
        # League average is 0, typical range is -3 to +3
        rv = b_stats.get("run_value_per_100", 0.0)

        # Whiff rate: higher = batter struggles
        whiff = b_stats.get("whiff_percent", 25.0)  # 25% = league avg

        weighted_rv    += rv * usage
        weighted_whiff += whiff * usage
        total_usage    += usage

    if total_usage > 0:
        avg_rv    = weighted_rv / total_usage
        avg_whiff = weighted_whiff / total_usage

        # Convert run value to 0-2.5 scale
        # avg_rv range roughly -3 to +3
        # +3 RV/100 = elite matchup, -3 = terrible matchup
        rv_score = min(2.5, max(0,
            (avg_rv + 3) / 6 * 2.5))

        # Whiff cross-reference 0-1.0
        # Low whiff vs pitcher's arsenal = good for batter
        # 10% whiff = great, 40% whiff = terrible
        whiff_score = min(1.0, max(0,
            (40 - avg_whiff) / 30 * 1.0))

        pitch_score = rv_score + whiff_score
    else:
        pitch_score = 1.75  # neutral if no pitch data

    # ── Career history vs this pitcher ───────────────────────────────
    career = get_career_matchup(batter_id, pitcher_id,)
                                
    career_weight = career.get("weight", 0.0)

    if career.get("has_history") and career_weight > 0:
        # Barrel% vs pitcher: league avg ~7%
        brl_score = min(1.5, max(0,
            (career.get("barrel_pct",7) - 4) / 10 * 1.5))

        # Hard hit% vs pitcher: league avg ~35%
        hh_score = min(1.0, max(0,
            (career.get("hard_hit_pct",35) - 25) / 20 * 1.0))

        # HR rate vs pitcher: league avg ~3%
        hr_score = min(1.0, max(0,
            (career.get("hr_pct",3) - 1) / 6 * 1.0))

        # OPS+ confidence: higher quality batter = trust history more
        ops_conf = min(1.2, max(0.7, ops_approx / 0.750))

        raw_career = (brl_score + hh_score + hr_score) * ops_conf

        # Blend with weight based on PA count
        career_score = raw_career * career_weight
    else:
        career_score = 1.75  # neutral

    # ── Combined score ────────────────────────────────────────────────
    # pitch_score max ~3.5, career_score max ~4.5 blended
    # Normalize to 0-8 with 4.0 = neutral
    raw_total = pitch_score + career_score

    # Scale to 0-8 range: raw 0 = terrible, raw 7 = elite
    # raw 3.5 = neutral (4.0 points)
    normalized = min(8.0, max(0.0,
        (raw_total / 7.0) * 8.0))

    return round(normalized, 2)


# ==========================================================================
# STEP 6 — BUILD FULL MATCHUP CACHE FOR ALL GAMES
# ==========================================================================

def build_matchup_cache(games, target_date_str,
                         season=SEASON, save=True):
    """
    Build complete matchup cache for all games on target date.
    Stores per-batter matchup scores keyed by batter_id + pitcher_id.
    """
    log.info("\n" + "="*55)
    log.info("BUILDING MATCHUP CACHE")
    log.info("="*55)

    cache_file = f"data/matchup_cache_{target_date_str}.json"

    if Path(cache_file).exists():
        age = datetime.now() - datetime.fromtimestamp(
            Path(cache_file).stat().st_mtime)
        if age.total_seconds() < 43200:  # 12 hours
            with open(cache_file) as f:
                data = json.load(f)
            log.info(f"Cache exists ({len(data)} matchups) — "
                     f"built {age.total_seconds()/3600:.1f}h ago")
            return data

    # Pull batter pitch arsenal (season-level, cached daily)
    log.info("\nPulling batter pitch arsenal...")
    batter_arsenal = pull_batter_pitch_arsenal(season)

    matchup_cache = {}
    total_matchups = 0

    for game in games:
        home = game["home_team"]
        away = game["away_team"]

        for bat_team, opp_team, p_id, p_name in [
            (home, away,
             game["away_pitcher_id"],
             game["away_pitcher_name"]),
            (away, home,
             game["home_pitcher_id"],
             game["home_pitcher_name"]),
        ]:
            if not p_id:
                continue

            log.info(f"\n  {bat_team} batters vs {p_name} ({p_id})")

            # Get pitcher arsenal
            p_arsenal = get_pitcher_arsenal(p_id, season)
            if not p_arsenal:
                log.warning(f"    No arsenal data for {p_name}")
                continue

            log.info(f"    Pitcher throws: "
                     f"{[(pt, str(s.get('usage_pct','?'))+'%') for pt,s in p_arsenal.items()]}")

            # Get opposing roster
            roster = get_active_roster(
                game["home_team_id"]
                if bat_team == home
                else game["away_team_id"]
            )

            if not roster:
                log.warning(f"    No roster found for {bat_team}")
                continue

            log.info(f"    {len(roster)} batters on roster")

            for batter in roster:
                b_id   = batter["id"]
                b_name = batter["name"]
                b_hand = batter.get("hand","R")

                # Get pitcher hand
                p_hand = get_pitcher_hand(p_id)

                # Compute matchup score
                score = compute_matchup_score(
                    b_id, p_id,
                    b_hand, p_hand,
                    batter_arsenal, p_arsenal,
                    ops_approx=0.750)

                key = f"{b_id}_{p_id}"
                matchup_cache[key] = {
                    "batter_id":    b_id,
                    "batter_name":  b_name,
                    "pitcher_id":   p_id,
                    "pitcher_name": p_name,
                    "bat_team":     bat_team,
                    "matchup_score":score,
                }
                total_matchups += 1

                time.sleep(0.02)

    log.info(f"\n  Total matchups computed: {total_matchups}")

    if save and matchup_cache:
        Path("data").mkdir(exist_ok=True)
        with open(cache_file, "w") as f:
            json.dump(matchup_cache, f, indent=2)
        log.info(f"  Saved -> {cache_file}")

    return matchup_cache


# ==========================================================================
# HELPER FUNCTIONS
# ==========================================================================

def get_active_roster(team_id):
    """Get active 26-man roster for a team."""
    try:
        r = requests.get(
            f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster"
            "?rosterType=active",
            timeout=10)
        r.raise_for_status()
        roster = r.json().get("roster", [])

        batters = []
        for p in roster:
            pos = p.get("position",{}).get("abbreviation","")
            if pos in ("P","TWP"):
                continue  # Skip pure pitchers
            pid  = p["person"]["id"]
            name = p["person"]["fullName"]
            hand = get_batter_hand(pid)
            batters.append({
                "id":   pid,
                "name": name,
                "hand": hand,
            })
            time.sleep(0.03)

        return batters
    except Exception as e:
        log.warning(f"Roster fetch failed for team {team_id}: {e}")
        return []


_hand_cache = {}


def get_batter_hand(mlbam_id):
    key = f"b_{mlbam_id}"
    if key in _hand_cache:
        return _hand_cache[key]
    try:
        r = requests.get(
            f"https://statsapi.mlb.com/api/v1/people/{mlbam_id}",
            timeout=8)
        r.raise_for_status()
        p    = r.json().get("people",[{}])[0]
        hand = p.get("batSide",{}).get("code","R")
    except Exception:
        hand = "R"
    _hand_cache[key] = hand
    time.sleep(0.03)
    return hand


def get_pitcher_hand(pitcher_id):
    key = f"p_{pitcher_id}"
    if key in _hand_cache:
        return _hand_cache[key]
    try:
        r = requests.get(
            f"https://statsapi.mlb.com/api/v1/people/{pitcher_id}",
            timeout=8)
        r.raise_for_status()
        p    = r.json().get("people",[{}])[0]
        hand = p.get("pitchHand",{}).get("code","R")
    except Exception:
        hand = "R"
    _hand_cache[key] = hand
    time.sleep(0.03)
    return hand


# ==========================================================================
# MAIN
# ==========================================================================

def run(target_date_str=None):
    if not target_date_str:
        # Default: tomorrow
        target_date_str = (
            date.today() + timedelta(days=1)
        ).strftime("%Y-%m-%d")

    Path("data").mkdir(exist_ok=True)

    log.info("="*55)
    log.info(f"MATCHUP CACHE BUILD  |  target: {target_date_str}")
    log.info("="*55)

    games = get_schedule_with_starters(target_date_str)
    if not games:
        log.error("No games found — exiting")
        return {}

    cache = build_matchup_cache(games, target_date_str)

    log.info("\n" + "="*55)
    log.info("MATCHUP CACHE COMPLETE")
    log.info(f"  {len(cache)} batter-pitcher matchups stored")
    log.info(f"  File: data/matchup_cache_{target_date_str}.json")
    log.info("="*55)

    return cache


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Build overnight matchup cache")
    ap.add_argument("--today",  action="store_true",
                    help="Cache for today instead of tomorrow")
    ap.add_argument("--date",   type=str, default=None,
                    help="Specific date YYYY-MM-DD")
    ap.add_argument("--season", type=int, default=SEASON)
    args = ap.parse_args()

    if args.date:
        target = args.date
    elif args.today:
        target = date.today().strftime("%Y-%m-%d")
    else:
        target = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")

    run(target)
