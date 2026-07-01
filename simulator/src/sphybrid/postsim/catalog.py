"""The compact team-name vocabulary used by the checked SportPredict snapshot."""

TEAM_CODE_NAMES = {
    "ALG": "Algeria", "ARG": "Argentina", "AUS": "Australia", "AUT": "Austria",
    "BEL": "Belgium", "BIH": "Bosnia and Herzegovina", "BRA": "Brazil", "CAN": "Canada",
    "CIV": "Ivory Coast", "COD": "DR Congo", "COL": "Colombia", "CPV": "Cape Verde",
    "CRO": "Croatia", "CZE": "Czechia", "CUW": "Curaçao", "ECU": "Ecuador",
    "EGY": "Egypt", "ENG": "England", "ESP": "Spain", "FRA": "France",
    "GER": "Germany", "GHA": "Ghana", "HAI": "Haiti", "IRN": "Iran", "IRQ": "Iraq",
    "JOR": "Jordan", "JPN": "Japan", "KOR": "South Korea", "KSA": "Saudi Arabia",
    "MAR": "Morocco", "MEX": "Mexico", "NED": "Netherlands", "NOR": "Norway",
    "NZL": "New Zealand", "PAN": "Panama", "PAR": "Paraguay", "POR": "Portugal",
    "QAT": "Qatar", "RSA": "South Africa", "SCO": "Scotland", "SEN": "Senegal",
    "SUI": "Switzerland", "SWE": "Sweden", "TUN": "Tunisia", "TUR": "Türkiye",
    "URU": "Uruguay", "USA": "United States", "UZB": "Uzbekistan",
}


def catalog_teams(match_name: str) -> tuple[str, str]:
    left, right = str(match_name).split(" vs ", 1)
    return TEAM_CODE_NAMES.get(left, left), TEAM_CODE_NAMES.get(right, right)
