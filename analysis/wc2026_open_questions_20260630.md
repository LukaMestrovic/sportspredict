# WC2026 open-question pipeline audit — 2026-06-30

Captured 180 open questions across 12 SportPredict matches. Every question was parsed with the parser LLM patched to fail, proving that the current inventory is deterministic. Evidence-route counts: AF exact 117, AF regulation proxy 7, simulator 54, web-only 2.

`AF exact` means the exact API-Football contract had at least one current/cached observation. `AF regulation proxy` is the deliberate use of bet 14 for a full-match first-goal question; the ET-only difference is accepted as immaterial and remains labeled in evidence. `simulator` is the exact fallback contract when no API-Football observation was available; an exact Odds API quote can supersede it during the live evidence build. `web-only` is intentionally empty deterministic evidence and must be priced/audited by the web-grounded LLM.

## Regulation / extra-time distinctions

- The seven “first goal of the match” questions remain `full_match`, but use regulation bet 14 as a labeled primary proxy because the ET-only difference is accepted as immaterial.
- “Will a red card be shown in the match?” is `full_match` and uses `red_card:match`; standard regulation red-card odds are rejected.
- Both open “after the second hydration break in regulation” questions use `goal_window:after_second_hydration:reg`. The otherwise-identical unqualified wording is covered by regression tests and uses `goal_window:after_second_hydration:et`.
- “Before the first hydration break”, halftime, and stoppage-time windows are regulation-bounded by their wording even without the word “regulation”.
- The three advancement questions are full qualification contracts and correctly use bet 61, including extra time/penalties when required.

## Confirmed fixes

- Preserved `time_scope` through deterministic parsing and the evidence/LLM handoff; mismatched regulation odds are blocked for full-match knockout contracts.
- Corrected the full-match first-goal simulator counter to include extra time.
- Mapped own goal to coherent Yes/No bet 59.
- Removed generic card comparisons from yellow-card-only bet 158 and routed them to the all-card simulator counter.
- Routed team scoring with “excluding own goals” to a dedicated non-own-goal simulator counter.
- Made the DR Congo team-score and both goal-method wordings deterministic instead of parser-LLM fallbacks.

## CIV vs NOR — 2026-06-30T17:00:00.000Z

- `f79c098c-ce29-4a68-8703-d357245b9a81` — **regulation / match_winner** — AF exact bet 1 (Norway win) — Will Norway win in regulation (90 minutes + stoppage time)?
- `91263748-3e9a-4100-9010-414ac8c1381d` — **regulation / player_goal_scorer** — AF exact bet 92 (player anytime scorer) — Will Erling Haaland (Norway) score a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `0231307d-2a42-47cd-998b-79498a1d010c` — **regulation / corners_compare** — AF exact bet 55 (corners_compare home) — Will Ivory Coast have more corner kicks than Norway in regulation (90 minutes + stoppage time)?
- `5101894f-3d9b-4ca8-9770-fab659e055ec` — **regulation / player_score_or_assist** — simulator `player_score_or_assist:full:reg:player` — Will Martin Ødegaard (Norway) score or assist a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `1ab4c264-7911-45ad-ad79-a528a454eb83` — **regulation / btts** — AF exact bet 8 (Both teams score) — Will both teams score in regulation (90 minutes + stoppage time)?
- `c0fb5c86-a449-4043-8ba0-98ef6b1a531d` — **regulation / player_shots_on_target** — AF exact bet 242 (player SoT) — Will Amad Diallo (Ivory Coast) have 1 or more shots on target in regulation (90 minutes + stoppage time)?
- `2318f323-20bc-41ca-8973-10aead65568a` — **regulation / total_goals** — AF exact bet 5 (total_goals Over 2.5) — Will the match have 3 or more total goals in regulation (90 minutes + stoppage time)?
- `6884f21b-23c3-4663-b7b3-0487ec6b64ea` — **regulation / none** — simulator `goal_window:before_first_hydration:reg` — Will a goal be scored before the first hydration break?
- `14b4ad9e-de55-4d15-8e3b-a4dda355438f` — **regulation / team_shots_on_target** — simulator `count:shots_on_target:team:full:>=:6:reg` — Will Norway have 6 or more shots on target in regulation (90 minutes + stoppage time)?
- `3289ea43-973f-4f2a-86a2-39d3fc7750a4` — **regulation / player_shots_on_target** — simulator `player_stat:shots_on_target:full:>=:2:reg:player` — Will Alexander Sørloth (Norway) have 2 or more shots on target in regulation (90 minutes + stoppage time)?
- `b58c9664-1cc6-4f32-97ab-a20235f7d5da` — **full_match / first_team_to_score** — AF regulation proxy bet 14 (away scores first) — Will Norway score the first goal of the match?
- `654cd80b-9113-4492-8430-a43381e4387f` — **regulation / total_offsides** — AF exact bet 164 (total_offsides Over 3.5) — Will there be 4 or more offside calls in regulation (90 minutes + stoppage time)?
- `400defa1-95d4-4aa0-bcaa-70c36c520a8a` — **regulation / total_cards** — AF exact bet 80 (total_cards Over 3.5) — Will there be 4 or more total cards shown in regulation (90 minutes + stoppage time)?
- `20d8186a-6616-46bd-bc59-52c125028520` — **regulation / none** — simulator `goal_window:after_second_hydration:reg` — Will a goal be scored after the second hydration break in regulation (90 minutes + stoppage time)?
- `c0e3f7a7-4731-473b-9eac-0770cc6f8541` — **regulation / penalty_awarded** — simulator `penalty_awarded:reg` — Will a penalty kick be awarded during regulation (90 minutes + stoppage time)?

## FRA vs SWE — 2026-06-30T21:00:00.000Z

- `45596cfc-fc5e-4588-a690-71d50e147e1a` — **regulation / total_goals** — AF exact bet 5 (total_goals Over 2.5) — Will the match have 3 or more total goals in regulation (90 minutes + stoppage time)?
- `cadaa5f5-c94d-4ba3-8758-c47da0bef49c` — **regulation / team_corners** — AF exact bet 57 (home team_corners Over 5.5) — Will France have 6 or more corner kicks in regulation (90 minutes + stoppage time)?
- `12f2d03c-6f5a-4176-bdf4-926b41741ddd` — **regulation / match_winner** — AF exact bet 1 (France win) — Will France win in regulation (90 minutes + stoppage time)?
- `11dcdc50-811d-4f68-a60a-90db4ec7b0de` — **regulation / player_goal_scorer** — AF exact bet 92 (player anytime scorer) — Will Ousmane Dembélé (France) score a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `1428fb96-6eb5-4510-a4ce-cce8c31deda2` — **regulation / match_winner** — AF exact bet 13 (match_winner home 1H) — Will France be ahead at halftime?
- `deb0412b-45c3-48f7-88d4-df4a95913c3f` — **regulation / player_shots_on_target** — AF exact bet 242 (player SoT) — Will Kylian Mbappé (France) have 2 or more shots on target in regulation (90 minutes + stoppage time)?
- `7f647172-e410-45cd-bdb0-d660e2221568` — **full_match / red_card** — simulator `red_card:match` — Will a red card be shown in the match?
- `c0e4cdaf-87d1-44ec-9cb8-2a0fade828cb` — **regulation / player_goal_scorer** — AF exact bet 92 (player anytime scorer) — Will Viktor Gyökeres (Sweden) score a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `abd59093-ba5b-4947-8923-2513ae203251` — **regulation / player_shots_on_target** — AF exact bet 242 (player SoT) — Will Alexander Isak (Sweden) have 2 or more shots on target in regulation (90 minutes + stoppage time)?
- `9cc9f5d8-6b2f-4a5a-96f1-84ea7b5875a9` — **full_match / first_team_to_score** — AF regulation proxy bet 14 (home scores first) — Will France score the first goal of the match?
- `8cf30b49-0133-481c-94fe-507bdf509d93` — **regulation / btts** — AF exact bet 8 (Both teams score) — Will both teams score in regulation (90 minutes + stoppage time)?
- `52d5f862-b2c6-4f05-9c41-8d4b150974fc` — **regulation / team_shots_on_target** — simulator `count:shots_on_target:team:full:>=:7:reg` — Will France have 7 or more shots on target in regulation (90 minutes + stoppage time)?
- `5464f357-04db-4d93-b147-a79e6fe71e6a` — **regulation / total_cards** — AF exact bet 80 (total_cards Over 4.5) — Will there be 5 or more total cards shown in regulation (90 minutes + stoppage time)?
- `b3bf11ad-9c24-4a88-ba56-f7458050e69b` — **regulation / none** — simulator `goal_window:stoppage:1H` — Will a goal be scored in first-half stoppage (added) time?
- `fc83868d-6c29-405c-bb17-2cb74c4de18a` — **regulation / total_offsides** — AF exact bet 164 (total_offsides Over 3.5) — Will there be 4 or more offside calls in regulation (90 minutes + stoppage time)?

## MEX vs ECU — 2026-07-01T01:00:00.000Z

- `9d20d804-0d06-44c0-aaca-fba06f49d2f5` — **regulation / player_goal_scorer** — AF exact bet 92 (player anytime scorer) — Will Raúl Jiménez (Mexico) score a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `531750f0-12c4-4559-89cf-cf91987edf21` — **regulation / highest_scoring_half_2h** — AF exact bet 11 (2nd half outscores 1st) — Will the second half produce more goals than the first half in regulation (90 minutes + stoppage time)?
- `ee51fb9b-059a-4aac-b060-2e9bb5b0c425` — **regulation / shots_on_target_compare** — AF exact bet 176 (shots_on_target_compare home) — Will Mexico have more shots on target than Ecuador in regulation (90 minutes + stoppage time)?
- `10ab79b8-6b9d-444a-99cc-7935b3cc5b7b` — **regulation / total_goals** — AF exact bet 5 (total_goals Under 2.5) — Will the match have 2 or fewer total goals in regulation (90 minutes + stoppage time)?
- `b921360c-9d05-44ed-8d87-b7a5dfb8a6ea` — **regulation / player_score_or_assist** — simulator `player_score_or_assist:full:reg:player` — Will Gonzalo Plata (Ecuador) score or assist a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `e520c9b6-add6-46bf-99f2-372b438f2ecb` — **regulation / team_shots_on_target** — simulator `count:shots_on_target:team:full:>=:6:reg` — Will Mexico have 6 or more shots on target in regulation (90 minutes + stoppage time)?
- `6ec9baf1-6621-49b7-9c1a-3590108864df` — **regulation / none** — web-only; no exact provider contract or defensible simulator counter — Will a goal be scored from outside the penalty area in regulation (90 minutes + stoppage time)?
- `2a6604ab-935a-4209-8f8f-5a2b323a8af0` — **regulation / own_goal** — AF exact bet 59 (own goal scored) — Will an own goal be scored in regulation (90 minutes + stoppage time)?
- `07127645-b758-4a83-b7c1-037142024aca` — **regulation / match_winner** — AF exact bet 1 (Mexico win) — Will Mexico win in regulation (90 minutes + stoppage time)?
- `4e43838b-83e1-4e09-92ae-1bf4cc60248d` — **regulation / total_corners** — AF exact bet 45 (total_corners Over 8.5) — Will there be 9 or more total corner kicks in regulation (90 minutes + stoppage time)?
- `afc7eab2-5a3d-4fb8-a382-ce73179ea58e` — **regulation / total_cards** — AF exact bet 80 (total_cards Over 3.5) — Will there be 4 or more total cards shown in regulation (90 minutes + stoppage time)?
- `eb778538-8707-454d-a2bb-f35ab5adc63d` — **regulation / total_offsides** — AF exact bet 164 (total_offsides Over 2.5) — Will there be 3 or more offside calls in regulation (90 minutes + stoppage time)?
- `3ccc77b7-0d13-4a43-8fca-378f99e84fe1` — **regulation / none** — simulator `goal_window:before_first_hydration:reg` — Will a goal be scored before the first hydration break?
- `983f8862-ce46-4378-adfa-238c40d4ed55` — **regulation / penalty_awarded** — simulator `penalty_awarded:reg` — Will a penalty kick be awarded during regulation (90 minutes + stoppage time)?
- `da1eb7f8-d303-4803-984e-7119ce5e200c` — **regulation / total_shots** — AF exact bet 211 (total_shots Over 19.5) — Will there be 20 or more total shots (on and off target) in regulation (90 minutes + stoppage time)?

## ENG vs COD — 2026-07-01T16:00:00.000Z

- `eb751cb1-34db-4ade-87b3-4867adc60762` — **regulation / player_goal_scorer** — AF exact bet 92 (player anytime scorer) — Will Harry Kane (England) score a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `72926a85-3305-49b4-a59a-4b362e73ff2f` — **regulation / player_shots_on_target** — AF exact bet 242 (player SoT) — Will Jude Bellingham (England) have 2 or more shots on target in regulation (90 minutes + stoppage time)?
- `f0aec6a6-d4ee-47fd-a055-c11fb6b55575` — **regulation / team_score** — simulator `team_score_no_own:reg` — Will DR Congo score a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `8346954b-16e8-40ac-bfe0-f70719eb5d42` — **regulation / player_shots_on_target** — AF exact bet 242 (player SoT) — Will Yoane Wissa (DR Congo) have 1 or more shots on target in regulation (90 minutes + stoppage time)?
- `fd024337-d00f-40ff-a848-749bd9e8fdf6` — **regulation / total_goals** — AF exact bet 5 (total_goals Over 2.5) — Will the match have 3 or more total goals in regulation (90 minutes + stoppage time)?
- `97ef8e60-afe0-4262-8ae7-ba033cda5fcf` — **regulation / team_score_both_halves** — AF exact bet 111 (team_score_both_halves home Yes) — Will England score in both halves in regulation (90 minutes + stoppage time)?
- `86ab3fdd-ab63-4c30-ad0d-c4984a4dd815` — **regulation / team_shots_on_target** — simulator `count:shots_on_target:team:full:>=:7:reg` — Will England have 7 or more shots on target in regulation (90 minutes + stoppage time)?
- `6fae8ac9-956e-4353-bb59-d8545615f9f6` — **regulation / team_corners** — AF exact bet 57 (home team_corners Over 7.5) — Will England have 8 or more corner kicks in regulation (90 minutes + stoppage time)?
- `4fa926dd-b31c-4704-9916-49a4632997e5` — **regulation / total_cards** — AF exact bet 80 (total_cards Over 3.5) — Will there be 4 or more total cards shown in regulation (90 minutes + stoppage time)?
- `67f7c8e7-6a11-42b9-82be-747a20521fa1` — **regulation / total_shots** — AF exact bet 211 (total_shots Over 19.5) — Will there be 20 or more total shots (on and off target) in regulation (90 minutes + stoppage time)?
- `7cf17dfa-063b-49ab-a533-13a605668343` — **regulation / highest_scoring_half_2h** — AF exact bet 11 (2nd half outscores 1st) — Will the second half produce more goals than the first half in regulation (90 minutes + stoppage time)?
- `fdb9842e-1c1c-4969-b0e1-d74d9f2b20cb` — **regulation / total_offsides** — AF exact bet 164 (total_offsides Over 2.5) — Will there be 3 or more offside calls in regulation (90 minutes + stoppage time)?
- `f04e2104-a49d-486c-8979-3a997f85236f` — **regulation / none** — simulator `goal_window:before_first_hydration:reg` — Will a goal be scored before the first hydration break?
- `5d743959-ac57-4e6d-9173-42c1255a179b` — **regulation / none** — simulator `penalty_or_red:reg` — Will a penalty kick be awarded OR a red card be shown in regulation (90 minutes + stoppage time)?
- `a85eda15-8d81-4062-9784-55e737ad60f0` — **regulation / win_margin** — simulator `win_margin:reg:2` — Will England win by 2 or more goals in regulation (90 minutes + stoppage time)?

## BEL vs SEN — 2026-07-01T20:00:00.000Z

- `52cf9f3d-d0fe-4d56-b9c9-f8cbc1e2d58f` — **regulation / match_winner** — AF exact bet 1 (Belgium win) — Will Belgium win in regulation (90 minutes + stoppage time)?
- `f9b9f00b-e947-4a3f-a552-4da006855e8a` — **regulation / player_goal_scorer** — AF exact bet 92 (player anytime scorer) — Will Leandro Trossard (Belgium) score a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `c3857191-3710-4ddf-ba37-5f96cc6a621a` — **regulation / player_score_or_assist** — simulator `player_score_or_assist:full:reg:player` — Will Kevin De Bruyne (Belgium) score or assist a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `933d0096-e687-4d48-9548-bfc241999ad6` — **regulation / btts** — AF exact bet 8 (Both teams score) — Will both teams score in regulation (90 minutes + stoppage time)?
- `387385be-1dd3-4439-b25f-ad48d1669be8` — **regulation / player_shots_on_target** — AF exact bet 242 (player SoT) — Will Sadio Mané (Senegal) have 2 or more shots on target in regulation (90 minutes + stoppage time)?
- `1a42854d-1a16-4196-a59c-98b7fed0ebc9` — **regulation / none** — simulator `substitute_score:reg` — Will a substitute score a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `b9cbc816-a779-4a0b-ac9e-be1195b96c12` — **regulation / total_goals** — AF exact bet 5 (total_goals Over 2.5) — Will the match have 3 or more total goals in regulation (90 minutes + stoppage time)?
- `f9f444f0-9ffe-4d13-a840-e05205b4d971` — **regulation / team_shots_on_target** — simulator `count:shots_on_target:team:full:>=:7:reg` — Will Belgium have 7 or more shots on target in regulation (90 minutes + stoppage time)?
- `c7eee461-d7d4-4d81-96d9-1d92d7601e38` — **regulation / total_cards** — AF exact bet 80 (total_cards Over 3.5) — Will there be 4 or more total cards shown in regulation (90 minutes + stoppage time)?
- `39c26a4c-4dc7-451f-acde-a04e64a7263e` — **regulation / player_shots_on_target** — AF exact bet 242 (player SoT) — Will Ismaïla Sarr (Senegal) have 1 or more shots on target in regulation (90 minutes + stoppage time)?
- `9ea342d2-bd40-4fe5-9f70-c96d6b317f97` — **regulation / team_corners** — AF exact bet 57 (home team_corners Over 5.5) — Will Belgium have 6 or more corner kicks in regulation (90 minutes + stoppage time)?
- `52ac1fc3-f481-46a3-b70a-91ab0c9c9901` — **regulation / match_winner** — AF exact bet 13 (match_winner home 1H) — Will Belgium be ahead at halftime?
- `61f9a9e0-b01c-4d69-b5e8-3af2c00a5e6c` — **regulation / none** — simulator `goal_window:stoppage:1H` — Will a goal be scored in first-half stoppage time?
- `45ecfed3-02b8-4f38-aa5a-aba44208c4aa` — **regulation / total_shots** — AF exact bet 211 (total_shots Over 23.5) — Will there be 24 or more total shots (on and off target) in regulation (90 minutes + stoppage time)?
- `32be95af-c864-46ba-968b-6ad327fc50ec` — **regulation / none** — simulator `penalty_or_red:reg` — Will a penalty kick be awarded OR a red card be shown in regulation (90 minutes + stoppage time)?

## USA vs BIH — 2026-07-02T00:00:00.000Z

- `61587b7b-05b3-4213-b521-d0162977620b` — **regulation / none** — simulator `goal_window:stoppage:2H` — Will a goal be scored in second-half stoppage time?
- `ced80060-58e1-426a-be44-e4e9c21d2cee` — **regulation / none** — simulator `stat_window:corners:before_first_hydration:reg:>=:2` — Will 2 or more corner kicks be taken before the first hydration break?
- `860a1474-3bb3-4d06-b9e2-ab291e239dc4` — **regulation / team_corners** — AF exact bet 57 (home team_corners Over 5.5) — Will the United States have 6 or more corner kicks in regulation (90 minutes + stoppage time)?
- `d6008186-be02-46a0-8f3c-36a92b90b74f` — **regulation / none** — simulator `substitution_before_halftime:reg` — Will a substitution be made before halftime?
- `619b26a6-2562-4bec-9f98-b7f7f40971b9` — **regulation / total_cards** — AF exact bet 80 (total_cards Over 4.5) — Will there be 5 or more total cards shown in regulation (90 minutes + stoppage time)?
- `001cdb6c-ef79-44f5-aa21-4e5298d099ab` — **regulation / none** — simulator `penalty_or_red:reg` — Will a penalty kick be awarded OR a red card be shown in regulation (90 minutes + stoppage time)?
- `4c29ab08-ca3d-470b-97d0-a2815d0ea076` — **regulation / win_margin** — AF exact bet 4 (home win by 2+) — Will the United States win by 2 or more goals in regulation (90 minutes + stoppage time)?
- `99dde047-6147-4d00-9e29-8b482e7b81f8` — **regulation / cards_compare** — simulator `compare:cards:full:reg` — Will Bosnia and Herzegovina receive more cards than the United States in regulation (90 minutes + stoppage time)?
- `684e0f33-1bb7-42ad-ada3-d56bf1a34610` — **regulation / player_goal_scorer** — AF exact bet 92 (player anytime scorer) — Will Folarin Balogun score a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `060bce76-0a84-477f-ba6f-c421a1ee53bd` — **regulation / total_goals** — AF exact bet 5 (total_goals Under 2.5) — Will the match have 2 or fewer total goals in regulation (90 minutes + stoppage time)?
- `0841ea27-cfc2-48f2-ab22-c459b89b2bcb` — **regulation / total_offsides** — AF exact bet 164 (total_offsides Over 3.5) — Will there be 4 or more offside calls in regulation (90 minutes + stoppage time)?
- `cf910072-29cf-45f6-bd67-d04d47115ecd` — **regulation / player_shots_on_target** — AF exact bet 242 (player SoT) — Will Ermedin Demirović have at least 1 shot on target in regulation (90 minutes + stoppage time)?
- `f96b7297-29cc-4caa-bf00-2fe5fa5dff36` — **regulation / total_cards** — AF exact bet 155 (total_cards Over 0.5 1H) — Will a card be shown in the first half?
- `4ad0956e-dd6c-40d0-9762-fd8d96bb83e4` — **regulation / total_shots** — AF exact bet 211 (total_shots Over 21.5) — Will there be 22 or more total shots (on and off target) in regulation (90 minutes + stoppage time)?
- `c64a7514-26e5-414b-b383-5c99bf06dc3f` — **regulation / team_shots_on_target** — simulator `count:shots_on_target:team:full:>=:6:reg` — Will the United States have 6 or more shots on target in regulation (90 minutes + stoppage time)?

## ESP vs AUT — 2026-07-02T19:00:00.000Z

- `d7a2e383-f1d2-4330-98fa-3e0be849414a` — **regulation / player_shots_on_target** — AF exact bet 242 (player SoT) — Will Lamine Yamal (Spain) have 2 or more shots on target in regulation (90 minutes + stoppage time)?
- `15ef3b72-4d19-41b1-8381-7964a9d16bfe` — **regulation / player_goal_scorer** — AF exact bet 92 (player anytime scorer) — Will Mikel Oyarzabal (Spain) score a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `7c63bc5a-d25a-4f9f-afa8-343c6fa5ebee` — **regulation / player_score_or_assist** — simulator `player_score_or_assist:full:reg:player` — Will Marcel Sabitzer (Austria) score or assist a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `99c182e7-b2aa-40d3-a1fb-dbe304d17754` — **regulation / total_goals** — AF exact bet 5 (total_goals Over 2.5) — Will the match have 3 or more total goals in regulation (90 minutes + stoppage time)?
- `5a166126-68d8-412d-a368-b1d4f94e2d87` — **full_match / first_team_to_score** — AF regulation proxy bet 14 (home scores first) — Will Spain score the first goal of the match?
- `5bc19a3f-7e30-45c8-85eb-c3f9c583c332` — **regulation / team_shots_on_target** — simulator `count:shots_on_target:team:full:>=:8:reg` — Will Spain have 8 or more shots on target in regulation (90 minutes + stoppage time)?
- `061aeb81-321a-4deb-8998-e2a1f55a7c4c` — **regulation / team_shots_on_target** — simulator `count:shots_on_target:team:full:>=:4:reg` — Will Austria have 4 or more shots on target in regulation (90 minutes + stoppage time)?
- `48406c6e-9222-4f1b-b57f-16f979b54cd5` — **regulation / team_corners** — AF exact bet 57 (home team_corners Over 6.5) — Will Spain have 7 or more corner kicks in regulation (90 minutes + stoppage time)?
- `e0c1df27-c528-4f2e-b51f-2ae60b6ab9c8` — **regulation / match_winner** — AF exact bet 13 (match_winner home 1H) — Will Spain be ahead at halftime?
- `29ee5b9f-5806-46b3-aed3-578cafd33f8b` — **regulation / btts** — AF exact bet 8 (Both teams score) — Will both teams score in regulation (90 minutes + stoppage time)?
- `d7655d0b-d218-4ed4-a475-316e3f47b8ca` — **regulation / team_score_both_halves** — AF exact bet 111 (team_score_both_halves home Yes) — Will Spain score in both halves in regulation (90 minutes + stoppage time)?
- `1df0c901-8c87-4e78-9172-93a7adaeae3d` — **regulation / total_cards** — AF exact bet 80 (total_cards Over 3.5) — Will there be 4 or more total cards shown in regulation (90 minutes + stoppage time)?
- `16a5ab92-b3d1-49f9-8de3-f783daf07d02` — **regulation / total_offsides** — AF exact bet 164 (total_offsides Over 3.5) — Will there be 4 or more offside calls in regulation (90 minutes + stoppage time)?
- `6b27e4f8-63d1-4b77-985e-fd2b80f9014b` — **regulation / none** — simulator `goal_window:before_first_hydration:reg` — Will a goal be scored before the first hydration break?
- `aea8fb01-9548-4468-9ed5-32a29c075938` — **regulation / match_winner** — AF exact bet 1 (Spain win) — Will Spain win in regulation (90 minutes + stoppage time)?

## POR vs CRO — 2026-07-02T23:00:00.000Z

- `885cd64c-e545-4e0f-9c2e-0060e38f429f` — **regulation / player_goal_scorer** — AF exact bet 92 (player anytime scorer) — Will Cristiano Ronaldo (Portugal) score a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `68a19697-c475-42cf-9fec-78d2ccaca7d2` — **regulation / player_shots_on_target** — AF exact bet 242 (player SoT) — Will Bruno Fernandes (Portugal) have 2 or more shots on target in regulation (90 minutes + stoppage time)?
- `ddaa196f-e8ad-48d2-b888-087f4efd418e` — **regulation / player_score_or_assist** — simulator `player_score_or_assist:full:reg:player` — Will Luka Modrić (Croatia) score or assist a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `8fae87b5-69cf-439a-a0b1-395e95251d0c` — **regulation / total_goals** — AF exact bet 5 (total_goals Under 2.5) — Will the match have 2 or fewer total goals in regulation (90 minutes + stoppage time)?
- `0d8b5cbe-a662-49e3-9e0b-aec265151376` — **regulation / team_shots_on_target** — simulator `count:shots_on_target:team:full:>=:4:reg` — Will Croatia have 4 or more shots on target in regulation (90 minutes + stoppage time)?
- `76a42a42-5d35-4ac8-ac00-628268d82c04` — **regulation / total_cards** — AF exact bet 80 (total_cards Over 3.5) — Will there be 4 or more total cards shown in regulation (90 minutes + stoppage time)?
- `1f8cf0d6-8608-426b-a0f7-d0e04f8f3664` — **regulation / both_teams_card** — simulator `both_teams_card:reg` — Will both teams receive at least one card in regulation (90 minutes + stoppage time)?
- `15d199ba-bd1a-4003-82d0-4e098cb7c7b8` — **regulation / team_corners** — AF exact bet 57 (home team_corners Over 5.5) — Will Portugal have 6 or more corner kicks in regulation (90 minutes + stoppage time)?
- `ad4917d9-9d86-4cc4-9fb5-51dc8ea51b3a` — **regulation / match_draw** — AF exact bet 13 (draw 1H) — Will the match be tied at halftime?
- `39ee4924-7315-4f52-8aee-8d0cb0ad5867` — **regulation / team_shots_on_target** — simulator `count:shots_on_target:team:full:>=:6:reg` — Will Portugal have 6 or more shots on target in regulation (90 minutes + stoppage time)?
- `ac50d1ea-0872-4150-93d5-11db85dfdc47` — **regulation / player_shots_on_target** — AF exact bet 242 (player SoT) — Will Bernardo Silva (Portugal) have 1 or more shots on target in regulation (90 minutes + stoppage time)?
- `cef9a8f6-101f-4931-aebb-ef5288611fb9` — **regulation / none** — web-only; no exact provider contract or defensible simulator counter — Will a header goal be scored in regulation (90 minutes + stoppage time)?
- `d278a305-9c25-423e-9e22-30d4f757f45f` — **regulation / total_shots** — AF exact bet 211 (total_shots Over 19.5) — Will there be 20 or more total shots (on and off target) in regulation (90 minutes + stoppage time)?
- `b6fe115e-860f-4fda-9285-16a23c134d76` — **regulation / none** — simulator `penalty_or_red:reg` — Will a penalty kick be awarded OR a red card be shown in regulation (90 minutes + stoppage time)?
- `ff34f943-9aef-4920-9aff-0d2bfc39e21d` — **full_match / to_advance** — AF exact bet 61 (to_advance home) — Will Portugal advance to the Round of 16?

## SUI vs ALG — 2026-07-03T03:00:00.000Z

- `1ae2b074-fd8d-4159-a41f-3ad3602308cf` — **regulation / player_goal_scorer** — AF exact bet 92 (player anytime scorer) — Will Breel Embolo (Switzerland) score a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `66feaf0e-47f6-47a1-8845-41ef165f6fd0` — **regulation / player_shots_on_target** — AF exact bet 242 (player SoT) — Will Rubén Vargas (Switzerland) have 2 or more shots on target in regulation (90 minutes + stoppage time)?
- `89c325b2-8d27-4887-a147-6854e4aa427a` — **regulation / btts** — AF exact bet 8 (Both teams score) — Will both teams score in regulation (90 minutes + stoppage time)?
- `78cfdcd8-474c-4985-8094-33a611ce4539` — **regulation / player_goal_scorer** — AF exact bet 92 (player anytime scorer) — Will Amine Gouiri (Algeria) score a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `05c125ad-1189-4d80-8a7c-f9acfe1fcc80` — **regulation / player_score_or_assist** — simulator `player_score_or_assist:full:reg:player` — Will Riyad Mahrez (Algeria) score or assist a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `5e7cc8a1-6323-4c29-a788-2187f9afef22` — **regulation / total_goals** — AF exact bet 5 (total_goals Over 2.5) — Will the match have 3 or more total goals in regulation (90 minutes + stoppage time)?
- `495b7d4a-dc7e-47ba-afc6-0c1d0b422811` — **regulation / corners_compare** — AF exact bet 55 (corners_compare away) — Will Algeria have more corner kicks than Switzerland in regulation (90 minutes + stoppage time)?
- `1d8b4d3a-4d6e-4382-b06f-1ac7aee9f8c6` — **regulation / total_cards** — AF exact bet 80 (total_cards Over 4.5) — Will there be 5 or more total cards shown in regulation (90 minutes + stoppage time)?
- `88823172-d3e1-40fb-9885-b4b576b83149` — **regulation / none** — simulator `any_player_threshold:goals:>=:2:reg` — Will any player score 2 or more goals in regulation (90 minutes + stoppage time)?
- `9b2e143a-a8bf-4b7c-9564-06fa011459bf` — **regulation / team_shots_on_target** — simulator `count:shots_on_target:team:full:>=:5:reg` — Will Switzerland have 5 or more shots on target in regulation (90 minutes + stoppage time)?
- `63f01042-2244-4ffe-b50f-6c938f9d957c` — **regulation / match_draw** — AF exact bet 13 (draw 1H) — Will the match be tied at halftime?
- `72f2792d-e035-4065-bd37-a99d1d20c5f2` — **regulation / none** — simulator `penalty_or_red:reg` — Will a penalty kick be awarded OR a red card be shown in regulation (90 minutes + stoppage time)?
- `ccc9e313-254b-46c7-944c-4de8e3038e95` — **full_match / first_team_to_score** — AF regulation proxy bet 14 (home scores first) — Will Switzerland score the first goal of the match?
- `f30adbfb-9db8-4ef1-9428-ad64b27910fe` — **regulation / none** — simulator `goal_window:after_second_hydration:reg` — Will a goal be scored after the second hydration break in regulation (90 minutes + stoppage time)?
- `f0062b9e-2b5e-49d4-8ecd-77bc2dd99b7c` — **full_match / to_advance** — AF exact bet 61 (to_advance home) — Will Switzerland advance to the Round of 16?

## AUS vs EGY — 2026-07-03T18:00:00.000Z

- `50e7c1e9-5699-4af6-b248-e73b17a48557` — **regulation / player_goal_scorer** — AF exact bet 92 (player anytime scorer) — Will Mahmoud Trezeguet (Egypt) score a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `03bed111-ecf0-4983-b009-fb744af21280` — **regulation / match_winner** — AF exact bet 1 (Egypt win) — Will Egypt win in regulation (90 minutes + stoppage time)?
- `726d8675-40e8-4010-9d08-42a890f4f4b3` — **regulation / match_draw** — AF exact bet 13 (draw 1H) — Will the match be tied at halftime?
- `9ef7ba30-7ed3-4619-b22d-790d0e89c672` — **regulation / total_goals** — AF exact bet 5 (total_goals Under 2.5) — Will the match have 2 or fewer total goals in regulation (90 minutes + stoppage time)?
- `56dcefe8-b830-4e66-a23c-eba7c3525aee` — **regulation / player_shots_on_target** — AF exact bet 242 (player SoT) — Will Nestory Irankunda (Australia) have 2 or more shots on target in regulation (90 minutes + stoppage time)?
- `576eeb43-30a7-4032-8ccd-bf0e389cfcb0` — **regulation / both_teams_card** — simulator `both_teams_card:reg` — Will both teams receive at least one card in regulation (90 minutes + stoppage time)?
- `f0a24625-4cc0-44f2-a25a-35659d0c2b7e` — **regulation / total_cards** — AF exact bet 80 (total_cards Over 3.5) — Will there be 4 or more total cards shown in regulation (90 minutes + stoppage time)?
- `83418d99-16ff-4054-b688-0eeaeb6ad937` — **regulation / none** — simulator `penalty_or_red:reg` — Will a penalty kick be awarded OR a red card be shown in regulation (90 minutes + stoppage time)?
- `5a56e10d-51d9-44ea-8e1e-22a68e966f3c` — **regulation / team_shots_on_target** — simulator `count:shots_on_target:team:full:>=:5:reg` — Will Egypt have 5 or more shots on target in regulation (90 minutes + stoppage time)?
- `1302cc43-72aa-4013-815d-4cee35349ae2` — **regulation / total_corners** — AF exact bet 45 (total_corners Over 8.5) — Will there be 9 or more total corner kicks in regulation (90 minutes + stoppage time)?
- `c1bfe7e5-5fb8-48af-8e31-cf2d12b001e9` — **full_match / first_team_to_score** — AF regulation proxy bet 14 (away scores first) — Will Egypt score the first goal of the match?
- `98a7eed2-6074-4c63-be57-15da97bc4119` — **regulation / highest_scoring_half_2h** — AF exact bet 11 (2nd half outscores 1st) — Will the second half produce more goals than the first half in regulation (90 minutes + stoppage time)?
- `5b659dfa-fb37-4a8e-bb88-f7d0f99606b8` — **regulation / total_offsides** — AF exact bet 164 (total_offsides Over 2.5) — Will there be 3 or more offside calls in regulation (90 minutes + stoppage time)?
- `62b66a1e-1865-481c-b939-a1b1a2a37c97` — **regulation / none** — simulator `substitution_before_halftime:reg` — Will a substitution be made before halftime?
- `e8c8a1cc-e1f0-49d7-8382-bfe3432b0c8c` — **regulation / total_shots** — AF exact bet 211 (total_shots Over 19.5) — Will there be 20 or more total shots (on and off target) in regulation (90 minutes + stoppage time)?

## ARG vs CPV — 2026-07-03T22:00:00.000Z

- `2f91ea01-f0cf-4782-91a9-67929e81a7be` — **regulation / match_winner** — AF exact bet 1 (Argentina win) — Will Argentina win in regulation (90 minutes + stoppage time)?
- `9844bc2b-4fbd-4457-aca6-7457c3ce5f6d` — **regulation / player_shots_on_target** — AF exact bet 242 (player SoT) — Will Lionel Messi (Argentina) have 3 or more shots on target in regulation (90 minutes + stoppage time)?
- `ea296a71-54cd-4899-9172-9fbabac09b84` — **regulation / none** — simulator `goal_window:before_first_hydration:reg` — Will a goal be scored before the first hydration break?
- `9c6d6e52-16ff-42c1-b758-afb9943cd40b` — **regulation / player_goal_scorer** — AF exact bet 92 (player anytime scorer) — Will Lautaro Martínez (Argentina) score a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `a9d654cd-6cd4-4afc-b6b2-dd44493e9223` — **regulation / team_clean_sheet** — AF exact bet 27 (team_clean_sheet home Yes) — Will Argentina keep a clean sheet in regulation (90 minutes + stoppage time)?
- `aaa0e152-6833-4bb7-91e7-a7ea91f0a539` — **regulation / team_shots_on_target** — simulator `count:shots_on_target:team:full:>=:2:reg` — Will Cape Verde have 2 or more shots on target in regulation (90 minutes + stoppage time)?
- `6be8fa1d-9147-4ad8-ab28-64e5fbb643cf` — **regulation / team_total_goals** — AF exact bet 16 (home team_total_goals Over 2.5) — Will Argentina score 3 or more goals in regulation (90 minutes + stoppage time)?
- `60f6a76a-9f20-458f-b153-5ad9bf27b36c` — **regulation / team_score_both_halves** — AF exact bet 111 (team_score_both_halves home Yes) — Will Argentina score in both halves in regulation (90 minutes + stoppage time)?
- `69616caf-9b07-4d23-9282-2aca8c998480` — **regulation / team_shots_on_target** — simulator `count:shots_on_target:team:full:>=:8:reg` — Will Argentina have 8 or more shots on target in regulation (90 minutes + stoppage time)?
- `2c487b3c-4b7d-4e37-88c9-a773faa0ddee` — **regulation / player_score_or_assist** — simulator `player_score_or_assist:full:reg:player` — Will Julián Álvarez (Argentina) score or assist a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `28f248b8-10fc-45fc-8a4c-a35908c18d43` — **regulation / total_cards** — AF exact bet 80 (total_cards Over 2.5) — Will there be 3 or more total cards shown in regulation (90 minutes + stoppage time)?
- `1af93b86-8ea1-4030-b614-105dd07817ba` — **full_match / first_team_to_score** — AF regulation proxy bet 14 (home scores first) — Will Argentina score the first goal of the match?
- `acababe1-851f-433e-9d89-475c08f392d0` — **regulation / team_corners** — AF exact bet 57 (home team_corners Over 7.5) — Will Argentina have 8 or more corner kicks in regulation (90 minutes + stoppage time)?
- `76925538-dec6-4caa-b4d5-4cb1e8e56dfe` — **regulation / match_winner** — AF exact bet 13 (match_winner home 1H) — Will Argentina be ahead at halftime?
- `2c0ffaa7-880d-492e-81a9-fd41caf1499a` — **regulation / total_shots** — AF exact bet 211 (total_shots Over 21.5) — Will there be 22 or more total shots (on and off target) in regulation (90 minutes + stoppage time)?

## COL vs GHA — 2026-07-04T01:30:00.000Z

- `cccdfbf7-8ac2-4e44-a2a6-c416a5dbcf26` — **regulation / player_goal_scorer** — AF exact bet 92 (player anytime scorer) — Will Luis Díaz (Colombia) score a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `81b9daa9-e8cd-4bb6-b1d8-0a51a757293d` — **regulation / player_score_or_assist** — simulator `player_score_or_assist:full:reg:player` — Will James Rodríguez (Colombia) score or assist a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `8b39b1cb-5383-4050-8b9a-1a47251b1cf0` — **regulation / player_shots_on_target** — AF exact bet 242 (player SoT) — Will Antoine Semenyo (Ghana) have 2 or more shots on target in regulation (90 minutes + stoppage time)?
- `ae1f6d42-b88d-4511-92d2-28e3d1436b06` — **regulation / btts** — AF exact bet 8 (Both teams score) — Will both teams score in regulation (90 minutes + stoppage time)?
- `94e9a2aa-8af1-460e-ad7e-cbb6aa98258d` — **regulation / player_goal_scorer** — AF exact bet 92 (player anytime scorer) — Will Jordan Ayew (Ghana) score a goal (excluding own goals) in regulation (90 minutes + stoppage time)?
- `89c1eba5-e3a7-4b8b-9522-e2562824704f` — **regulation / team_shots_on_target** — simulator `count:shots_on_target:team:full:>=:7:reg` — Will Colombia have 7 or more shots on target in regulation (90 minutes + stoppage time)?
- `ea3308ff-eddc-4499-9cca-97650ae6b83e` — **full_match / first_team_to_score** — AF regulation proxy bet 14 (home scores first) — Will Colombia score the first goal of the match?
- `2e0e676c-0153-43d3-bd9c-8706469813fb` — **regulation / total_cards** — AF exact bet 80 (total_cards Over 3.5) — Will there be 4 or more total cards shown in regulation (90 minutes + stoppage time)?
- `b224fee8-4a45-4ce8-9156-de1fa31a14d0` — **regulation / match_winner** — AF exact bet 13 (match_winner home 1H) — Will Colombia be ahead at halftime?
- `5087567c-989c-43d8-83db-2cd9d4ae5159` — **regulation / total_shots** — AF exact bet 211 (total_shots Over 21.5) — Will there be 22 or more total shots (on and off target) in regulation (90 minutes + stoppage time)?
- `13b81973-ab63-4c9c-b993-fa878e4df2a8` — **regulation / team_corners** — AF exact bet 57 (home team_corners Over 5.5) — Will Colombia have 6 or more corner kicks in regulation (90 minutes + stoppage time)?
- `4bcae76c-d0b9-4d28-861a-2e6c6ad2311f` — **regulation / total_offsides** — AF exact bet 164 (total_offsides Over 2.5) — Will there be 3 or more offside calls in regulation (90 minutes + stoppage time)?
- `2b595d63-8732-4a43-8e03-9436ea414afe` — **regulation / total_goals** — AF exact bet 5 (total_goals Over 2.5) — Will the match have 3 or more total goals in regulation (90 minutes + stoppage time)?
- `02b77526-49b7-4819-b7c1-82d4cdd10862` — **regulation / none** — simulator `goal_window:before_first_hydration:reg` — Will a goal be scored before the first hydration break?
- `af49eb21-f513-4c43-ae24-882e2f887606` — **full_match / to_advance** — AF exact bet 61 (to_advance home) — Will Colombia advance to the Round of 16?
