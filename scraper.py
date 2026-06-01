"""
scraper.py — siege.gg HTTP layer.

Three public functions:
  sync_catalog()                  → updates data/competitions.json
  sync_matches(comp_id, db)       → updates data/competitions/{id}/matches.json,
                                    returns list of new match IDs not yet in DB
  fetch_payload(match_id, slug, comp_id) → decoded match object (cached in raw/)
"""
import json
import os
import re

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

_HEADERS = {"User-Agent": "r6-operator-tracker/2.0 (+https://github.com)"}
_SESSION = requests.Session()
_SESSION.headers.update(_HEADERS)


# ── Catalog ──────────────────────────────────────────────────────────────────

def sync_catalog():
    """Fetch siege.gg/competitions and merge into data/competitions.json."""
    html = _SESSION.get("https://siege.gg/competitions", timeout=30).text

    # Each competition card has href="/competitions/{id}-{slug}" and
    # an <img alt="{display name}"> inside it. Match both in one pass.
    found = {}
    for cid, name in re.findall(
        r'href="/competitions/(\d+)-[^"]*"[^>]*>[\s\S]{1,500}?alt="([^"]+)"', html
    ):
        found[int(cid)] = name.strip()

    # Fallback: pick up any IDs missed by the pattern above (no name known)
    for cid in re.findall(r'/competitions/(\d+)', html):
        if int(cid) not in found:
            found[int(cid)] = None

    catalog_path = os.path.join(DATA, "competitions.json")
    existing = {}
    if os.path.exists(catalog_path):
        for entry in json.load(open(catalog_path)):
            existing[entry["id"]] = entry

    new_count = 0
    for cid, live_name in found.items():
        if cid not in existing:
            existing[cid] = {
                "id": cid, "name": live_name or f"Competition {cid}",
                "region": "", "dates": "", "type": "", "status": "unknown",
                "atk_list": [], "def_list": [],
            }
            new_count += 1
        else:
            # Always update name from live HTML
            if live_name:
                existing[cid]["name"] = live_name
            # Ensure analyst list fields exist (never overwrite if already set)
            existing[cid].setdefault("atk_list", [])
            existing[cid].setdefault("def_list", [])

    catalog = sorted(existing.values(), key=lambda e: e["id"])
    _write_json(catalog_path, catalog)
    print(f"sync_catalog: {len(catalog)} competitions ({new_count} new)")
    return catalog


# ── Match list ───────────────────────────────────────────────────────────────

def sync_matches(comp_id, db, rebuild=False):
    """
    Fetch live match list, compare with stored matches.json, and merge.

    Merge rules:
      rebuild=True      → discard stored, use live entirely
      0 overlap         → live is likely wrong (client-rendered page); keep stored
      partial/full overlap → merge (stored takes priority for existing slugs)

    Returns (new_ids, slug_map) where new_ids are not in processed_matches table.
    """
    comp_id = int(comp_id)
    comp_dir = os.path.join(DATA, "competitions", str(comp_id))
    os.makedirs(comp_dir, exist_ok=True)
    matches_path = os.path.join(comp_dir, "matches.json")
    raw_stored = json.load(open(matches_path)) if os.path.exists(matches_path) else {}
    # Clean stored data on load in case a previous run left contamination
    stored, _ = _filter_by_prefix(raw_stored)
    if stored != raw_stored and raw_stored:
        _write_json(matches_path, stored)  # persist the cleanup immediately

    # Completed competitions have a fixed, final match list.
    # siege.gg's results page is client-rendered for completed competitions
    # and returns unrelated data — skip the network fetch entirely.
    # Status comes from competitions.json only (no config.json fallback needed).
    catalog = {c["id"]: c for c in _load_catalog()}
    _KNOWN_STATUSES = {"completed", "ongoing", "upcoming"}
    status = catalog.get(comp_id, {}).get("status")
    if status not in _KNOWN_STATUSES:
        status = "unknown"

    if status == "completed" and stored:
        # Already have the match list — use it, no need to hit the network
        print(f"  Competition {comp_id} is completed — using stored match list ({len(stored)} matches).", flush=True)
        processed = {str(r[0]) for r in db.execute(
            "SELECT match_id FROM processed_matches WHERE comp_id=?", (comp_id,)
        )}
        new_ids = [mid for mid in stored if mid not in processed]
        print(f"sync_matches({comp_id}): {len(stored)} total, {len(new_ids)} new", flush=True)
        return new_ids, stored

    if status == "completed" and not stored:
        # No stored data: try auto-seed from competitions.json first
        seed = catalog.get(comp_id, {}).get("matches")
        if seed:
            stored = seed
            _write_json(matches_path, stored)
            print(f"  Auto-seeded {len(stored)} matches from competitions.json.", flush=True)
            processed = {str(r[0]) for r in db.execute(
                "SELECT match_id FROM processed_matches WHERE comp_id=?", (comp_id,)
            )}
            new_ids = [mid for mid in stored if mid not in processed]
            print(f"sync_matches({comp_id}): {len(stored)} total, {len(new_ids)} new", flush=True)
            return new_ids, stored
        # No seed either — fall through to live scrape and let prefix filter guard the result

    if status == "upcoming":
        # Upcoming competitions have no matches yet — skip live scrape
        print(f"  Competition {comp_id} is upcoming — no matches yet, skipping.", flush=True)
        return [], stored

    # Scrape live pages (ongoing, or completed with no stored/seeded data)
    live = {}
    page = 1
    max_pages = 50
    while page <= max_pages:
        url = f"https://siege.gg/matches?tab=results&competitions={comp_id}&page={page}"
        print(f"  Fetching match list page {page}...", flush=True)
        html = _SESSION.get(url, timeout=30).text
        matches_on_page = re.findall(r'/matches/(\d+)-([a-z0-9-]+)', html)
        new_on_page = {mid: slug for mid, slug in matches_on_page if mid not in live}
        if not new_on_page:
            break
        live.update(new_on_page)
        page += 1

    # Compare live vs stored and decide how to merge
    if rebuild:
        # Rebuild: re-process stored matches from scratch (DB was already cleared by caller).
        # Do NOT replace stored with live — stored is the authoritative list.
        merged = stored
        print(f"  Rebuild mode: re-processing {len(stored)} stored matches.", flush=True)
    elif stored and live:
        overlap = len(set(live) & set(stored))
        overlap_pct = overlap / len(stored) * 100
        if overlap_pct < 50:
            # Live recognises fewer than half the stored matches — the page is likely
            # client-rendered or returning unrelated competition data (e.g. sidebar links).
            # Keep stored and warn rather than merging in bad IDs.
            merged = stored
            print(f"  WARNING: live list only matches {overlap}/{len(stored)} stored entries "
                  f"({overlap_pct:.0f}%). Keeping stored. "
                  f"Use --rebuild to override.", flush=True)
        else:
            # Sufficient overlap — merge incrementally (stored takes priority for existing slugs)
            merged = {**live, **stored}
    else:
        # stored is empty (first run) or live is empty — use whichever has data
        merged = {**live, **stored}

    # Apply prefix filter to catch any contamination before persisting
    merged, _ = _filter_by_prefix(merged)

    # Write back when merged changed OR when stored was empty (first live scrape).
    # For completed competitions with existing stored data, never overwrite.
    first_run = not raw_stored
    if merged != stored or (first_run and merged):
        if not (status == "completed" and not first_run):
            _write_json(matches_path, merged)

    # New = in live list but not yet processed
    processed = {str(r[0]) for r in db.execute(
        "SELECT match_id FROM processed_matches WHERE comp_id=?", (comp_id,)
    )}
    new_ids = [mid for mid in merged if mid not in processed]
    print(f"sync_matches({comp_id}): {len(merged)} total, {len(new_ids)} new")
    return new_ids, merged


# ── Payload fetch / cache ─────────────────────────────────────────────────────

_DEVALUE_SENTINELS = {-1: None, -2: None, -3: float("nan"),
                      -4: float("inf"), -5: float("-inf"), -6: -0.0}


def _devalue_unflatten(flat):
    if not isinstance(flat, list) or not flat:
        raise ValueError("devalue payload is not a non-empty array")
    cache = {}

    def hydrate(index):
        if isinstance(index, int) and index in _DEVALUE_SENTINELS:
            return _DEVALUE_SENTINELS[index]
        if index in cache:
            return cache[index]
        value = flat[index]
        if isinstance(value, list):
            if value and isinstance(value[0], str):
                tag = value[0]
                if tag in ("Date", "RegExp", "BigInt"):
                    cache[index] = value[1]; return value[1]
                if tag == "Set":
                    out = []; cache[index] = out
                    out.extend(hydrate(i) for i in value[1:]); return out
                if tag == "Map":
                    out = {}; cache[index] = out
                    it = value[1:]
                    for i in range(0, len(it), 2):
                        out[hydrate(it[i])] = hydrate(it[i + 1])
                    return out
                cache[index] = None
                r = hydrate(value[1]) if len(value) > 1 else None
                cache[index] = r; return r
            arr = []; cache[index] = arr
            arr.extend(hydrate(i) for i in value); return arr
        if isinstance(value, dict):
            obj = {}; cache[index] = obj
            for k, v in value.items():
                obj[k] = hydrate(v)
            return obj
        cache[index] = value; return value

    return hydrate(0)


def _find_match_object(node, seen=None):
    if seen is None:
        seen = set()
    if id(node) in seen:
        return None
    if isinstance(node, (dict, list)):
        seen.add(id(node))
    if isinstance(node, dict):
        games = node.get("games")
        if isinstance(games, list) and games and isinstance(games[0], dict) and "map" in games[0]:
            return node
        for v in node.values():
            result = _find_match_object(v, seen)
            if result:
                return result
    elif isinstance(node, list):
        for v in node:
            result = _find_match_object(v, seen)
            if result:
                return result
    return None


def fetch_payload(match_id, slug, comp_id):
    """
    Return decoded match object for match_id.
    Uses cache at data/competitions/{comp_id}/raw/{match_id}.json if available.
    """
    cache_path = os.path.join(
        DATA, "competitions", str(comp_id), "raw", f"{match_id}.json"
    )
    if os.path.exists(cache_path):
        return json.load(open(cache_path))

    url = f"https://siege.gg/matches/{match_id}-{slug}".rstrip("-")
    html = _SESSION.get(url, timeout=30).text

    m = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise ValueError(f"match {match_id}: no __NUXT_DATA__ found")

    payload = _devalue_unflatten(json.loads(m.group(1)))
    match_obj = _find_match_object(payload)
    if not match_obj:
        raise ValueError(f"match {match_id}: no match object in payload")

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    _write_json(cache_path, match_obj)
    return match_obj


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_catalog():
    path = os.path.join(DATA, "competitions.json")
    return json.load(open(path)) if os.path.exists(path) else []


def _filter_by_prefix(matches):
    """Remove entries whose slug prefix (first two dash-separated words) does not
    match the majority prefix in the dict. Returns (filtered_dict, removed_count).

    Example: if 55 slugs start with 'mjr-intl-' and 5 start with 'start-latam-',
    the 5 are removed. Requires ≥60% majority to apply filtering."""
    if not matches:
        return matches, 0
    from collections import Counter
    prefix_counts = Counter(
        "-".join(slug.split("-")[:2]) for slug in matches.values()
    )
    dominant, dominant_count = prefix_counts.most_common(1)[0]
    if dominant_count / len(matches) < 0.6:
        return matches, 0  # no clear majority — leave untouched
    filtered = {k: v for k, v in matches.items()
                if "-".join(v.split("-")[:2]) == dominant}
    removed = len(matches) - len(filtered)
    if removed:
        print(f"  Removed {removed} entries with non-dominant slug prefix "
              f"(dominant: '{dominant}', kept {len(filtered)})", flush=True)
    return filtered, removed


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
