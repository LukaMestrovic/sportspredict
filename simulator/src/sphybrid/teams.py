"""FIFA 3-letter code -> Elo/results dataset team name.

SportsPredict names matches by code ("KOR vs CZE"); our Elo table (martj42 results dataset)
uses full names ("South Korea", "Czech Republic"). This map bridges them so strength lookups
work. Unknown codes fall back to the code itself (Elo default 1500); extend as needed.
"""

from __future__ import annotations

FIFA_CODE_TO_NAME: dict[str, str] = {
    # Hosts
    "USA": "United States", "CAN": "Canada", "MEX": "Mexico",
    # CONMEBOL
    "ARG": "Argentina", "BRA": "Brazil", "URU": "Uruguay", "COL": "Colombia",
    "ECU": "Ecuador", "PAR": "Paraguay", "PER": "Peru", "CHI": "Chile",
    "VEN": "Venezuela", "BOL": "Bolivia",
    # UEFA
    "ESP": "Spain", "FRA": "France", "ENG": "England", "GER": "Germany",
    "POR": "Portugal", "NED": "Netherlands", "BEL": "Belgium", "CRO": "Croatia",
    "ITA": "Italy", "SUI": "Switzerland", "DEN": "Denmark", "SRB": "Serbia",
    "POL": "Poland", "WAL": "Wales", "AUT": "Austria", "UKR": "Ukraine",
    "SCO": "Scotland", "TUR": "Turkey", "NOR": "Norway", "SWE": "Sweden",
    "CZE": "Czech Republic", "HUN": "Hungary", "ROU": "Romania", "GRE": "Greece",
    "SVN": "Slovenia", "SVK": "Slovakia", "MKD": "North Macedonia",
    "BIH": "Bosnia and Herzegovina", "IRL": "Republic of Ireland", "NIR": "Northern Ireland",
    "ALB": "Albania", "GEO": "Georgia", "ISL": "Iceland", "FIN": "Finland",
    # CONCACAF
    "CRC": "Costa Rica", "PAN": "Panama", "JAM": "Jamaica", "HON": "Honduras",
    "SLV": "El Salvador", "GUA": "Guatemala", "HAI": "Haiti", "TRI": "Trinidad and Tobago",
    "CUW": "Curaçao", "SUR": "Suriname",
    # SportsPredict sometimes uses full names without diacritics in match names.
    "CURACAO": "Curaçao",
    # AFC
    "JPN": "Japan", "KOR": "South Korea", "IRN": "Iran", "AUS": "Australia",
    "KSA": "Saudi Arabia", "QAT": "Qatar", "IRQ": "Iraq", "UAE": "United Arab Emirates",
    "UZB": "Uzbekistan", "JOR": "Jordan", "OMA": "Oman", "BHR": "Bahrain",
    "CHN": "China PR", "IND": "India",
    # CAF
    "MAR": "Morocco", "SEN": "Senegal", "TUN": "Tunisia", "GHA": "Ghana",
    "CMR": "Cameroon", "NGA": "Nigeria", "EGY": "Egypt", "ALG": "Algeria",
    "CIV": "Ivory Coast", "RSA": "South Africa", "COD": "DR Congo", "CPV": "Cape Verde",
    "MLI": "Mali", "BFA": "Burkina Faso", "ANG": "Angola", "ZAM": "Zambia",
    # OFC
    "NZL": "New Zealand",
}


def canonical_name(team: str, elo_table: dict | None = None) -> str:
    """Resolve a team token to its dataset name (exact name passes through)."""
    if elo_table and team in elo_table:
        return team
    return FIFA_CODE_TO_NAME.get(team.strip().upper(), team)
