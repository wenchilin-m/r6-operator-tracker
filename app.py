import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime

import pandas as pd
import streamlit as st
from matplotlib.colors import LinearSegmentedColormap

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DB   = os.path.join(DATA, "siege.db")
REF  = os.path.join(HERE, "reference")

# ── Reference ──────────────────────────────────────────────────────────────────

@st.cache_data
def load_reference():
    m = json.load(open(os.path.join(REF, "maps.json")))
    return m["map_order"], m["map_picks_layout"]


# ── Competition catalog ────────────────────────────────────────────────────────

@st.cache_data
def load_catalog():
    path = os.path.join(DATA, "competitions.json")
    if not os.path.exists(path):
        return []
    return json.load(open(path))


_MONTHS = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
           "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}

def _comp_end_key(dates_str, year=2026):
    """Return a sortable int (YYYYMMDD) from the END date of 'May 8 – 17', or 0 if unparseable."""
    if not dates_str:
        return 0
    try:
        parts = re.split(r'\s*[–-]\s*', dates_str.strip())
        start_part = parts[0].split()
        start_month = _MONTHS[start_part[0][:3]]
        end_part = parts[1].split()
        if len(end_part) == 2:
            end_month, end_day = _MONTHS[end_part[0][:3]], int(end_part[1])
        else:
            end_month, end_day = start_month, int(end_part[0])
        return year * 10000 + end_month * 100 + end_day
    except (KeyError, ValueError, IndexError):
        return 0


def available_competitions():
    """Return [(id, name)] for competitions that have a workbook on disk."""
    catalog = {c["id"]: c for c in load_catalog()}
    result = []
    comps_dir = os.path.join(DATA, "competitions")
    if not os.path.exists(comps_dir):
        return result
    for d in sorted(os.listdir(comps_dir), key=lambda x: int(x) if x.isdigit() else 0):
        if not d.isdigit():
            continue
        cid = int(d)
        wb_path = os.path.join(comps_dir, d, "workbook.xlsx")
        if not os.path.exists(wb_path):
            continue
        name  = catalog.get(cid, {}).get("name", f"Competition {cid}")
        dates = catalog.get(cid, {}).get("dates", "")
        result.append((cid, name, dates))
    # Sort most recent first
    result.sort(key=lambda x: _comp_end_key(x[2]), reverse=True)
    return [(cid, name) for cid, name, _ in result]


# ── DB queries ─────────────────────────────────────────────────────────────────

def _conn():
    if not os.path.exists(DB):
        return None
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def _where(comp_ids):
    if comp_ids is None:
        return "", []
    return f"WHERE comp_id IN ({','.join('?'*len(comp_ids))})", list(comp_ids)


@st.cache_data(ttl=60)
def query_map_picks(comp_ids_tuple):
    conn = _conn()
    if not conn:
        return pd.DataFrame()
    comp_ids = list(comp_ids_tuple) if comp_ids_tuple else None
    w, params = _where(comp_ids)
    df = pd.read_sql_query(
        f"SELECT map, site, COUNT(*) AS rounds, SUM(def_win) AS def_wins FROM rounds {w} GROUP BY map, site",
        conn, params=params
    )
    conn.close()
    return df


@st.cache_data(ttl=60)
def query_ops(comp_ids_tuple):
    conn = _conn()
    if not conn:
        return pd.DataFrame(), pd.DataFrame()
    comp_ids = list(comp_ids_tuple) if comp_ids_tuple else None
    w, params = _where(comp_ids)
    picks = pd.read_sql_query(
        f"SELECT map, side, operator, site, COUNT(*) AS appearances FROM picks {w} GROUP BY map, side, operator, site",
        conn, params=params
    )
    bans = pd.read_sql_query(
        f"SELECT map, side, operator, COUNT(*) AS times_banned, SUM(game_rounds) AS value FROM bans {w} GROUP BY map, side, operator",
        conn, params=params
    )
    conn.close()
    return picks, bans


# ── App layout ─────────────────────────────────────────────────────────────────

st.set_page_config(page_title="R6 Operator Tracker", page_icon="🎮", layout="wide")

# ── Elegant console theme ───────────────────────────────────────────────────────
# Recreates the "console / light" direction from the Claude Design handoff:
# cool paper canvas, ruled-grid texture, near-black ink, muted clay accent,
# mono-forward numerals (Geist Mono), Newsreader-italic accents.

# Clay-diverging heatmap, lifted from the design's palette engine (sRGB stops):
# clay (low) → cream (neutral) → teal (high). Used for every heat cell.
CLAY = LinearSegmentedColormap.from_list("r6_clay", [
    (0.00, "#965039"), (0.27, "#c7967c"), (0.50, "#ede7db"),
    (0.73, "#89a89e"), (1.00, "#34675d"),
])


def inject_theme():
    st.html(
        """
<style>
@import url('https://fonts.googleapis.com/css2?family=Geist:wght@400;450;500;600;700&family=Geist+Mono:wght@400;500&family=Newsreader:ital,opsz,wght@1,6..72,400;1,6..72,500&display=swap');
:root {
  --ff-sans:"Geist", system-ui, sans-serif;
  --ff-mono:"Geist Mono", ui-monospace, monospace;
  --ff-serif:"Newsreader", Georgia, serif;
  --bg:#f3f4f3; --panel:#fbfbfa; --card:#ffffff;
  --ink:#16181a; --ink-2:#5d6166; --ink-3:#969a9e;
  --line:#e2e4e3; --line-2:#eceeed;
  --accent:#b05a44; --accent-soft:#f1e0db; --r:4px;
}

/* canvas + base type — console ruled-grid texture */
.stApp {
  color:var(--ink);
  background-color:var(--bg);
  background-image:
    linear-gradient(to right, var(--line-2) 1px, transparent 1px),
    linear-gradient(to bottom, var(--line-2) 1px, transparent 1px);
  background-size:48px 48px;
}
[data-testid="stHeader"] { background:transparent; }
/* base sans — !important to beat Streamlit's per-element font rules */
.stApp, .stApp p, .stApp li, .stApp a, .stApp button, .stApp input,
.stApp select, .stApp textarea, .stApp h1, .stApp h2, .stApp h3, .stApp h4,
.stApp h5, .stMarkdown, [data-testid="stMarkdownContainer"],
.r6-title, .r6-section-head h3, .r6-colhead {
  font-family:var(--ff-sans) !important;
}
/* mono — labels, eyebrows, numeric chrome */
.r6-wordmark .tag, .r6-eyebrow, .r6-sub, .r6-delta-label, .r6-delta-cell .num,
.r6-side-foot, [data-testid="stSidebar"] label, [data-testid="stWidgetLabel"] p,
[data-testid="stMetricLabel"] p {
  font-family:var(--ff-mono) !important;
}
/* serif italic accents */
.r6-title .em, .r6-section-head .em { font-family:var(--ff-serif) !important; }
/* console: stat numerals are mono */
[data-testid="stMetricValue"] { font-family:var(--ff-mono) !important; }

/* ---------- sidebar ---------- */
[data-testid="stSidebar"] {
  background:var(--panel); border-right:1px solid var(--line);
}
[data-testid="stSidebar"] .block-container { padding-top:30px; }
.r6-wordmark { display:flex; flex-direction:column; gap:4px; margin-bottom:26px; }
.r6-wordmark .tag {
  font-family:var(--ff-mono); font-size:10.5px; letter-spacing:.22em;
  text-transform:uppercase; color:var(--accent);
}
.r6-wordmark h1 {
  margin:0; font-size:17px; font-weight:500; letter-spacing:-.02em;
  line-height:1.2; color:var(--ink); padding:0;
  font-family:var(--ff-mono) !important;
}
.r6-side-foot {
  margin-top:18px; color:var(--ink-3); font-family:var(--ff-mono); font-size:11px;
}
/* sidebar field labels (multiselect / radio headers) */
[data-testid="stSidebar"] label, [data-testid="stWidgetLabel"] p {
  font-family:var(--ff-mono); font-size:11px; letter-spacing:.14em;
  text-transform:uppercase; color:var(--ink-3); font-weight:500;
}

/* ---------- page header ---------- */
.r6-pagehead { margin:6px 0 34px; }
.r6-eyebrow {
  font-family:var(--ff-mono); font-size:11.5px; letter-spacing:.16em;
  text-transform:uppercase; color:var(--accent); margin-bottom:14px;
}
.r6-title {
  margin:0; font-size:46px; font-weight:500; letter-spacing:-.035em;
  line-height:1.02; color:var(--ink);
}
.r6-title .em, .r6-section-head .em {
  font-family:var(--ff-serif); font-style:italic; font-weight:400; color:var(--ink-2);
}
.r6-sub {
  margin-top:14px; color:var(--ink-3); font-family:var(--ff-mono);
  font-size:13px; letter-spacing:.02em;
}

/* ---------- section heads ---------- */
.r6-section-head { margin:8px 0 18px; }
.r6-section-head h3 {
  margin:0; font-size:24px; font-weight:600; letter-spacing:-.015em; color:var(--ink);
}
.r6-section-head p { margin:8px 0 0; color:var(--ink-2); font-size:14px; }

/* ---------- metrics (stat row) ---------- */
[data-testid="stMetric"] {
  background:transparent; border:none; padding:0;
}
[data-testid="stMetricLabel"] p {
  font-family:var(--ff-mono) !important; font-size:11px !important; letter-spacing:.12em;
  text-transform:uppercase; color:var(--ink-3) !important; font-weight:500 !important;
}
[data-testid="stMetricValue"] {
  font-family:var(--ff-mono); font-size:38px; font-weight:500;
  letter-spacing:-.02em; color:var(--ink); line-height:1;
}

/* ---------- delta-by-site grid ---------- */
.r6-delta-label {
  font-family:var(--ff-mono); font-size:11px; letter-spacing:.12em;
  text-transform:uppercase; color:var(--ink-3); margin:8px 0 14px;
}
.r6-delta-grid {
  display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); gap:1px;
  background:var(--line); border:1px solid var(--line); border-radius:var(--r);
  overflow:hidden; margin-bottom:8px;
}
.r6-delta-cell { background:var(--card); padding:20px 22px; }
.r6-delta-cell .site { color:var(--ink-2); font-size:13.5px; margin-bottom:10px; }
.r6-delta-cell .num {
  font-family:var(--ff-mono); font-size:30px; font-weight:500; letter-spacing:-.01em;
}
.r6-delta-cell .num .arrow { font-size:18px; vertical-align:2px; margin-right:4px; }
.r6-pos-up { color:#4f7a4a; }
.r6-pos-down { color:var(--accent); }

/* ---------- subheaders (Defense / Attack) ---------- */
.r6-colhead {
  font-size:22px; font-weight:600; letter-spacing:-.015em; color:var(--ink);
  margin:6px 0 14px;
}

/* ---------- expander ---------- */
[data-testid="stExpander"] {
  border:1px solid var(--line); border-radius:var(--r); background:var(--card);
}
[data-testid="stExpander"] summary { color:var(--ink); font-size:14.5px; }
[data-testid="stExpander"] summary:hover { color:var(--accent); }

/* ---------- dataframe ---------- */
[data-testid="stDataFrame"] { border:1px solid var(--line); border-radius:var(--r); }

/* ---------- download button ---------- */
[data-testid="stDownloadButton"] button,
[data-testid="stBaseButton-secondary"] {
  border:1px solid var(--line); background:var(--card); color:var(--ink);
  border-radius:var(--r); font-family:var(--ff-mono); font-size:12.5px;
  letter-spacing:.02em;
}
[data-testid="stLinkButton"] a {
  border:1px solid var(--line); background:var(--panel); color:var(--ink);
  border-radius:var(--r); font-family:var(--ff-mono); font-size:12.5px;
  letter-spacing:.02em; text-decoration:none;
}
[data-testid="stDownloadButton"] button:hover,
[data-testid="stLinkButton"] a:hover { border-color:var(--ink-3); color:var(--ink); }

/* hairline divider */
hr { border:none; border-top:1px solid var(--line); }
</style>
"""
    )


_TITLE_YEAR = re.compile(r"\b(\d{4})\b")


def format_title(name):
    """Roman lead + serif-italic accent: split on the 4-digit year so the
    year (+ any trailing phase) becomes the lyrical accent. Mirrors the design's
    formatTitle so it generalizes to any competition name."""
    m = _TITLE_YEAR.search(name)
    if not m:
        parts = name.strip().split()
        if len(parts) <= 1:
            return name, ""
        return " ".join(parts[:-1]), parts[-1]
    return name[: m.start()].strip(), name[m.start():].strip()


def page_head(eyebrow, title, sub):
    lead, accent = format_title(title)
    accent_html = f' <span class="em">{accent}</span>' if accent else ""
    st.markdown(
        f'<div class="r6-pagehead">'
        f'<div class="r6-eyebrow">{eyebrow}</div>'
        f'<h1 class="r6-title">{lead}{accent_html}</h1>'
        + (f'<div class="r6-sub">{sub}</div>' if sub else "")
        + "</div>",
        unsafe_allow_html=True,
    )


def section_head(lead, accent, desc):
    accent_html = f' <span class="em">{accent}</span>' if accent else ""
    st.markdown(
        f'<div class="r6-section-head"><h3>{lead}{accent_html}</h3>'
        f'<p>{desc}</p></div>',
        unsafe_allow_html=True,
    )


inject_theme()

MAP_ORDER, MAP_PICKS_LAYOUT = load_reference()
MAP_PICKS_ORDER = [m for m, _ in MAP_PICKS_LAYOUT]
comps = available_competitions()

with st.sidebar:
    st.markdown(
        '<div class="r6-wordmark">'
        '<span class="tag">R6 · Operator Tracker</span>'
        '<h1>Match Analytics</h1>'
        '</div>',
        unsafe_allow_html=True,
    )

    if not comps:
        st.warning("No competition data found. Run `python3 run.py fetch --comp 100`.")
        st.stop()

    comp_map = {cid: name for cid, name in comps}
    options  = [cid for cid, _ in comps]
    labels   = {cid: name for cid, name in comps}

    selected_ids = st.multiselect(
        "Competitions",
        options=options,
        default=options,
        format_func=lambda cid: labels[cid],
    )

    page = st.radio("Report Type", ["Operator Tracker", "Map Performance"])

    st.link_button("View on SiegeGG ↗", "https://siege.gg/competitions")

    # Download only available when exactly one competition is selected
    if len(selected_ids) == 1:
        cid = selected_ids[0]
        wb_path = os.path.join(DATA, "competitions", str(cid), "workbook.xlsx")
        if os.path.exists(wb_path):
            with open(wb_path, "rb") as f:
                st.download_button(
                    label="Export Report",
                    data=f,
                    file_name=f"Siege Stats - Competition {cid}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )

# comp_ids_tuple: None = all competitions, tuple = selected subset
all_selected = set(selected_ids) == set(options)
comp_ids_tuple = None if all_selected else tuple(selected_ids)

if not selected_ids:
    st.info("Select at least one competition in the sidebar.")
    st.stop()

catalog_lookup = {c["id"]: c for c in load_catalog()}

if all_selected:
    comp_title = "All Competitions"
    date_note  = ""
elif len(selected_ids) == 1:
    cid = selected_ids[0]
    comp_title = comp_map[cid]
    date_note  = catalog_lookup.get(cid, {}).get("dates", "")
else:
    comp_title = f"{len(selected_ids)} Competitions"
    date_note  = ""

if all_selected or len(selected_ids) != 1:
    sub = f"{len(selected_ids)} competitions selected" if not all_selected else "All competitions"
else:
    sub = date_note
page_head("Tournament", comp_title, sub)


# ── Page: Map Win Rates ────────────────────────────────────────────────────────

if page == "Map Performance":
    section_head("Map &amp; Site", "Win Rates", "Defender win % across the selected competition(s).")

    df_rounds = query_map_picks(comp_ids_tuple)

    if df_rounds.empty:
        st.info("No round data available.")
    else:
        # Cross-map summary table
        summary = []
        for map_name in MAP_PICKS_ORDER:
            map_df = df_rounds[df_rounds["map"] == map_name]
            total_rounds = int(map_df["rounds"].sum()) if not map_df.empty else 0
            if total_rounds == 0:
                continue
            total_wins = int(map_df["def_wins"].sum())
            map_wr = round(100 * total_wins / total_rounds)
            site_rows = map_df[map_df["site"].notna()].copy()
            site_rows["wr"] = (100 * site_rows["def_wins"] / site_rows["rounds"]).round().astype(int)
            site_rows["delta"] = (site_rows["wr"] - map_wr) * 100 * (site_rows["rounds"] / total_rounds)
            best  = site_rows.loc[site_rows["delta"].idxmax(), "site"] if not site_rows.empty else "—"
            worst = site_rows.loc[site_rows["delta"].idxmin(), "site"] if not site_rows.empty else "—"
            summary.append({"Map": map_name, "Rounds": total_rounds,
                            "Def Win Rate": map_wr, "Best Def Site": best, "Best Atk Site": worst})

        if summary:
            summary_df = pd.DataFrame(summary).sort_values("Def Win Rate", ascending=False).reset_index(drop=True)
            styler = summary_df.style.background_gradient(
                subset=["Def Win Rate"], cmap=CLAY, vmin=30, vmax=70
            ).format({"Def Win Rate": "{}%"})
            st.dataframe(styler, use_container_width=True, hide_index=True)
            st.divider()

            section_head("Site Win-Rate", "Delta",
                         "Bomb sites ranked by how far their win rate deviates from the map baseline.")

        # Flat site-level detail table — all maps and sites in one view
        detail = []
        for map_name in MAP_PICKS_ORDER:
            map_df = df_rounds[df_rounds["map"] == map_name]
            total_rounds = int(map_df["rounds"].sum()) if not map_df.empty else 0
            if total_rounds == 0:
                continue
            total_wins = int(map_df["def_wins"].sum())
            map_wr = round(100 * total_wins / total_rounds)
            site_rows = map_df[map_df["site"].notna()].copy()
            for _, row in site_rows.iterrows():
                site_wr = round(100 * row["def_wins"] / row["rounds"])
                delta = (site_wr - map_wr) * 100 * (row["rounds"] / total_rounds)
                detail.append({
                    "Map": map_name,
                    "Site": row["site"],
                    "Rounds": int(row["rounds"]),
                    "Win Rate %": site_wr,
                    "Win Rate Delta": round(delta, 1),
                })

        if detail:
            detail_df = pd.DataFrame(detail).sort_values("Win Rate Delta", ascending=False).reset_index(drop=True)
            _dmax = detail_df["Win Rate Delta"].abs().max() or 1
            styler = detail_df.style.background_gradient(
                subset=["Win Rate %"], cmap=CLAY, vmin=30, vmax=70
            ).background_gradient(
                subset=["Win Rate Delta"], cmap=CLAY, vmin=-_dmax, vmax=_dmax
            ).format({"Win Rate %": "{}%", "Win Rate Delta": "{:+.1f}"})
            st.dataframe(styler, use_container_width=True, hide_index=True)

        with st.expander("What is Win Rate Delta?"):
            st.markdown(
                "**Win Rate Delta** measures how much defenders over- or under-perform "
                "at each bomb site compared to the map average, scaled by how often "
                "that site is played.\n\n"
                "- **Positive (+)** — defenders win more rounds here than the map average. "
                "This site has a structural defensive advantage.\n"
                "- **Negative (−)** — defenders consistently lose more rounds here than average. "
                "Attackers prefer to play this site.\n"
                "- **Near zero** — this site plays roughly in line with the map average."
            )

# ── Page: Operator Meta ────────────────────────────────────────────────────────

elif page == "Operator Tracker":
    selected_map = st.selectbox("Map", MAP_ORDER)

    df_picks, df_bans = query_ops(comp_ids_tuple)

    # Map-level metrics from rounds
    df_rounds = query_map_picks(comp_ids_tuple)
    map_rounds_df = df_rounds[df_rounds["map"] == selected_map]
    total_rounds = int(map_rounds_df["rounds"].sum()) if not map_rounds_df.empty else 0
    total_wins   = int(map_rounds_df["def_wins"].sum()) if not map_rounds_df.empty else 0
    map_wr = round(100 * total_wins / total_rounds) if total_rounds else None

    m1, m2, _ = st.columns([1, 1, 4])
    m1.metric("Rounds played", total_rounds if total_rounds else "—")
    m2.metric("Def win rate", f"{map_wr}%" if map_wr is not None else "—")

    # Site deviations as metrics
    if not map_rounds_df.empty and total_rounds > 0:
        map_wr_val = round(100 * total_wins / total_rounds)
        site_devs = []
        for _, row in map_rounds_df.iterrows():
            if row["site"] and row["rounds"] > 0:
                site_wr = round(100 * row["def_wins"] / row["rounds"])
                dev = (site_wr - map_wr_val) * 100 * (row["rounds"] / total_rounds)
                site_devs.append((row["site"], dev))
        if site_devs:
            st.markdown('<div class="r6-delta-label">Win Rate Δ by Site</div>',
                        unsafe_allow_html=True)
            cells = ""
            for site, dev in site_devs:
                pos = dev >= 0
                cls = "r6-pos-up" if pos else "r6-pos-down"
                arrow = "↑" if pos else "↓"
                sign = "+" if pos else ""
                cells += (
                    f'<div class="r6-delta-cell"><div class="site">{site}</div>'
                    f'<div class="num {cls}"><span class="arrow">{arrow}</span>'
                    f'{sign}{dev:.1f}</div></div>'
                )
            st.markdown(f'<div class="r6-delta-grid">{cells}</div>',
                        unsafe_allow_html=True)
            with st.expander("What is Win Rate Delta?"):
                st.markdown(
                    "**Win Rate Delta** measures how much defenders over- or under-perform "
                    "at each bomb site compared to the map average, scaled by how often "
                    "that site is played.\n\n"
                    "- **Positive (+)** — defenders win more rounds here than the map average. "
                    "This site has a structural defensive advantage; attackers tend to avoid banning it.\n"
                    "- **Negative (−)** — defenders consistently lose more rounds here than average. "
                    "Attackers prefer to play this site.\n"
                    "- **Near zero** — this site plays roughly in line with the map average.\n\n"
                    "Sites played rarely contribute less to the score than heavily contested ones, "
                    "so the number reflects real competitive relevance."
                )

    def ops_table(side):
        p = df_picks[(df_picks["map"] == selected_map) & (df_picks["side"] == side)] if not df_picks.empty else pd.DataFrame()
        b = df_bans[(df_bans["map"] == selected_map) & (df_bans["side"] == side)] if not df_bans.empty else pd.DataFrame()

        if p.empty and b.empty:
            st.info(f"No {side} data.")
            return

        # Pivot picks to wide (operator × site)
        if not p.empty:
            wide = p.pivot_table(index="operator", columns="site", values="appearances",
                                 aggfunc="sum", fill_value=0).reset_index()
        else:
            wide = pd.DataFrame(columns=["operator"])

        # Merge bans
        if not b.empty:
            b_sub = b[["operator", "times_banned", "value"]]
            df = wide.merge(b_sub, on="operator", how="outer").fillna(0)
        else:
            df = wide.copy()
            df["times_banned"] = 0
            df["value"] = 0

        df["times_banned"] = df["times_banned"].astype(int)
        df["value"]        = df["value"].astype(int)

        site_cols = [c for c in df.columns if c not in ["operator", "times_banned", "value"]]
        for s in site_cols:
            df[s] = df[s].astype(int)

        # Priority = Value + site picks
        prio_cols = [f"{s} ★" for s in site_cols]
        for s, p_col in zip(site_cols, prio_cols):
            df[p_col] = df["value"] + df[s]

        display_cols = ["operator", "times_banned", "value"] + prio_cols
        df = df[display_cols].sort_values("times_banned", ascending=False).reset_index(drop=True)
        df.columns = ["Operator", "Times Banned", "Value"] + prio_cols

        styler = (
            df.style
            .background_gradient(subset=["Times Banned"], cmap=CLAY)
            .background_gradient(subset=prio_cols, cmap=CLAY)
            .format(precision=0)
        )
        st.dataframe(styler, use_container_width=True, hide_index=True)

    st.markdown('<div class="r6-colhead">Defense</div>', unsafe_allow_html=True)
    ops_table("Defense")
    st.markdown('<div class="r6-colhead">Attack</div>', unsafe_allow_html=True)
    ops_table("Attack")

# ── Page: Data ─────────────────────────────────────────────────────────────────

else:
    st.header("Data")

    if len(selected_ids) != 1:
        st.info("Select exactly one competition in the sidebar to rebuild its workbook.")
    else:
        cid = selected_ids[0]
        cname = comp_map[cid]
        conn = _conn()
        if conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM processed_matches WHERE comp_id=?", (cid,)
            ).fetchone()
            match_count = row[0] if row else 0
            conn.close()
            st.caption(f"{match_count} matches processed for {cname}")
        else:
            st.caption("siege.db not found — run fetch first")

        st.info("Fetches all matches from siege.gg and rebuilds the workbook. Takes ~1–2 minutes.")

        if st.button("Rebuild from siege.gg", type="primary"):
            log_box = st.empty()
            lines = []
            proc = subprocess.Popen(
                [sys.executable, os.path.join(HERE, "run.py"),
                 "refresh", "--comp", str(cid)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=HERE,
            )
            for line in proc.stdout:
                lines.append(line.rstrip())
                log_box.code("\n".join(lines), language=None)
            proc.wait()
            if proc.returncode == 0:
                st.success("Done.")
                st.cache_data.clear()
            else:
                st.error("Build failed — check log above.")
