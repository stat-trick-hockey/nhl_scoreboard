# fetch_nhl.py
# Fetches NHL schedule/scores, win probability, and standings.
# No API key required.
#
# Endpoints:
#   GET /v1/schedule/now        - today + upcoming games
#                                 homeTeamWinProbability is ON the game object here
#   GET /v1/gamecenter/{id}/landing  - win prob for LIVE games only
#   GET /v1/standings/now       - current standings

import json
import time
import pathlib
import urllib.request
import urllib.error
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "https://api-web.nhle.com/v1"


def get_json(path, retries=3, silent_404=False):
    url = f"{BASE}/{path}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nhl-arcade/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404 and silent_404:
                return {}
            if e.code == 429 and attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    return {}


def map_state(s):
    if s in ("FUT", "PRE"):
        return "scheduled"
    if s in ("LIVE", "CRIT"):
        return "inprogress"
    return "closed"


def team_full_name(t):
    place  = t.get("placeName",  {}).get("default", "")
    common = t.get("commonName", {}).get("default", "")
    return f"{place} {common}".strip() if place else common


def fetch_win_prob_live(game_id, home_abbr, away_abbr):
    """For LIVE games: hits gamecenter landing for homeTeamWinProbability."""
    try:
        data = get_json(f"gamecenter/{game_id}/landing", silent_404=True)
        home_prob = data.get("homeTeamWinProbability")
        if home_prob is None:
            home_prob = data.get("game", {}).get("homeTeamWinProbability")
        if home_prob is None:
            return None
        home_prob = round(float(home_prob), 1)
        away_prob = round(100.0 - home_prob, 1)
        return {home_abbr: home_prob, away_abbr: away_prob}
    except Exception as e:
        print(f"  Win prob fetch failed for {game_id}: {e}")
        return None


def fetch_today_games():
    print("Fetching today's schedule...")
    data = get_json("schedule/now")

    game_week = data.get("gameWeek", [])
    if not game_week:
        print("  No gameWeek data.")
        return {"date": "unknown", "games": []}

    date_str = game_week[0].get("date", "unknown")
    raw = []
    for bucket in game_week:
        raw.extend(bucket.get("games", []))

    print(f"  {date_str}: {len(raw)} game(s) across {len(game_week)} day(s)")

    games_out = []
    live_prob_needed = []  # (index, game_id, home_abbr, away_abbr)

    for g in raw:
        state     = g.get("gameState", "FUT")
        status    = map_state(state)
        home_t    = g.get("homeTeam", {})
        away_t    = g.get("awayTeam", {})
        home_abbr = home_t.get("abbrev", "???")
        away_abbr = away_t.get("abbrev", "???")

        obj = {
            "id":         str(g.get("id", "")),
            "status":     status,
            "start_time": g.get("startTimeUTC", ""),
            "home":       home_abbr,
            "away":       away_abbr,
            "teams": {
                home_abbr: {"name": team_full_name(home_t)},
                away_abbr: {"name": team_full_name(away_t)},
            },
        }

        # Scores
        hs  = home_t.get("score")
        as_ = away_t.get("score")
        if hs is not None and as_ is not None:
            obj["score"] = {home_abbr: hs, away_abbr: as_}

        # Live clock / period
        if status == "inprogress":
            pd = g.get("periodDescriptor", {})
            obj["period"]      = pd.get("number", 1)
            obj["period_type"] = pd.get("periodType", "REG")
            obj["clock"]       = g.get("clock", {}).get("timeRemaining", "")

        # Win probability for SCHEDULED games:
        # homeTeamWinProbability lives directly on the schedule game object (0-100)
        if status == "scheduled":
            home_prob = g.get("homeTeamWinProbability")
            if home_prob is not None:
                home_prob = round(float(home_prob), 1)
                away_prob = round(100.0 - home_prob, 1)
                obj["win_probability"] = {home_abbr: home_prob, away_abbr: away_prob}

        games_out.append(obj)

        # For LIVE games, fetch win prob from gamecenter endpoint
        if status == "inprogress":
            live_prob_needed.append((len(games_out) - 1, obj["id"], home_abbr, away_abbr))

    # Fetch live win probabilities in parallel
    if live_prob_needed:
        print(f"  Fetching live win probability for {len(live_prob_needed)} game(s)...")
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(fetch_win_prob_live, gid, ha, aa): idx
                for idx, gid, ha, aa in live_prob_needed
            }
            for future in as_completed(futures):
                idx = futures[future]
                prob = future.result()
                if prob:
                    games_out[idx]["win_probability"] = prob

    return {"date": date_str, "games": games_out}


CONF_LABEL = {
    "Eastern": "EASTERN CONFERENCE",
    "Western": "WESTERN CONFERENCE",
}

def fetch_standings():
    print("Fetching standings...")
    data = get_json("standings/now")
    raw  = data.get("standings", [])
    print(f"  {len(raw)} teams")

    out = []
    for t in raw:
        conf_raw = t.get("conferenceName", "")
        div_raw  = t.get("divisionName", "")
        conf_lbl = CONF_LABEL.get(conf_raw, conf_raw.upper() + " CONFERENCE")

        wins      = t.get("wins", 0)
        losses    = t.get("losses", 0)
        ot_losses = t.get("otLosses", 0)
        gp        = t.get("gamesPlayed", 1) or 1
        point_pct = float(t.get("pointPctg", wins / gp))

        div_seq  = t.get("divisionSequence",  99)
        conf_seq = t.get("conferenceSequence", 99)
        wc_seq   = t.get("wildCardSequence",   0)

        rank = {"division": div_seq, "conference": conf_seq}
        if wc_seq and wc_seq > 0:
            rank["wildcard"] = wc_seq

        out.append({
            "rank":           rank,
            "team":           {"name": team_full_name(t) or t.get("teamName", {}).get("default", "")},
            "wins":           wins,
            "losses":         losses + ot_losses,
            "win_percentage": round(point_pct, 3),
            "conference":     conf_lbl,
            "division":       div_raw,
        })

    return out


def main():
    games_data     = fetch_today_games()
    standings_data = fetch_standings()

    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "games":      games_data,
        "standings":  standings_data,
    }

    out_path = pathlib.Path(__file__).parent.parent / "data.json"
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"\nOK data.json written ({len(games_data['games'])} games, {len(standings_data)} teams)")


if __name__ == "__main__":
    main()
