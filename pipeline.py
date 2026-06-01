"""
pipeline.py — normalize match payloads and store to siege.db (SQLite).

Public API:
  init_db(db_path)                          → sqlite3.Connection with schema
  normalize_match(match, comp_id, match_id) → (picks, bans, rounds)
  store(conn, picks, bans, rounds, match_id, comp_id)  → atomic insert
"""
import json
import os
import re
import sqlite3
import unicodedata
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
_REF = os.path.join(HERE, "reference")

# Load game-wide operator reference once at import time
_ops_ref = json.load(open(os.path.join(_REF, "operators.json")))
OPERATOR_SIDE  = _ops_ref["side"]
OPERATOR_CANON = _ops_ref["canon"]

_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
_ALT_RE = re.compile(r'alt="([^"]+)"')


# ── SQLite ────────────────────────────────────────────────────────────────────

DB_PATH = os.path.join(HERE, "data", "siege.db")


def init_db(db_path=None):
    """Open (or create) siege.db, ensure schema exists, return connection."""
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS picks (
            comp_id  INTEGER, match_id INTEGER, date TEXT,
            map TEXT, round INTEGER, side TEXT, operator TEXT, site TEXT
        );
        CREATE TABLE IF NOT EXISTS bans (
            comp_id INTEGER, match_id INTEGER,
            map TEXT, side TEXT, operator TEXT, game_rounds INTEGER
        );
        CREATE TABLE IF NOT EXISTS rounds (
            comp_id INTEGER, match_id INTEGER,
            map TEXT, site TEXT, def_win INTEGER
        );
        CREATE TABLE IF NOT EXISTS processed_matches (
            match_id    INTEGER PRIMARY KEY,
            comp_id     INTEGER NOT NULL,
            processed_at TEXT   NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_picks_comp  ON picks(comp_id);
        CREATE INDEX IF NOT EXISTS idx_bans_comp   ON bans(comp_id);
        CREATE INDEX IF NOT EXISTS idx_rounds_comp ON rounds(comp_id);
    """)
    conn.commit()
    return conn


def store(conn, picks, bans, rounds, match_id, comp_id):
    """Insert all records for one match atomically. Skips if already processed."""
    mid = int(match_id)
    row = conn.execute(
        "SELECT 1 FROM processed_matches WHERE match_id=?", (mid,)
    ).fetchone()
    if row:
        return  # idempotent

    with conn:  # single transaction — rolls back on error
        conn.executemany(
            "INSERT INTO picks VALUES (?,?,?,?,?,?,?,?)",
            [(comp_id, mid, p["date"], p["map"], p["round"],
              p["side"], p["operator"], p["site"]) for p in picks]
        )
        conn.executemany(
            "INSERT INTO bans VALUES (?,?,?,?,?,?)",
            [(comp_id, mid, b["map"], b["side"],
              b["operator"], b["game_rounds"]) for b in bans]
        )
        conn.executemany(
            "INSERT INTO rounds VALUES (?,?,?,?,?)",
            [(comp_id, mid, r["map"], r["site"], r["def_win"]) for r in rounds]
        )
        conn.execute(
            "INSERT INTO processed_matches VALUES (?,?,?)",
            (mid, comp_id, datetime.utcnow().isoformat())
        )


# ── Normalization ─────────────────────────────────────────────────────────────

def _canon(name):
    raw = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    key = raw.upper().strip()
    return OPERATOR_CANON.get(key, raw.strip().title())


def _ban_side(raw):
    return "Defense" if str(raw or "").upper().startswith("DEF") else "Attack"


def _event_operators(html):
    out = []
    for tag in _IMG_RE.findall(html or ""):
        alt = _ALT_RE.search(tag)
        if not alt:
            continue
        op = _canon(alt.group(1))
        side = OPERATOR_SIDE.get(op)
        if side:
            out.append((op, side))
    return out


def _parse_date(raw):
    if not raw:
        return ""
    s = str(raw)
    return s[:10] if len(s) >= 10 else s


def normalize_match(match, comp_id, match_id):
    """
    Decode a match payload dict into three fact lists.

    Returns:
      picks  — [{comp_id, match_id, date, map, round, side, operator, site}]
      bans   — [{comp_id, match_id, map, side, operator, game_rounds}]
      rounds — [{comp_id, match_id, map, site, def_win}]
    """
    comp_id  = int(comp_id)
    match_id = int(match_id)
    picks, bans, rounds = [], [], []

    date = _parse_date(
        match.get("date") or match.get("scheduled_at") or match.get("start_time")
    )

    for game in match.get("games", []):
        map_name = (game.get("map") or {}).get("name")
        grounds  = game.get("rounds") or []
        if not map_name or not grounds:
            continue
        n_rounds = len(grounds)

        for b in game.get("operator_bans", []):
            op     = (b.get("operator") or {})
            opname = op.get("name")
            if opname:
                bans.append({
                    "comp_id": comp_id, "match_id": match_id,
                    "map": map_name, "side": _ban_side(op.get("side")),
                    "operator": _canon(opname), "game_rounds": n_rounds,
                })

        for round_num, rnd in enumerate(grounds, start=1):
            site = (rnd.get("site") or {}).get("name")
            if rnd.get("def_win") is not None and not rnd.get("is_tie"):
                rounds.append({
                    "comp_id": comp_id, "match_id": match_id,
                    "map": map_name, "site": site,
                    "def_win": 1 if rnd["def_win"] else 0,
                })
            seen = set()
            for ev in rnd.get("events", []):
                for op, side in _event_operators(ev.get("html", "")):
                    if (op, side) in seen:
                        continue
                    seen.add((op, side))
                    if site:
                        picks.append({
                            "comp_id": comp_id, "match_id": match_id,
                            "date": date, "map": map_name, "round": round_num,
                            "side": side, "operator": op, "site": site,
                        })

    return picks, bans, rounds
