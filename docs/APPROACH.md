# R6 Operator Tracker — Engineering Approach

Technical findings, design decisions, and known behaviours discovered during development.

---

## Repository layout

```
run.py              CLI orchestrator (sync-catalog / fetch / build / refresh)
scraper.py          HTTP layer: competition catalog, match list, payload cache
pipeline.py         Normalize match payload → SQLite (picks / bans / rounds)
build_workbook.py   SQLite → per-competition workbook.xlsx
app.py              Streamlit dashboard
reference/
  operators.json    Complete operator roster with Attack/Defense side (game-wide)
  maps.json         Map order, bomb sites, Map Picks layout (game-wide)
data/
  competitions.json Single source of truth: catalog + metadata + atk/def lists + match seeds
  siege.db          SQLite: picks, bans, rounds, processed_matches
  competitions/{id}/
    matches.json    Match list (permanent for completed, live-updated for ongoing)
    raw/{id}.json   Decoded payload cache (avoids re-fetching)
    workbook.xlsx   Generated Excel artifact (download only)
```

`config.json` was removed. All competition-specific data (`name`, `region`, `dates`, `status`, `atk_list`, `def_list`, `matches`) now lives in `competitions.json`.

---

## siege.gg scraping findings

### Page rendering modes

siege.gg is a Nuxt.js (Vue SSR) application. Different pages have different rendering behaviours:

| Page | Rendering | Scraping approach |
|------|-----------|-------------------|
| `/competitions` | Server-rendered | `requests.get()` works — competition IDs and names in HTML |
| `/matches?tab=results&competitions={id}` | **Client-rendered for completed competitions** | Returns wrong/unrelated data without JS; only reliable for ongoing competitions |
| `/matches/{id}-{slug}` | Server-rendered | `requests.get()` works — full match payload in `__NUXT_DATA__` |

**Critical finding:** The match results listing page is client-rendered for completed competitions. Without JavaScript execution, the server returns the default (most recent ongoing competition's matches). For competition 100, this returned Liga START 2026 (competition 101) matches.

### Competition name extraction

The `/competitions` page HTML changed structure during development. The original regex `href="/competitions/(\d+)"[^>]*>\s*([^<]+)` no longer captures names. Current approach matches competition cards:

```python
re.findall(r'href="/competitions/(\d+)-[^"]*"[^>]*>[\s\S]{1,500}?alt="([^"]+)"', html)
```

Each competition card has `href="/competitions/{id}-{slug}"` and an `<img alt="{display name}">` inside it.

### `__NUXT_DATA__` payload

Match detail pages embed all data in a `<script id="__NUXT_DATA__">` tag as a flat JSON array in the [devalue](https://github.com/Rich-Harris/devalue) serialisation format:
- Flattens an object graph into an index array; every value is referenced by index
- Negative integer sentinels: `null` (-1/-2), `NaN` (-3), `Infinity` (-4/-5), `-0` (-6)
- Tagged arrays for special types: `["Set", ...]`, `["Map", ...]`, `["Date", value]`
- Nuxt reducers: `["ShallowReactive", ref]`, `["Ref", ref]`, etc.

`_devalue_unflatten()` in `scraper.py` reconstructs the full object graph. Circular references are guarded with a `seen` set of node IDs.

### Match object location

`_find_match_object()` walks the hydrated payload looking for a dict with a `games` list where each game has a `map` key. Match object structure:
- `games[].map.name` — map played
- `games[].operator_bans[]` — ban data per game
- `games[].rounds[].site.name` — bomb site per round
- `games[].rounds[].def_win` — defender win flag
- `games[].rounds[].events[].html` — event HTML fragments

### Operator side detection

**Failed approach:** Side from image `src` URL (`/operators/attack/hibana.png`). Worked for Attack operators but produced 0 Defense picks — defense operator images use a different path structure that doesn't match the regex.

**Working approach:** Look up canonical name in `OPERATOR_SIDE` dict (`reference/operators.json`). Every operator is permanently Attack or Defense. Immune to URL structure changes.

**Operator canonicalisation:** Strip accents (NFKD → ASCII), title-case. Special cases in `OPERATOR_CANON`: `MONTAGNE` → `Monty`, `DOKKAEBI` → `Dokk`, `SOLID SNAKE`/`SNAKE` → `Snake`.

### Events and pick counting

Events (`games[].rounds[].events[]`) are **highlight plays only** (multikill, plant, clutch, disable), not every kill. Operators who play normally without triggering a highlight event do not appear in pick counts — consistent with the oracle.

Pick counting rules:
- **Deduplication**: one appearance per `(operator, side)` per round across all events
- **Site attribution**: bomb site from `round.site.name` attached to every pick in that round
- **Dropped rounds**: `def_win = None` or `is_tie = True` → excluded from win-rate counts
- **Dropped games**: no `map.name` or 0 rounds (forfeits) → skipped entirely

---

## Match list contamination (Liga START in competition 100)

### Root cause

`siege.gg/matches?tab=results&competitions=100` is client-rendered for the completed BLAST Major. `requests.get()` returns Liga START 2026 matches instead. These contaminated competition 100's `matches.json` and `siege.db` multiple times during development.

### Fix history

| Attempt | Approach | Why it failed |
|---------|----------|---------------|
| 0% overlap guard | Skip merge if `set(live) & set(stored) == {}` | siege.gg HTML includes 1–5 BLAST Major links in navigation/sidebar → tiny false overlap, Liga START IDs still merged |
| 50% overlap threshold | Skip merge if `overlap / len(stored) < 0.5` | When `stored` was empty (after a wipe), 0/0 fell through to "trust live" → all Liga START accepted |
| Current: status-based skip | If `status == "completed"` AND `stored` exists → skip live scrape entirely | Only reliable fix — completed competition match lists never change |

### Current logic (`sync_matches`)

```
stored exists + status == completed   → use stored, skip all network requests
stored missing + status == completed  → auto-seed from competitions.json["matches"]
stored exists + status == ongoing     → scrape live, apply 50% overlap guard before merging
stored missing + status == ongoing    → scrape live, use result directly
```

**matches.json write-back rule:** Never written for completed competitions. Only written for ongoing competitions when live adds new IDs. Prevents contamination from ever being persisted to disk.

### Competition 100 match list durability

The 50 BLAST Major match IDs are stored in `competitions.json` under `"matches"`. If `matches.json` is deleted, `sync_matches` auto-restores it from this field — no manual seeding needed. `competitions.json` survives standard cleanup operations because it is not inside `data/competitions/{id}/`.

---

## Incremental scraping

### Sentinel: `processed_matches` table

`processed_matches` is the authoritative record of what has been successfully normalized and stored. A match is "done" only when it appears in this table — not based on `matches.json` or `raw/` presence.

- Interrupted runs are safe: each match is stored in a single atomic transaction
- Re-running always resumes from exactly where it left off
- `matches.json` is a change-detection file, not a processing record

### Raw payload cache

Decoded match payloads saved to `data/competitions/{id}/raw/{match_id}.json`. On re-run:
- Cache hit → loaded from disk, no HTTP
- Cache miss → fetched from siege.gg, saved, then processed

`--rebuild` clears `picks`, `bans`, `rounds`, and `processed_matches` for a competition but does **not** delete raw cache. Normalization is repeated without re-downloading.

### `--rebuild` flag

Clears DB data, then re-processes all matches from the stored list using cached payloads. Does not replace `matches.json` with the live list — stored is always the authority.

---

## competitions.json — single source of truth

All competition data is in one file. Fields per entry:

```json
{
  "id": 100,
  "name": "BLAST Major Salt Lake City 2026",
  "region": "International",
  "dates": "May 8 – 17",
  "type": "LAN",
  "status": "completed",
  "atk_list": ["Monty", "Grim", ...],
  "def_list": ["Azami", "Kaid", ...],
  "matches": { "3474": "mjr-intl-los-vs-wolves-esports", ... }
}
```

`atk_list` / `def_list` are analyst-curated ban-priority lists — not derivable from data. `matches` is only set for completed competitions whose results page is client-rendered.

`sync_catalog()` always updates `name` from live HTML but never overwrites `atk_list`, `def_list`, `matches`, or `status` if already set.

---

## SQLite design

### Why not CSV

| Concern | CSV | SQLite |
|---------|-----|--------|
| Partial write safety | No | Yes (atomic transactions) |
| Deduplication | Full scan required | `INSERT OR IGNORE` on PK |
| Per-competition query | Scan entire file | Indexed `WHERE comp_id = ?` |
| Cross-competition aggregation | Load all files + pandas merge | Single SQL query |
| Incremental sentinel | Fragile (infer from contents) | `processed_matches` table |

### Schema

```sql
picks    (comp_id, match_id, date, map, round, side, operator, site)
bans     (comp_id, match_id, map, side, operator, game_rounds)
rounds   (comp_id, match_id, map, site, def_win)
processed_matches (match_id PK, comp_id, processed_at)

INDEX idx_picks_comp  ON picks(comp_id)
INDEX idx_bans_comp   ON bans(comp_id)
INDEX idx_rounds_comp ON rounds(comp_id)
```

`PRAGMA journal_mode=WAL` — allows concurrent reads (Streamlit app) while a refresh is writing.

---

## Workbook format (oracle validation)

The oracle `Siege Stats - BLAST Major SLC 2026.xlsx` was hand-built and revealed:

- **Ban Value** = `Σ(game_rounds × times_banned_in_game)` — not a simple count
- **Priority columns** (G-I in per-map sheets) = `Value + site_appearances` per site. Formula in oracle (`=$C5+D5`); generated as static values
- **Site deviation** (Map Picks col D) = `(site_WR - map_WR) × 100 × (site_rounds / map_rounds)`. Oracle has a drag error on Lair which the pipeline does not reproduce
- **Conditional formatting** on priority columns: red (`EA4335`) → yellow (`FFFF00`) → green (`34A853`), percentile 0–50–100
- **atk_list / def_list** (Map Picks cols G/I): analyst-curated — not derivable from any sort of the data

---

## Competition catalog (as of 2026-05-31)

| ID | Name | Region | Dates | Status |
|----|------|--------|-------|--------|
| 92 | Six Invitational 2026 | International | Feb 2–15 | Completed |
| 93–95 | Challenger Series (EU/NA/SA) | Regional | Mar 5–17 | Completed |
| 96–99 | Kickoff Tournaments | Regional | Mar–Apr | Completed |
| 100 | BLAST Major Salt Lake City 2026 | International | May 8–17 | Completed |
| 101 | Liga START 2026 | LATAM | May 5–Aug 2 | Ongoing |
| 102–104 | APAC Kickoffs | Regional | Mar–Apr | Completed |
| 105–111 | Regional League Stage 1 | All regions | Jun–Jul | Upcoming |

---

## Known limitations

1. **Client-rendered match lists**: Completed competition results pages require JavaScript. `requests.get()` returns wrong data. Match lists must come from `competitions.json["matches"]` (seeded once per completed competition).

2. **Sparse event coverage**: Picks only capture operators who appear in highlight events. Operators who play normally without triggering a highlight are not counted.

3. **Date field**: Extracted from `match.date`, `match.scheduled_at`, or `match.start_time` in order. Blank if none present.

4. **Team name canonicalisation**: Derived from slug (`slug.replace("-", " ").title()`). Less common team names may differ from official spellings.
