"""
HR Prop Model v4 — Daily Scorer + Excel Output
================================================
Scores MLB batters for HR prop betting.
Outputs formatted Excel file with:
  - Color-coded picks by verdict bracket
  - Wind diagram for each ballpark
  - Best matchup of the day callout
  - Result tracking tab (builds over season)

Usage:
    python hr_prop_model.py --no-arsenal     (~30 sec)
    python hr_prop_model.py                  (~10 min first run)
    python hr_prop_model.py --schedule       (daily at 9 AM)
    python hr_prop_model.py --track          (mark yesterday's results)
"""

import os
import sys
import json
import time
import logging
import argparse
from datetime import date, datetime
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

import pandas as pd
import numpy as np

try:
    from pybaseball import statcast_batter, cache as pb_cache
    pb_cache.enable()
except ImportError:
    log.error("pybaseball not installed.")
    sys.exit(1)

from setup_and_pull import (
    pull_mlb_schedule,
    pull_all_player_info,
    pull_batter_statcast,
    pull_bullpen_stats,
    pull_starter_hr_proneness,
    pull_weather_all_games,
    pull_hr_prop_odds,
    PARK_COORDS,
    DOME_PARKS,
)

SEASON          = int(os.getenv("SEASON", 2026))
MIN_SCORE       = float(os.getenv("MIN_SCORE", 55))
TOP_N           = int(os.getenv("TOP_N", 50))
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY","")
ODDS_API_KEY    = os.getenv("ODDS_API_KEY","")


# ==========================================================================
# VERDICT BRACKETS
# ==========================================================================

def get_verdict(score):
    if   score >= 85: return "STRONG"
    elif score >= 70: return "LEAN"
    elif score >= 55: return "MONITOR"
    else:             return "FADE"


# ==========================================================================
# MODEL WEIGHTS (sum = 112)
# ==========================================================================

WEIGHTS = {
    "barrel_pct":   22,
    "ev50":         14,
    "hard_hit_pct": 10,
    "hr_fb_rate":    8,
    "pull_air_rate": 6,
    "rolling_form":  9,
    "weather":      14,
    "park_factor":   7,
    "sp_proneness":  7,
    "bullpen":       8,
    "matchup":       8,
    "platoon":       2,
}
WEIGHT_TOTAL = sum(WEIGHTS.values())  # 112


# ==========================================================================
# PARK FACTORS (handedness split)
# ==========================================================================

PARK_FACTORS_L = {
    "NYY":120,"LAA":118,"LAD":112,"COL":128,"PHI":115,
    "CIN":142,"BOS":104,"DET":108,"CHC":106,"HOU":103,
    "MIN":102,"STL":90, "KC":112, "CLE":97, "WSH":98,
    "PIT":72, "TB":96,  "BAL":118,"ATH":108,"CWS":95,
    "NYM":108,"MIL":96, "TOR":101,"LAA":118,"TEX":108,
    "ATL":97, "SF":88,  "SD":90,  "SEA":88, "MIA":90,
    "AZ":112,
}

PARK_FACTORS_R = {
    "NYY":97, "LAA":98, "LAD":116,"COL":136,"PHI":109,
    "CIN":122,"BOS":114,"DET":110,"CHC":104,"HOU":103,
    "MIN":102,"STL":95, "KC":108, "CLE":101,"WSH":98,
    "PIT":78, "TB":96,  "BAL":116,"ATH":112,"CWS":91,
    "NYM":88, "MIL":96, "TOR":101,"LAA":98, "TEX":108,
    "ATL":97, "SF":96,  "SD":92,  "SEA":94, "MIA":94,
    "AZ":108,
}

PARK_LF_WIND = {
    # Dome/retractable roof — wind irrelevant
    "HOU":999,"MIA":999,"TOR":999,"TB":999,"AZ":999,
    "MIL":999,"TEX":999,"SEA":999,
    # Outdoor parks — LF direction from home plate in degrees
    # Derived from verified CF orientations (Andrew Clem / satellite data)
    # LF is ~90 degrees counterclockwise from CF when facing outfield
    # Key: this is the compass direction TOWARD left field from home plate
    "CHC": 50,   # CF~NE(50) → LF toward NNW; Wrigley wind OUT for RHB = SW winds
    "COL":340,   # CF~NNW(340) → LF toward W; Coors OUT for RHB = W/SW winds
    "NYY":270,   # CF~W → LF toward S; short RF porch helps LHB not RHB
    "BOS":220,   # CF~SW(220) → LF(Green Monster) toward SE; OUT RHB = E/SE winds
    "PHI":250,   # CF~WSW(250) → LF toward SSE(160)
    "PIT":310,   # CF~NW(310) → LF toward SW(220); deep RF suppresses LHB
    "CLE":225,   # CF~SW(225) → LF toward SE(135)
    "DET": 65,   # CF~SSE(155) most misaligned park → LF toward ENE(65)
    "STL":180,   # CF~S(180) → LF toward E(90)
    "WSH":150,   # CF~SSE(150) → LF toward E(60)
    "BAL":140,   # CF~SE(140) → LF toward ENE(50)
    "LAD":210,   # CF~SSW(210) → LF toward SE(120)
    "SF":  90,   # CF~E(90) → LF toward N(0); Oracle faces due east
    "SD": 180,   # CF~S(180) → LF toward E(90)
    "LAA":180,   # CF~S → LF toward E
    "ATH":200,   # CF~SSW → LF toward SE
    "CWS": 50,   # CF~SE(135) → LF toward ENE(45); points SE per article
    "KC": 180,   # CF~S → LF toward E
    "ATL":185,   # CF~S → LF toward E
    "NYM":150,   # CF~SSE → LF toward E
    "MIN":270,   # CF~N(5) → LF toward W(275); Target Field fully open air
    "CIN":120,   # CF~SSW(210) → LF toward SE(120)
}


# ==========================================================================
# WEATHER SCORING
# ==========================================================================

def compute_weather_score(weather, batter_hand, home_team):
    wind_speed = weather.get("wind_speed_mph", 5)
    wind_deg   = weather.get("wind_deg", 180)
    temp_f     = weather.get("temp_f", 72)
    is_dome    = weather.get("is_dome", False)

    dirs = ["N","NE","E","SE","S","SW","W","NW","N"]
    card = dirs[round(int(wind_deg)/45)%8]

    if is_dome or wind_speed == 0:
        return 7.0, "DOME"

    if   temp_f >= 90: ts = 4.0
    elif temp_f >= 80: ts = 3.2
    elif temp_f >= 70: ts = 2.5
    elif temp_f >= 60: ts = 1.5
    elif temp_f >= 50: ts = 0.7
    else:              ts = 0.0

    if   wind_speed >= 20: wb = 6.0
    elif wind_speed >= 15: wb = 4.8
    elif wind_speed >= 10: wb = 3.0
    elif wind_speed >=  5: wb = 1.2
    else:                  wb = 0.0

    lf  = PARK_LF_WIND.get(home_team, 270)
    if lf == 999:
        dm, ws = 0.0, "DOME"
    else:
        tgt = (lf if batter_hand=="R"
               else (lf+180)%360
               if batter_hand=="L"
               else (lf+90)%360)
        diff = min(abs(wind_deg-tgt)%360,
                   360 - abs(wind_deg-tgt)%360)
        if   diff <=  30: dm,ws = 1.00, f"OUT-{int(wind_speed)}"
        elif diff <=  60: dm,ws = 0.65, f"OUT-{int(wind_speed)}"
        elif diff <=  90: dm,ws = 0.20, f"CROSS-{int(wind_speed)}"
        elif diff <= 120: dm,ws =-0.30, f"CROSS-{int(wind_speed)}"
        elif diff <= 150: dm,ws =-0.65, f"IN-{int(wind_speed)}"
        else:             dm,ws =-1.00, f"IN-{int(wind_speed)}"

    total = round(max(2.0, min(14.0, ts + wb*dm)), 1)
    return total, ws


# ==========================================================================
# BATTER SCORER
# ==========================================================================

def score_batter(batter, pitcher_data, weather,
                  home_team, batter_hand="R",
                  matchup_score=4.0, bullpen_data=None):
    comp = {}

    barrel = float(batter.get("barrel_pct") or 0)
    comp["barrel_pct"] = min(WEIGHTS["barrel_pct"],
                              (barrel/20)*WEIGHTS["barrel_pct"])

    ev50 = float(batter.get("ev50") or 0)
    comp["ev50"] = min(WEIGHTS["ev50"],
                        max(0,(ev50-85)/18*WEIGHTS["ev50"]))

    hh = float(batter.get("hard_hit_pct") or 0)
    comp["hard_hit_pct"] = min(WEIGHTS["hard_hit_pct"],
                                (hh/52)*WEIGHTS["hard_hit_pct"])

    hr_fb = float(batter.get("hr_fb_rate") or 0)
    comp["hr_fb_rate"] = min(WEIGHTS["hr_fb_rate"],
                              (hr_fb/28)*WEIGHTS["hr_fb_rate"])

    pull = float(batter.get("pull_air_rate") or 0)
    comp["pull_air_rate"] = min(WEIGHTS["pull_air_rate"],
                                 (pull/20)*WEIGHTS["pull_air_rate"])

    form = float(batter.get("form_score") or 0)
    comp["rolling_form"] = min(WEIGHTS["rolling_form"],
                                max(0,(form+10)/20*WEIGHTS["rolling_form"]))

    w_score, wind_str = compute_weather_score(
        weather, batter_hand, home_team)
    comp["weather"] = w_score

    if batter_hand == "L":
        park_f = PARK_FACTORS_L.get(home_team, 100)
    elif batter_hand == "R":
        park_f = PARK_FACTORS_R.get(home_team, 100)
    else:
        park_f = round((PARK_FACTORS_L.get(home_team,100) +
                         PARK_FACTORS_R.get(home_team,100))/2)
    comp["park_factor"] = min(WEIGHTS["park_factor"],
                               max(0,(park_f-80)/55*WEIGHTS["park_factor"]))

    sp_raw     = pitcher_data.get("sp_score",5.0)
    fatigue_mod= pitcher_data.get("fatigue_modifier",1.0)
    fatigue_adj= (1.0-fatigue_mod)*1.0
    comp["sp_proneness"] = round(
        max(0,min(WEIGHTS["sp_proneness"]+1,
                   (sp_raw/10)*WEIGHTS["sp_proneness"]+fatigue_adj)),1)

    if bullpen_data:
        bp = bullpen_data.get("bullpen_score",5.0)
        comp["bullpen"] = min(WEIGHTS["bullpen"],(bp/10)*WEIGHTS["bullpen"])
    else:
        comp["bullpen"] = WEIGHTS["bullpen"]*0.5

    raw_m = float(matchup_score)
    comp["matchup"] = round(
        max(0,min(WEIGHTS["matchup"],(raw_m/8.0)*WEIGHTS["matchup"])),1)

    pitcher_hand = pitcher_data.get("pitcher_hand","R")
    platoon = (
        (batter_hand=="L" and pitcher_hand=="R") or
        (batter_hand=="R" and pitcher_hand=="L")
    )
    comp["platoon"] = WEIGHTS["platoon"] if platoon else 0

    raw_total  = sum(comp.values())
    normalized = round((raw_total/WEIGHT_TOTAL)*100,1)
    normalized = min(100.0, normalized)

    return {
        "score":         normalized,
        "verdict":       get_verdict(normalized),
        "components":    {k:round(v,1) for k,v in comp.items()},
        "wind_str":      wind_str,
        "park_factor":   park_f,
        "platoon":       platoon,
        "matchup_score": matchup_score,
        "sp_score":      sp_raw,
        "fatigue_mod":   fatigue_mod,
        "bullpen_score": (bullpen_data.get("bullpen_score",5.0)
                          if bullpen_data else 5.0),
    }


# ==========================================================================
# MAIN DAILY RUNNER
# ==========================================================================

def run_daily(season=SEASON, top_n=TOP_N, min_score=MIN_SCORE,
              use_arsenal_matchup=True):
    log.info("="*65)
    log.info(f"HR PROP MODEL v4  |  {date.today()}")
    log.info("="*65)

    games = pull_mlb_schedule(save=True)
    if not games:
        log.error("No games today.")
        return pd.DataFrame()

    games, pitcher_hands, batter_info = pull_all_player_info(games)
    batters_df = pull_batter_statcast(
        season, save=True, batter_info=batter_info)
    if batters_df.empty:
        log.error("No batter data.")
        return pd.DataFrame()

    bullpen_map = pull_bullpen_stats(season, save=True)
    starter_map = pull_starter_hr_proneness(games, season)
    weather_map = pull_weather_all_games(games, save=True)

    today_str    = date.today().strftime("%Y-%m-%d")
    matchup_cache = {}
    mf = f"data/matchup_cache_{today_str}.json"
    if Path(mf).exists():
        with open(mf) as f:
            matchup_cache = json.load(f)
        log.info(f"  Matchup cache: {len(matchup_cache)} matchups")
    else:
        log.info("  No matchup cache — run cache_matchups.py")

    odds_map = {}
    of = f"data/odds_{today_str}.json"
    if Path(of).exists():
        with open(of) as f:
            odds_map = json.load(f)

    all_rows = []

    for game in games:
        home    = game["home_team"]
        away    = game["away_team"]
        weather = weather_map.get(home,{
            "temp_f":72,"wind_speed_mph":5,
            "wind_deg":180,"is_dome":False})

        for bat_team, opp, p_id, p_name, p_hand in [
            (home, away,
             game["away_pitcher_id"], game["away_pitcher"],
             game["away_pitcher_hand"] or "R"),
            (away, home,
             game["home_pitcher_id"], game["home_pitcher"],
             game["home_pitcher_hand"] or "R"),
        ]:
            if use_arsenal_matchup and p_id and p_id in starter_map:
                pitcher_data = starter_map.get(str(p_id),{})
            else:
                pitcher_data = {
                    "pitcher_hand":     p_hand,
                    "gb_rate":          45.0,
                    "pitch_breakdown":  {},
                    "stuff_score":      5.0,
                    "fatigue_modifier": 1.0,
                }
            pitcher_data["pitcher_hand"] = p_hand

            sp_data = starter_map.get(str(p_id),{}) if p_id else {}
            opp_bullpen = bullpen_map.get(opp)

            team_batters = batters_df[batters_df["team"]==bat_team]
            if team_batters.empty:
                continue

            for _, brow in team_batters.iterrows():
                bname     = str(brow.get("name","Unknown"))
                bhand     = str(brow.get("hand","R"))
                batter_id = brow.get("mlbam_id")
                ops_approx = float(brow.get("ops_approx") or 0.750)

                # SP score by handedness
                if bhand == "L":
                    pitcher_data["sp_score"] = sp_data.get(
                        "sp_score_vs_L", sp_data.get("sp_score",5.0))
                elif bhand == "R":
                    pitcher_data["sp_score"] = sp_data.get(
                        "sp_score_vs_R", sp_data.get("sp_score",5.0))
                else:
                    pitcher_data["sp_score"] = sp_data.get("sp_score",5.0)

                # Matchup from cache
                matchup = 4.0
                if matchup_cache and batter_id and p_id:
                    ck = f"{int(float(batter_id))}_{int(p_id)}"
                    cm = matchup_cache.get(ck, {})
                    if cm:
                        matchup = float(cm.get("matchup_score", 4.0))

                result = score_batter(
                    brow, pitcher_data, weather,
                    home, bhand, matchup, opp_bullpen)

                odds_data    = odds_map.get(bname,{})
                best_odds    = odds_data.get("best_odds")
                implied_prob = odds_data.get("implied_prob")
                model_prob   = result["score"]/100
                edge = round(model_prob-implied_prob,4) \
                       if implied_prob else None

                all_rows.append({
                    "name":          bname,
                    "team":          bat_team,
                    "opponent":      opp,
                    "home_park":     home,
                    "pitcher":       p_name,
                    "pitcher_hand":  p_hand,
                    "batter_hand":   bhand,
                    "score":         result["score"],
                    "verdict":       result["verdict"],
                    "barrel_pct":    round(float(brow.get("barrel_pct") or 0),1),
                    "ev50":          round(float(brow.get("ev50") or 0),1),
                    "hard_hit_pct":  round(float(brow.get("hard_hit_pct") or 0),1),
                    "hr_fb_rate":    round(float(brow.get("hr_fb_rate") or 0),1),
                    "pull_air_rate": round(float(brow.get("pull_air_rate") or 0),1),
                    "form_pct":      str(brow.get("form_pct","0%")),
                    "park_factor":   result["park_factor"],
                    "wind":          result["wind_str"],
                    "sp_score":      result["sp_score"],
                    "bullpen_score": result["bullpen_score"],
                    "matchup_score": result["matchup_score"],
                    "platoon":       result["platoon"],
                    "best_odds":     best_odds,
                    "implied_prob":  implied_prob,
                    "edge":          edge,
                    "components":    json.dumps(result["components"]),
                })

    if not all_rows:
        log.error("No results generated.")
        return pd.DataFrame()

    out = (pd.DataFrame(all_rows)
             .sort_values("score", ascending=False)
             .reset_index(drop=True))

    _print_results(out, top_n, min_score, odds_map)

    # Save CSV
    try:
        Path("data").mkdir(exist_ok=True)
        csv_fname = os.path.join(
            "data", f"hr_picks_{today_str}.csv")
        out.to_csv(csv_fname, index=False)
        log.info(f"\n  CSV saved -> {csv_fname}")
    except Exception as e:
        log.warning(f"  CSV save failed: {e}")

    # Push to Google Sheets
    try:
        from push_to_sheets import push_all
        push_all(
            picks_df=out,
            weather_map=weather_map,
            games=games,
        )
    except Exception as e:
        log.warning(f"  Google Sheets push failed: {e}")

    return out


# ==========================================================================
# TERMINAL OUTPUT
# ==========================================================================

def _print_results(df, top_n, min_score, odds_map):
    filtered = df[df["score"] >= min_score].head(top_n)

    log.info(f"\n{'='*75}")
    log.info(f"HR PROP PICKS v4  |  {date.today()}  |  "
             f"{len(filtered)} picks >= {min_score}")
    log.info(f"{'='*75}")

    for verdict, desc in [
        ("STRONG",  "Score 85-100 — High conviction"),
        ("LEAN",    "Score 70-84  — Good value"),
        ("MONITOR", "Score 55-69  — Watch"),
    ]:
        group = filtered[filtered["verdict"]==verdict]
        if group.empty:
            continue
        log.info(f"\n  {verdict}  —  {desc}")
        log.info(
            f"  {'#':>3}  {'Score':>6}  {'Player':<22} "
            f"{'Team':<5} {'Pitcher':<22} "
            f"{'Brl%':>5} {'EV50':>5} {'HH%':>5} "
            f"{'Form':>6} {'Park':>4} {'Wind':>8} "
            f"{'SP':>4} {'BP':>4} {'Odds':>6}")
        log.info("  "+"-"*112)
        for i,(_, r) in enumerate(group.iterrows(),1):
            plat = "*" if r["platoon"] else " "
            odds = (f"{int(r['best_odds']):+d}"
                    if r.get("best_odds") else "  N/A")
            log.info(
                f"  {i:>3}  {r['score']:>6.1f}  "
                f"{r['name']:<22} {r['team']:<5} "
                f"{r['pitcher']:<22} "
                f"{r['barrel_pct']:>5.1f} {r['ev50']:>5.1f} "
                f"{r['hard_hit_pct']:>5.1f} "
                f"{str(r.get('form_pct','0%')):>6} "
                f"{str(int(r['park_factor'])):>4} "
                f"{str(r.get('wind','N/A')):>8} "
                f"{r['sp_score']:>4.1f} "
                f"{r['bullpen_score']:>4.1f} "
                f"{odds:>6}{plat}")

    sc = len(filtered[filtered["verdict"]=="STRONG"])
    lc = len(filtered[filtered["verdict"]=="LEAN"])
    mc = len(filtered[filtered["verdict"]=="MONITOR"])
    log.info(f"\n  SUMMARY: STRONG={sc}  LEAN={lc}  MONITOR={mc}")
    log.info(f"\n  COLUMN KEY:")
    log.info(f"    Form=15d rolling form  Park=HR factor  "
             f"Wind=direction+mph")
    log.info(f"    SP=starter proneness(0-10)  "
             f"BP=bullpen vulnerability(0-10)  *=platoon")


# ==========================================================================
# SCHEDULER
# ==========================================================================
# ==========================================================================
# SCHEDULER
# ==========================================================================

def run_scheduled(run_time="09:00"):
    import schedule as sched

    def job():
        log.info("Scheduled run starting...")
        run_daily()

    sched.every().day.at(run_time).do(job)
    log.info(f"Scheduler active — {run_time} daily")
    job()
    while True:
        sched.run_pending()
        time.sleep(60)


# ==========================================================================
# ENTRY POINT
# ==========================================================================

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="HR Prop Model v4")
    ap.add_argument("--no-arsenal",    action="store_true")
    ap.add_argument("--schedule",      action="store_true")
    ap.add_argument("--schedule-time", default="09:00")
    ap.add_argument("--season",        type=int, default=SEASON)
    ap.add_argument("--top",           type=int, default=TOP_N)
    ap.add_argument("--min-score",     type=float, default=MIN_SCORE)
    args = ap.parse_args()

    if args.schedule:
        run_scheduled(args.schedule_time)
    else:
        run_daily(
            season=args.season,
            top_n=args.top,
            min_score=args.min_score,
            use_arsenal_matchup=not args.no_arsenal,
        )
