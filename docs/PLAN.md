# R6 Operator Tracker — Build Plan

A pipeline + app that tracks **operator bans, picks-by-bomb-site, and win rates** for Rainbow Six Siege esports, scoped per competition, sourced from siege.gg. Replaces the manual Claude-for-Chrome reading that produced the original Google Sheet / workbook.

Target branch: `develop` (repo has `main` + `develop` + `origin`).

---

## What the data model really is

The workbook is not a flat table — it's a set of linked tabs:

- **Map Picks** — per map: rounds played, defender win rate %, a per-site "standout/deviation" metric, plus a global Attack/Defense ban-priority list.
- **`<Map> Att` / `<Map> Def`** (Lair, Bank, Oregon, Club House, Chalet, Border, Kafe, Consulate, Nighthaven Labs, Fortress, Skyscraper…) — the core fact tables. Per operator: Times Banned, Value (rounds × bans), and appearance counts broken out by bomb site (sites differ per map; e.g. Lair = Master/R6 Room, Lab/Lab Support, Bunks/Briefing).
- **Per-match tabs** (e.g. `CH G2-TWMI 11May26`) — one match's attack/defense operator appearances + bans by site.
- **Match URLs** — Match ID, matchup, maps played, and the `siege.gg/matches/...` link (`mjr-intl` = BLAST Major SLC 2026).

So this is fundamentally an **operator ban + pick-by-site + win-rate tracker, scoped to one competition.** The collector's job is to walk a competition's match list, open each match, and extract per-map/per-side operator bans and site appearances.

---

## Ground truth: what was validated live vs. assumed

Captured from a real extraction of competition 100 (BLAST Major SLC 2026, 50 matches / 86 games) this session.

### Validated (observed firsthand)

- **Match data lives in the page payload**, not a separate API call. Path: `useNuxtApp().payload.data["match:<id>"]` →
  - `match.games[]` (one per map): `.map.name`, `.operator_bans[]`, `.rounds[]`
  - `operator_bans[]`: `operator{name, side}`, `is_league_wide`, `ban_round`, `stats_roster_id`
  - `rounds[]`: `site.name`, `atk_roster_id`, `def_roster_id`, `def_win`, `is_tie`, `win_condition`, `events[]`
  - `events[]`: `{type: multikill | plant | clutch | disable, html}` — operator from the `<img alt>` in `html`, player from the text.
- **No data XHR**: expanding a game fires only operator-icon image requests; the round/ban/site data is server-rendered into `<script id="__NUXT_DATA__">` (~227 KB, devalue-serialized array) and hydrated into `useNuxtApp().payload`.
- **Match-list page is client-rendered**: `siege.gg/matches?tab=results&competitions=<id>&page=N` did *not* contain the list in its SSR payload; links were read from the rendered DOM. Competition 100 = 50 matches over 3 pages.
- **Counting that reproduced the sheet exactly**:
  - Side = DEF if acting player's roster == `round.def_roster_id`, else ATK.
  - Appearance = distinct `(operator, side)` per round, **de-duplicated across all event types** (an operator's clutch is not double-counted if they already appeared that round). Reproduced **23** events for Club House match 3536.
  - Win rate = defender round-win % = `Σ def_win / Σ rounds`. Reproduced **Club House 146 rounds / 47%**, matching the existing sheet.
  - Bans: "Times Banned" = number of ban entries per game; rotating bans give multiple entries (`ban_round ∈ {1,4,7}`). `is_league_wide` present but **false for all** of this event. Ban "Value" = `rounds × times_banned` (single game = the sheet's "9 × bans"; aggregated across a map = `Σ(game_rounds × times_banned_in_game)`).
- **Player-name gotcha (real bug)**: events render the player's **stylized_name**, which often differs from `ign` (e.g. ign `MOONL1GHT` → `MoonL1ght`; ign `BLAZTFUL` → `JJBlazt`). Resolve via a name map from `{ign, stylized_name, name}` (uppercase, strip non-alphanumerics) and **exclude operator-name tokens** to avoid collisions ("Solid Snake" the operator vs. a player named "Snake"). ~16 events/match dropped before this fix.
- **Operator canonicalization (confirmed)**: Montagne→Monty, Dokkaebi→Dokk, Solid Snake→Snake.
- **Excluded** 2 "Unknown" / 0-round games (forfeits/unplayed).
- **Site-label differences**: siege.gg names differ from the old sheet's abbreviations (Fortress "Commander's/Bathroom" vs "Command/Bath"; Lair "Master/R6 Room" vs "Master/R6").
- **Two grains are required**: operator appearances and round/win-rate counts cannot share one table (operators don't appear every round). This was learned the hard way — a first pass storing only operator tallies could not produce win rate and required a second full pass capturing per-round `site` + `def_win`.

### Assumed / NOT yet validated (spike before relying on)

1. **Browser-free match parsing** via `requests` + a devalue parser for `__NUXT_DATA__`. The blob exists and parses as an array, but the object graph was only read via the *hydrated* `useNuxtApp().payload` in a live browser — never reconstructed from raw devalue, never fetched with plain `requests`.
2. **`/competitions` render mode**: never visited. The *matches* list was client-rendered, so do not assume `/competitions` is plain server-rendered without checking.
3. ~~**The "standout/deviation" metric** (Map Picks column D): never built or defined; exact formula unknown.~~ **RESOLVED** (read from the live formulas) — see "Derived columns" below.
4. **Operator catalog completeness**: confirmed mappings are partial. Accented forms (Nøkk, Jäger, Tubarão…) actually arrived *un-accented* in the data, and a real un-handled case appeared — "Iq" rendered lowercase (should be "IQ"). Build the catalog from observed alt-text, not an assumed list.
5. **Official SiegeGG API** (dev.siege.gg) exists but needs an approved app key; round-level data likely a paid tier. Robust "tier 0" source if obtainable; not used here.

---

## Derived columns (read from the live Google Sheet formulas)

The sheet carries three derived column-sets beyond the base operator tables. All
are now reproduced by `build_workbook.py`:

1. **Map Picks col D — round-weighted win-rate deviation.** Per site:
   `=(site_WR − map_WR) × 100 × (site_rounds / map_rounds)`. Verified exact
   against the cached values on 31/33 sites.
2. **Per-map priority block** (one column per site, right of the appearance
   block) = `Value + per-site appearances`. The side-label row repeats site
   names over it; the Operator row references the Map Picks deviation
   (`='Map Picks'!D…`). We write the computed values, not the cross-sheet refs.
3. **Map Picks Atk/Def lists** (cols G/I) are **static, analyst-curated text** —
   NOT formulas, and reproduced by no single sort of the data (checked value,
   appearances, bans, and combinations). Embedded verbatim for competition 100
   (`ATK_LIST`/`DEF_LIST`); a generalised pipeline must take these as input.

**Source-sheet bug found & corrected:** Lair `D62`/`D63` (Lab/Lab Support,
Bunks/Briefing) reference Fortress's map row (`$C$55`/`$B$55`) instead of Lair's
(`$C$60`/`$B$60`), so the sheet shows −292.5 / −300. The correct deviations are
−62.07 / −110.34, which the pipeline emits. Flag for the sheet owner.

## Normalized schema (the key engineering move)

Three tidy fact tables (not one), because bans and win rate live at different grains:

- **operator_picks**: `competition_id | match_id | map | side(ATK/DEF) | operator | site | appearances`
- **bans**: `competition_id | match_id | map | side | operator | times_banned | ban_value | ban_round | is_league_wide`  *(bans have no site dimension)*
- **site_rounds**: `competition_id | match_id | map | site | rounds_played | def_wins`  *(win rate + standout computed from this)*

Plus facet/lineage tables:

- **competitions**: `id, name, series, tier, type, env, region, start_date, end_date, status`
- **matches**: `match_id, competition_id, teams, score, date, url, maps_played`

Win rate, ban priority, and the standout metric are **computed aggregates**, not stored columns.

Reference data:

- **map_sites.json**: map → ordered bomb sites, keeping both raw (siege.gg) and display labels.
- **operator_catalog.json**: canonical operator names + side; used for name canonicalization and as a test oracle (unknown operator → fail the job).

---

## Proposed repo layout

```
r6-operator-tracker/
├─ app.py                      # Streamlit app (Stage 4)
├─ collector/
│  ├─ scrape_competitions.py   # /competitions → competitions.json
│  ├─ scrape_matches.py        # competition → match list → per-match data
│  ├─ devalue.py               # __NUXT_DATA__ parser (if browser-free path works)
│  ├─ normalize.py             # raw match data → tidy fact tables
│  └─ requirements.txt
├─ data/
│  ├─ competitions.json
│  ├─ matches.json
│  ├─ operator_picks.parquet
│  ├─ bans.parquet
│  ├─ site_rounds.parquet
│  └─ workbooks/<competition>.xlsx   # generated artifact per competition
├─ reference/
│  ├─ map_sites.json
│  └─ operator_catalog.json
├─ etl/
│  └─ build_workbook.py        # tidy data → xlsx (openpyxl), the delivered format
├─ tests/
│  └─ test_normalize.py        # schema + sanity (non-empty, known operators, win-rate sane)
├─ .github/workflows/
│  └─ scrape.yml               # daily cron → scrape → auto-commit to develop
├─ docs/PLAN.md                # this file
├─ requirements.txt
├─ README.md   LICENSE   .gitignore
```

---

## Staged delivery

### Stage 1 — Browser-free pipeline that rebuilds the delivered workbook

**Goal:** a standalone Python pipeline that reproduces the exact 11-tab xlsx for competition 100, with zero Claude-for-Chrome / manual browser involvement.

**Layout (kept deliberately flat — Stage 1 only has to rebuild one competition and prove it matches):**

```
r6-operator-tracker/
├─ build_workbook.py     # the whole pipeline: fetch comp-100 → normalize → write the xlsx
├─ requirements.txt
├─ data/
│  └─ Siege Stats - Competition 100.xlsx   # regenerated workbook (the deliverable)
├─ Siege Stats - BLAST Major SLC 2026.xlsx # known-good oracle (already in repo)
└─ README.md   LICENSE   .gitignore   docs/PLAN.md
```

What's intentionally collapsed (it returns in later stages):
- Tidy tables (`operator_picks` / `bans` / `site_rounds`) are **in-memory DataFrames** inside the script, not persisted Parquet — they get written out in Stage 2 when the app needs to read them. The two-grain design still holds.
- Operator catalog and map→sites maps live as **dicts at the top of the script**, not `reference/*.json` — extracted only when a second competition forces reuse.
- `devalue.py` and match-enumeration logic stay **folded into `build_workbook.py`** — split only when Stage 2 generalizes them.
- No `tests/` package — a `--verify` flag (or final assert block) that diffs the regenerated workbook against the oracle is enough.

**Steps:**
1. Enumerate matches for competition 100 (50 matches / 3 result pages).
2. Fetch + parse each match payload (`games[] → operator_bans[] / rounds[] / events[]`).
3. Normalize using the validated rules (side, player resolution, operator canonicalization, per-round dedup, bans with `ban_round`, drop 0-round games).
4. Aggregate + build the xlsx (openpyxl): Map Picks (rounds + def win rate), Match URLs, 9 per-map Def/Att tabs → `data/Siege Stats - Competition 100.xlsx`.

**Acceptance test (regression oracle):** output must match the already-delivered workbook — Club House 146/47, the per-map Def/Att counts, Match URLs (50 rows). This makes correctness verifiable, not just plausible.

**Spikes to resolve inside Stage 1:**
- *Match detail browser-free:* does `requests` + a devalue parser read `__NUXT_DATA__`? If yes → no Playwright. If no → headless Playwright for detail only.
- *Match enumeration:* find a list endpoint, or use one headless render pass for enumeration.

**Deliverable:** one command produces the verified `data/Siege Stats - Competition 100.xlsx` for competition 100.

### Stage 2 — Generalize to many competitions + archive to git

**Goal:** drive the same pipeline from `/competitions`, producing data + a workbook per competition.

**Steps:**
1. Scrape `/competitions` → `competitions.json`. *First confirm its render mode* (don't assume server-rendered).
2. Parameterize the collector by `competition_id`.
3. Persist per competition: tidy tables (source of truth) + generated xlsx artifact.

**Archiving to git — yes, fully possible:**
- Commit the **tidy CSV/Parquet as the source of truth** (diffable — commits show what changed).
- Commit the **xlsx as a generated artifact** alongside it (or push to a GitHub Release if you prefer lean history). At ~30 KB, daily commits are negligible (~10 MB/yr).

### Stage 3 — Daily scheduled auto-update (hands-off, no manual `git add`)

**Goal:** a cron GitHub Action that refreshes ongoing competitions and writes results back to `develop` automatically.

**How the auto-commit works:** the workflow checks out `develop`, runs the pipeline, then commits + pushes changed files back as the `github-actions[bot]` user. Auth is the built-in `GITHUB_TOKEN` with `permissions: contents: write` — no PAT needed for same-repo pushes. No human runs `git add`.

**Steps:**
1. Determine "ongoing" competitions from date ranges/status; only re-scrape active ones.
2. Incremental fetch — skip match IDs already in `matches.json`; pull only new/updated matches.
3. Regenerate affected tidy tables + xlsx; run Stage-1 sanity tests as a **gate** so a broken scrape never overwrites good data.
4. Auto-commit `data/` back to `develop`.

**Decisions / caveats:**
- **Direct push vs. auto-PR** depends on branch protection on `develop`. If `develop` is protected (requires PR/review), a direct bot push is rejected — either allow the bot to bypass, or have the job **open a daily PR** (safer; you see the data diff before merge). If unprotected, direct push is simplest.
- **Skip empty commits** (`git diff --quiet`) so quiet days add no noise.
- **No trigger loop**: cron (`schedule`) doesn't re-trigger on the bot's push. (Only relevant if also triggering `on: push` — then add `[skip ci]`/paths filter.)
- Runner runs the scraper headless (fine for `requests`; installs a headless browser if Playwright is needed). GitHub runners can reach siege.gg.

### Stage 4 — Streamlit app with multi-competition filters

**Goal:** read the tidy tables and let users multi-select competitions to view aggregated numbers.

**Steps:**
1. Load `operator_picks` / `bans` / `site_rounds` + `competitions`.
2. Sidebar: **multi-select Competition** (primary), then Map, Side, Site, Operator.
3. Views: aggregated ban priority, pick rate by site, map win rates, per-match drill-down linking back to the siege.gg page. Aggregation happens live across selected competitions (the whole reason for the tidy schema — filter instead of stitching workbooks).

**Dependency:** needs the tidy tables from Stages 1–2. The xlsx is for humans/archival; the app reads the data tables. Deployable on Streamlit Community Cloud (one private app on the free tier).

---

## Dependency chain & open items

Stage 1 (prove correctness vs. known-good artifact) → Stage 2 (generalize + storage) → Stage 3 (automate) → Stage 4 (consume).

**Still genuinely unproven** (resolve as part of the relevant stage): browser-free `__NUXT_DATA__` parsing, `/competitions` render mode, the standout-metric formula, the operator catalog, and whether `develop` is branch-protected (push vs. PR).
