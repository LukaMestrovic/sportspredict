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

# Model for structured question parsing. Parser responses are cached to disk
# (see parser.chat_json), so the model is a one-time tournament cost and we use
# the most reliable affordable option rather than the cheapest. gpt-4.1 misparses
# far less than nano on period/market extraction; re-runs then cost $0.
PARSER_MODEL = os.environ.get("PARSER_MODEL", "gpt-4.1")
