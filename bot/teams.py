"""Team-name normalization shared by provider fixture/event matching."""
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


def normalize_team(name: str) -> str:
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    text = _ALIASES.get(text, text)
    # Providers disagree about the word order for DR Congo.
    if text in ("dr congo", "congo dr"):
        return "congo dr"
    if text == "cape verde islands":
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
