# R6 Operator Tracker

A data pipeline and Streamlit dashboard for tracking Rainbow Six Siege esports operator bans, picks-by-bomb-site, and map win rates — sourced from [siege.gg](https://siege.gg), supporting any competition.

---

## Features

- **Multi-competition** — tracks any siege.gg competition; select one or multiple in the dashboard
- **Incremental scraping** — nightly job detects and fetches only new matches; existing data is never re-fetched
- **Offline cache** — decoded match payloads saved per competition; rebuilds reuse cache, no network required
- **SQLite storage** — `siege.db` stores picks, bans, and rounds; atomic transactions prevent partial writes
- **Downloadable workbooks** — per-competition `.xlsx` with Map Picks, per-map operator sheets, and Raw Data tab
- **Streamlit dashboard** — multi-select competition filter, cross-competition aggregation, operator meta with priority scoring, map/site win rates

---

## Project layout

```
r6-operator-tracker/
├─ run.py                 # CLI: sync-catalog / sync-matches / fetch / build / refresh
├─ scraper.py             # HTTP layer: catalog + match list + payload fetch/cache
├─ pipeline.py            # normalize_match + atomic SQLite store
├─ build_workbook.py      # siege.db → per-competition workbook.xlsx
├─ app.py                 # Streamlit dashboard
├─ requirements.txt
├─ reference/
│  ├─ operators.json      # Complete operator roster with side (Attack/Defense) — game-wide
│  └─ maps.json           # Map order, bomb sites, Map Picks layout — game-wide
└─ data/
   ├─ competitions.json   # Single source of truth: catalog + metadata + analyst lists + match seeds
   ├─ siege.db            # SQLite: picks, bans, rounds, processed_matches
   └─ competitions/
      └─ {id}/
         ├─ matches.json  # Match list (permanent for completed comps, live-updated for ongoing)
         ├─ raw/
         │  └─ {match_id}.json   # Decoded payload cache (avoids re-fetching)
         └─ workbook.xlsx        # Generated Excel artifact (download only)
```

**No `config.json`** — all competition metadata (`name`, `region`, `dates`, `status`, `atk_list`, `def_list`) lives in `competitions.json`.

---

## Quick start

### 1. Install dependencies

```bash
pip3 install -r requirements.txt
```

### 2. Fetch and process a competition

```bash
python3 run.py refresh --comp 100
```

### 3. Launch the dashboard

```bash
streamlit run app.py
```

---

## CLI reference

```
python3 run.py sync-catalog
    Fetch siege.gg/competitions → update data/competitions.json.
    Extracts competition IDs and names from the page HTML.

python3 run.py sync-matches [--comp N | --active | --all]
    Update the match list for one or more competitions.
    Completed competitions use stored matches.json (client-rendered pages
    return wrong data). Ongoing competitions scrape live pages.

python3 run.py fetch [--comp N | --active | --all]
    Sync catalog + match list, then fetch + normalize new matches → siege.db.
    Skips matches already in processed_matches. Caches payloads in raw/.

python3 run.py build [--comp N | --all]
    Generate data/competitions/{id}/workbook.xlsx from siege.db.
    Always runs when --comp is specified (even if no new matches).

python3 run.py refresh [--comp N | --active | --all]
    sync-catalog + sync-matches + fetch + build in sequence.
    With --comp: always builds workbook. With --active/--all: only builds if new data.

Flags:
  --rebuild   Clear DB data for this competition and re-process all matches.
              Does NOT delete raw/ cache — reuses downloaded payloads.
```

### Nightly job

```bash
python3 run.py refresh --active
```

Processes only competitions with `"status": "ongoing"` in `data/competitions.json`.

---

## Adding a new competition

```bash
python3 run.py refresh --comp 101
```

For ongoing competitions the match list is discovered automatically. To add analyst-curated ban-priority lists, edit `data/competitions.json` and add `atk_list`/`def_list` to the competition's entry:

```json
{
  "id": 101,
  "name": "Liga START 2026",
  "status": "ongoing",
  "atk_list": ["Monty", "Grim", "Ace"],
  "def_list": ["Azami", "Kaid", "Mira"]
}
```

---

## Completed vs ongoing competitions

| Aspect | Completed | Ongoing |
|---|---|---|
| Match list source | `matches.json` (permanent, never re-scraped) | Live scrape of `/matches?competitions={id}` |
| Auto-restore if missing | From `competitions.json["matches"]` field | From live scrape |
| Match list write-back | Never — file is read-only | Written when new matches found |
| `--rebuild` behaviour | Re-processes stored list from DB-cleared state | Same |

Competition 100 (BLAST Major SLC 2026) is completed. Its 50 match IDs are embedded in `competitions.json["matches"]` and auto-restore `matches.json` if it is ever deleted.

---

## Data pipeline

```
siege.gg/competitions  →  sync_catalog()     →  data/competitions.json
competitions.json      →  sync_matches()     →  data/competitions/{id}/matches.json
siege.gg/matches/{id}  →  fetch_payload()    →  data/competitions/{id}/raw/{id}.json (cache)
raw payload            →  normalize_match()  →  picks / bans / rounds records
records                →  store()            →  data/siege.db (atomic transaction)
siege.db               →  build_workbook()   →  data/competitions/{id}/workbook.xlsx
```

### SQLite schema

| Table | Key columns |
|---|---|
| `picks` | comp_id, match_id, date, map, round, side, operator, site |
| `bans` | comp_id, match_id, map, side, operator, game_rounds |
| `rounds` | comp_id, match_id, map, site, def_win |
| `processed_matches` | match_id (PK), comp_id, processed_at |

---

## Competitions catalog

| ID | Competition | Region | Dates | Status |
|----|-------------|--------|-------|--------|
| 92 | Six Invitational 2026 | International | Feb 2–15 | Completed |
| 93–95 | Challenger Series (EU/NA/SA) | Regional | Mar 5–17 | Completed |
| 96–99 | Kickoff Tournaments | EU/MENA, SA, NA, Asia | Mar–Apr | Completed |
| 100 | BLAST Major Salt Lake City 2026 | International | May 8–17 | Completed |
| 101 | Liga START 2026 | LATAM | May 5–Aug 2 | Ongoing |
| 102–104 | APAC Kickoffs | APAC, ANZ | Mar–Apr | Completed |
| 105–111 | Regional League Stage 1 | All regions | Jun–Jul | Upcoming |

Run `python3 run.py sync-catalog` to refresh the full list from siege.gg.

---

## Reference data

- `reference/operators.json` — complete operator roster (77 operators) with canonical names and Attack/Defense side
- `reference/maps.json` — all 11 competitive maps with bomb sites and Map Picks display order

These are game-wide constants that do not change per competition.

---

## What is safe to delete

| File / folder | Safe to delete? | Recoverable? |
|---|---|---|
| `data/siege.db` | Yes | Re-run `python3 run.py refresh --comp N` |
| `data/competitions/{id}/raw/` | Yes | Re-fetched from siege.gg on next run |
| `data/competitions/{id}/workbook.xlsx` | Yes | Re-run `python3 run.py build --comp N` |
| `data/competitions/{id}/matches.json` (completed) | **No** | Auto-restored from `competitions.json["matches"]` if present |
| `data/competitions/{id}/matches.json` (ongoing) | Yes | Re-scraped from siege.gg |
| `data/competitions.json` | Yes | Re-run `python3 run.py sync-catalog` |
| `reference/operators.json` | **No** | Manual — game-wide operator roster |
| `reference/maps.json` | **No** | Manual — game-wide map data |

---

## Requirements

- Python 3.10+
- `requests`, `openpyxl`, `streamlit`, `pandas`
