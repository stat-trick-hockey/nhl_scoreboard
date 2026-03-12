"""
fetch_nhl.py
============
Fetches today's NHL schedule/scores and current standings from the
free, no-key-required NHL Web API (api-web.nhle.com) and writes
data.json in the shape expected by nhl-arcade/index.html.

Endpoints used:
  GET https://api-web.nhle.com/v1/schedule/now    — today's games
  GET https://api-web.nhle.com/v1/standings/now   — current standings

No API key required.
"""

import json
import time
import pathlib
import urllib.request
import urllib.error
from datetime import datetime, timezone

BASE = "https://api-web.nhle.com/v1"


# ── HTTP helper ───────────────────────────────────────────────────────────────

def get_json(path, retries=3):
    url = f"{BASE}/{path}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nhl-arcade/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    return {}


# ── game-state mapping ────────────────────────────────────────────────────────
# NHL API gameState:  FUT/PRE = scheduled  LIVE/CRIT = inprogress  everything else = closed

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


# ── fetch games ───────────────────────────────────────────────────────────────

def fetch_today_games():
    print("Fetching today's schedule…")
    data = get_json("schedule/now")

    game_week = data.get("gameWeek", [])
    if not game_week:
        print("  No gameWeek data.")
        return {"date": "unknown", "games": []}

    # Collect games from ALL buckets in the response:
    #   bucket[0] = today  (live / finished games)
    #   bucket[1] = tomorrow  (upcoming — only present when today has games)
    # This ensures the "upcoming" section is always populated.
    date_str = game_week[0].get("date", "unknown")
    raw = []
    for bucket in game_week:
        raw.extend(bucket.get("games", []))

    print(f"  {date_str}: {len(raw)} game(s) across {len(game_week)} day(s)")

    games_out = []
    for g in raw:
        state      = g.get("gameState", "FUT")
        status     = map_state(state)
        home_t     = g.get("homeTeam", {})
        away_t     = g.get("awayTeam", {})
        home_abbr  = home_t.get("abbrev", "???")
        away_abbr  = away_t.get("abbrev", "???")

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

        # Scores (once game has started)
        hs = home_t.get("score")
        as_ = away_t.get("score")
        if hs is not None and as_ is not None:
            obj["score"] = {home_abbr: hs, away_abbr: as_}

        # Live clock / period
        if status == "inprogress":
            pd_desc = g.get("periodDescriptor", {})
            obj["period"]      = pd_desc.get("number", 1)
            obj["period_type"] = pd_desc.get("periodType", "REG")
            obj["clock"]       = g.get("clock", {}).get("timeRemaining", "")

        games_out.append(obj)

    return {"date": date_str, "games": games_out}


# ── fetch standings ───────────────────────────────────────────────────────────

CONF_LABEL = {
    "Eastern": "EASTERN CONFERENCE",
    "Western": "WESTERN CONFERENCE",
}

def fetch_standings():
    print("Fetching standings…")
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
            "rank":            rank,
            "team":            {"name": team_full_name(t) or t.get("teamName", {}).get("default", "")},
            "wins":            wins,
            "losses":          losses + ot_losses,
            "win_percentage":  round(point_pct, 3),
            "conference":      conf_lbl,
            "division":        div_raw,
        })

    return out


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    games_data    = fetch_today_games()
    standings_data = fetch_standings()

    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "games":      games_data,
        "standings":  standings_data,
    }

    out_path = pathlib.Path(__file__).parent.parent / "data.json"
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"\n✓ data.json written ({len(games_data['games'])} games, {len(standings_data)} teams)")


if __name__ == "__main__":
    main()
