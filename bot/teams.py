"""Name normalization shared by provider matching."""
from __future__ import annotations

import re
import unicodedata


_ALIASES = {
    "alg": "algeria", "arg": "argentina", "aus": "australia",
    "aut": "austria", "bel": "belgium", "bih": "bosnia herzegovina",
    "bra": "brazil", "can": "canada", "civ": "ivory coast",
    "cod": "congo dr", "col": "colombia", "cpv": "cape verde",
    "cro": "croatia", "cze": "czechia", "ecu": "ecuador",
    "egy": "egypt", "eng": "england", "esp": "spain", "fra": "france",
    "ger": "germany", "gha": "ghana", "irn": "iran", "irq": "iraq",
    "jor": "jordan", "jpn": "japan", "kor": "south korea",
    "ksa": "saudi arabia", "mar": "morocco", "mex": "mexico",
    "ned": "netherlands", "nor": "norway", "pan": "panama",
    "par": "paraguay", "por": "portugal", "qat": "qatar",
    "rsa": "south africa", "sco": "scotland", "sen": "senegal",
    "sui": "switzerland", "swe": "sweden", "tun": "tunisia",
    "tur": "turkiye", "uru": "uruguay", "usa": "united states",
    "uzb": "uzbekistan",
}


def _normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def normalize_team(name: str) -> str:
    text = _normalize_name(name)
    text = _ALIASES.get(text, text)
    # Providers disagree about the word order for DR Congo.
    if text in ("dr congo", "congo dr"):
        return "congo dr"
    if text in ("cabo verde", "cape verde islands"):
        return "cape verde"
    if text == "bosnia and herzegovina":
        return "bosnia herzegovina"
    return text


def split_match_name(name: str) -> tuple[str, str] | None:
    parts = re.split(r"\s+vs\.?\s+", name or "", maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return None
    return normalize_team(parts[0]), normalize_team(parts[1])


def same_team(left: str, right: str) -> bool:
    return normalize_team(left) == normalize_team(right)


def player_matches(candidate: str, player: str) -> bool:
    """Match provider player names despite accents or abbreviated forenames."""
    candidate = _normalize_name(candidate)
    player = _normalize_name(player)
    if not candidate or not player:
        return False
    if candidate == player or candidate in player or player in candidate:
        return True
    surname = player.split()[-1]
    return len(surname) >= 4 and surname in candidate.split()
