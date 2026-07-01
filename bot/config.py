"""Central config: loads keys from .env and exposes constants."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    """Minimal .env loader (no dependency on python-dotenv)."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


_load_env()

SPORTSPREDICT_KEY = os.environ.get("SPORTSPREDICT_KEY", "")
APIFOOTBALL_KEY = os.environ.get("APIFOOTBALL_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")

SP_BASE = "https://api.sportspredict.com/api/v1"
AF_BASE = "https://v3.football.api-sports.io"
FIFA_BASE = "https://api.fifa.com/api/v3"
ODDS_BASE = "https://api.the-odds-api.com/v4"
ODDS_SPORT = "soccer_fifa_world_cup"
# Regions to query on the (paid) Odds API. More regions = more books but more
# cost (billed markets × regions). US is included because some player props —
# notably score-or-assist — are quoted only by US books (DraftKings, FanDuel),
# and the extra books tighten the de-vig average on the markets EU/UK already
# cover.
ODDS_REGIONS = os.environ.get("ODDS_REGIONS", "eu,uk,us")

# FIFA World Cup 2026 in API-Football.
WC_LEAGUE_ID = 1
WC_SEASON = 2026
# FIFA's public match-centre API identifiers for the 2026 men's World Cup.
FIFA_WC_COMPETITION_ID = "17"
FIFA_WC_SEASON_ID = "285023"


def _int_list(value: str) -> list[int]:
    return [int(x) for x in value.split(",") if x.strip()]


# Referee discipline history. API-Football has no referee endpoint and rejects a
# referee filter on /fixtures, so we scan these competitions' fixtures (shared,
# cached) and match the assigned referee by name across them — far deeper than the
# 1-3 games a referee gets in the WC alone. Elite international referees mostly
# work top UEFA leagues + continental + confederation competitions, so this set
# captures the bulk of their recent matches. Override via REFEREE_SCAN_LEAGUES.
REFEREE_SCAN_LEAGUES = _int_list(os.environ.get(
    "REFEREE_SCAN_LEAGUES",
    # WC, UCL, UEL, UECL, Euro, Copa America, then the big domestic top flights.
    "1,2,3,848,4,9,39,140,135,78,61,94,88,71,128",
))
# Seasons to scan for the domestic/continental leagues above (the current WC is
# always included separately via the cached WC fixtures). Recent completed
# seasons give the freshest card profile.
REFEREE_SCAN_SEASONS = _int_list(os.environ.get(
    "REFEREE_SCAN_SEASONS", f"{WC_SEASON - 1},{WC_SEASON - 2}",
))

# Model for structured question parsing. Parser responses are cached to disk
# (see parser.chat_json), so the model is a one-time tournament cost and we use
# the most reliable affordable option rather than the cheapest. gpt-5.4-mini
# parses period/market extraction reliably and accepts temperature=0 + seed on
# chat/completions, so re-runs stay deterministic and then cost $0.
PARSER_MODEL = os.environ.get("PARSER_MODEL", "gpt-5.4-mini")
