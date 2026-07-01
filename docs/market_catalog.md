# Market Catalog

Generated from the current production matcher. Do not hand-edit the tables;
run `python -m scripts.export_market_catalog` after matcher changes.

Raw provider catalog: `soccer_live_odds_market_catalog.pdf`.

## Scope Rules

- API-Football and Odds API pre-match bookmaker contracts settle at regulation time unless explicitly documented otherwise.
- For knockout full-match questions, provider regulation markets are blocked except qualification and the labeled first-team-to-score proxy.
- Outcome sets are de-vigged only within the same bookmaker and coherent contract, then averaged across quoting bookmakers.
- Half-period Odds API markets are not wired in the current matcher.
- Unsupported, compound, and simulator-only templates must not be forced onto approximate provider contracts.

## API-Football Mappings

| intent_market | subject | period | market_key | spec_type | target | line_rule | devig | notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| match_winner | home | match | af_bet_1 | select | Home |  | same-book categorical de-vig |  |
| match_winner | away | match | af_bet_1 | select | Away |  | same-book categorical de-vig |  |
| match_draw | match | match | af_bet_1 | select | Draw |  | same-book categorical de-vig |  |
| first_team_to_score | home | match | af_bet_14 | select | Home |  | same-book categorical de-vig | Used as a labeled regulation proxy for unqualified knockout first-goal questions. |
| first_team_to_score | away | match | af_bet_14 | select | Away |  | same-book categorical de-vig | Used as a labeled regulation proxy for unqualified knockout first-goal questions. |
| btts | match | match | af_bet_8 | select | Yes |  | same-book categorical de-vig |  |
| highest_scoring_half_2h | match | match | af_bet_11 | select | 2nd Half |  | same-book categorical de-vig |  |
| red_card | match | match | af_bet_335 | ou | Over 0.5 | fixed Over 0.5 | same-book over/under de-vig |  |
| own_goal | match | match | af_bet_59 | select | Yes |  | same-book categorical de-vig |  |
| win_margin | home | match | af_bet_4 | ah | Home -line | win by N+ -> side -(N-0.5) | same-book Asian-handicap pair de-vig |  |
| win_margin | away | match | af_bet_4 | ah | Away -line | win by N+ -> side -(N-0.5) | same-book Asian-handicap pair de-vig |  |
| total_goals | match | match | af_bet_5 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| total_corners | match | match | af_bet_45 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| total_cards | match | match | af_bet_80 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| total_offsides | match | match | af_bet_164 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| total_fouls | match | match | af_bet_173 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| total_shots_on_target | match | match | af_bet_87 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| total_shots | match | match | af_bet_211 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| team_total_goals | home | match | af_bet_16 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| team_total_goals | away | match | af_bet_17 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| team_corners | home | match | af_bet_57 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| team_corners | away | match | af_bet_58 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| team_cards | home | match | af_bet_82 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| team_cards | away | match | af_bet_83 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| team_offsides | home | match | af_bet_167 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| team_offsides | away | match | af_bet_168 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| team_fouls | home | match | af_bet_171 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| team_fouls | away | match | af_bet_170 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| team_shots | home | match | af_bet_221 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| team_shots | away | match | af_bet_220 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| team_score | home | match | af_bet_43 | select | Yes |  | same-book categorical de-vig |  |
| team_score | away | match | af_bet_44 | select | Yes |  | same-book categorical de-vig |  |
| team_score_1h | home | 1H | af_bet_114 | select | Yes |  | same-book categorical de-vig |  |
| team_score_1h | away | 1H | af_bet_116 | select | Yes |  | same-book categorical de-vig |  |
| team_score_2h | home | 2H | af_bet_115 | select | Yes |  | same-book categorical de-vig |  |
| team_score_2h | away | 2H | af_bet_117 | select | Yes |  | same-book categorical de-vig |  |
| team_clean_sheet | home | match | af_bet_27 | select | Yes |  | same-book categorical de-vig |  |
| team_clean_sheet | away | match | af_bet_28 | select | Yes |  | same-book categorical de-vig |  |
| team_score_both_halves | home | match | af_bet_111 | select | Yes |  | same-book categorical de-vig |  |
| team_score_both_halves | away | match | af_bet_112 | select | Yes |  | same-book categorical de-vig |  |
| both_teams_card | match | match | af_bet_252 | select | Yes |  | same-book categorical de-vig |  |
| penalty_awarded | match | match | af_bet_163 | select | Yes |  | same-book categorical de-vig |  |
| to_advance | home | match | af_bet_61 | select | Home |  | same-book categorical de-vig |  |
| to_advance | away | match | af_bet_61 | select | Away |  | same-book categorical de-vig |  |
| corners_compare | home | match | af_bet_55 | select | Home |  | same-book categorical de-vig |  |
| corners_compare | away | match | af_bet_55 | select | Away |  | same-book categorical de-vig |  |
| offsides_compare | home | match | af_bet_165 | select | Home |  | same-book categorical de-vig |  |
| offsides_compare | away | match | af_bet_165 | select | Away |  | same-book categorical de-vig |  |
| fouls_compare | home | match | af_bet_175 | select | Home |  | same-book categorical de-vig |  |
| fouls_compare | away | match | af_bet_175 | select | Away |  | same-book categorical de-vig |  |
| shots_on_target_compare | home | match | af_bet_176 | select | Home |  | same-book categorical de-vig |  |
| shots_on_target_compare | away | match | af_bet_176 | select | Away |  | same-book categorical de-vig |  |
| match_winner | home | 1H | af_bet_13 | select | Home |  | same-book categorical de-vig |  |
| match_winner | away | 1H | af_bet_13 | select | Away |  | same-book categorical de-vig |  |
| match_winner | home | 2H | af_bet_3 | select | Home |  | same-book categorical de-vig |  |
| match_winner | away | 2H | af_bet_3 | select | Away |  | same-book categorical de-vig |  |
| match_draw | match | 1H | af_bet_13 | select | Draw |  | same-book categorical de-vig |  |
| match_draw | match | 2H | af_bet_3 | select | Draw |  | same-book categorical de-vig |  |
| corners_compare | home | 1H | af_bet_130 | select | Home |  | same-book categorical de-vig |  |
| corners_compare | away | 1H | af_bet_130 | select | Away |  | same-book categorical de-vig |  |
| corners_compare | home | 2H | af_bet_131 | select | Home |  | same-book categorical de-vig |  |
| corners_compare | away | 2H | af_bet_131 | select | Away |  | same-book categorical de-vig |  |
| total_goals | match | 1H | af_bet_6 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| total_goals | match | 2H | af_bet_26 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| total_corners | match | 1H | af_bet_77 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| total_corners | match | 2H | af_bet_127 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| total_cards | match | 1H | af_bet_155 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig | Provider names this half-card contract yellow-card O/U; the current matcher uses it only for half total_cards. |
| total_cards | match | 2H | af_bet_156 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig | Provider names this half-card contract yellow-card O/U; the current matcher uses it only for half total_cards. |
| team_total_goals | home | 1H | af_bet_105 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| team_total_goals | away | 1H | af_bet_106 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| team_total_goals | home | 2H | af_bet_107 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| team_total_goals | away | 2H | af_bet_108 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| team_corners | home | 1H | af_bet_132 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| team_corners | away | 1H | af_bet_134 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| team_corners | home | 2H | af_bet_133 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| team_corners | away | 2H | af_bet_135 | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book over/under de-vig |  |
| btts | match | 1H | af_bet_34 | select | Yes |  | same-book categorical de-vig |  |
| btts | match | 2H | af_bet_35 | select | Yes |  | same-book categorical de-vig |  |
| player_goal_scorer | player | match | af_bet_92 | player_yes | player value |  | single-sided player prop haircut |  |
| player_card | player | match | af_bet_251 | player_yes | player value |  | single-sided player prop haircut |  |
| player_shots_on_target | player | match | af_bet_242 | player_threshold | Player - N+ | gte N -> Over N-0.5; lte N -> Under N+0.5 | single-sided player prop haircut |  |

## Odds API Mappings

| intent_market | subject | period | market_key | kind | target | line_rule | devig | notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| match_winner | home | match | h2h | multiway | team name |  | same-book categorical de-vig |  |
| corners_compare | home | match | corners_1x2 | multiway | team name |  | same-book categorical de-vig |  |
| double_chance | home | match | draw_no_bet | multiway | team name |  | same-book categorical de-vig | Current fallback uses draw_no_bet for this intent. |
| match_winner | away | match | h2h | multiway | team name |  | same-book categorical de-vig |  |
| corners_compare | away | match | corners_1x2 | multiway | team name |  | same-book categorical de-vig |  |
| double_chance | away | match | draw_no_bet | multiway | team name |  | same-book categorical de-vig | Current fallback uses draw_no_bet for this intent. |
| btts | match | match | btts | yesno | Yes |  | same-book two-sided de-vig |  |
| total_goals | match | match | totals | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book two-sided de-vig |  |
| total_corners | match | match | alternate_totals_corners | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book two-sided de-vig |  |
| total_cards | match | match | alternate_totals_cards | ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book two-sided de-vig |  |
| player_goal_scorer | player | match | player_goal_scorer_anytime | player_yesno | player Yes |  | same-book two-sided de-vig if both sides exist |  |
| player_score_or_assist | player | match | player_to_score_or_assist | player_yesno | player Yes |  | same-book two-sided de-vig if both sides exist |  |
| player_card | player | match | player_to_receive_card | player_yesno | player Yes |  | same-book two-sided de-vig if both sides exist |  |
| player_shots_on_target | player | match | player_shots_on_target | player_ou |  | gte N -> Over N-0.5; lte N -> Under N+0.5 | same-book two-sided de-vig if both sides exist |  |

## Provider Gaps

| intent_market | reason |
| --- | --- |
| cards_compare | No coherent all-card provider comparison is wired; the yellow-card comparison market is intentionally not reused. |
| team_shots_on_target | No direct provider mapping is wired; simulator context may handle it. |
| none | Parser uses this for compounds or templates that need non-provider handling. |
