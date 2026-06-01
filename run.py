#!/usr/bin/env python3
"""
run.py — R6 Operator Tracker pipeline CLI.

Commands:
  sync-catalog                              Update data/competitions.json from siege.gg
  sync-matches  [--comp N|--active|--all]   Update matches.json per competition
  fetch         [--comp N|--active|--all]   Fetch + normalize new matches → siege.db
  build         [--comp N|--all]            Generate workbook.xlsx per competition
  refresh       [--comp N|--active|--all]   sync-catalog + sync-matches + fetch + build

Flags:
  --rebuild   Discard stored matches.json and re-fetch raw payloads from scratch.
              Use when stored data is wrong or corrupted.

Examples:
  python3 run.py sync-catalog
  python3 run.py refresh --active              # nightly job
  python3 run.py refresh --comp 101
  python3 run.py refresh --comp 100 --rebuild  # force full re-fetch
  python3 run.py build --comp 100
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")


def _load_catalog():
    path = os.path.join(DATA, "competitions.json")
    if not os.path.exists(path):
        return []
    return json.load(open(path))


def _resolve_comps(args_comp, args_active, args_all):
    catalog = _load_catalog()
    if args_comp:
        return [args_comp]
    if args_active:
        return [c["id"] for c in catalog if c.get("status") == "ongoing"]
    if args_all:
        return [c["id"] for c in catalog]
    # Default: show error
    print("Specify --comp N, --active, or --all", file=sys.stderr)
    sys.exit(1)


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_sync_catalog(_args):
    from scraper import sync_catalog
    sync_catalog()


def cmd_sync_matches(args):
    from scraper import sync_matches
    from pipeline import init_db
    db = init_db()
    comp_ids = _resolve_comps(args.comp, args.active, args.all)
    for comp_id in comp_ids:
        try:
            sync_matches(comp_id, db)
        except Exception as e:
            print(f"  ERROR sync_matches({comp_id}): {e}", file=sys.stderr)
    db.close()


def _do_fetch(comp_ids, db, rebuild=False):
    from scraper import sync_matches, fetch_payload
    from pipeline import normalize_match, store
    fetched_by_comp = {}
    for comp_id in comp_ids:
        print(f"\n[{comp_id}] Syncing match list...", flush=True)
        if rebuild:
            # Clear processed_matches so all matches are treated as new
            db.execute("DELETE FROM processed_matches WHERE comp_id=?", (comp_id,))
            db.execute("DELETE FROM picks  WHERE comp_id=?", (comp_id,))
            db.execute("DELETE FROM bans   WHERE comp_id=?", (comp_id,))
            db.execute("DELETE FROM rounds WHERE comp_id=?", (comp_id,))
            db.commit()
            print(f"  Cleared existing data for competition {comp_id}.", flush=True)
        try:
            new_ids, slug_map = sync_matches(comp_id, db, rebuild=rebuild)
        except Exception as e:
            print(f"  ERROR sync({comp_id}): {e}", file=sys.stderr)
            continue
        if not new_ids:
            print(f"  No new matches.")
            fetched_by_comp[comp_id] = 0
            continue
        fetched = 0
        for mid in sorted(new_ids, key=int):
            slug = slug_map.get(str(mid), "")
            try:
                print(f"  Fetching match {mid} ({slug})...", flush=True)
                payload = fetch_payload(mid, slug, comp_id)
                picks, bans, rounds = normalize_match(payload, comp_id, mid)
                store(db, picks, bans, rounds, mid, comp_id)
                fetched += 1
            except Exception as e:
                print(f"  ERROR match {mid}: {e}", file=sys.stderr)
        print(f"  {fetched} matches stored for competition {comp_id}.")
        fetched_by_comp[comp_id] = fetched
    return fetched_by_comp


def cmd_fetch(args):
    from pipeline import init_db
    db = init_db()
    # Always sync catalog first so status info is current
    from scraper import sync_catalog
    print("Syncing competition catalog...", flush=True)
    sync_catalog()
    comp_ids = _resolve_comps(args.comp, args.active, args.all)
    _do_fetch(comp_ids, db, rebuild=getattr(args, "rebuild", False))
    db.close()


def cmd_build(args):
    from build_workbook import build_workbook
    catalog = _load_catalog()
    if args.comp:
        comp_ids = [args.comp]
    elif args.all:
        comp_ids = [c["id"] for c in catalog]
    else:
        print("Specify --comp N or --all", file=sys.stderr); sys.exit(1)
    for comp_id in comp_ids:
        try:
            build_workbook(comp_id)
        except Exception as e:
            print(f"  ERROR build({comp_id}): {e}", file=sys.stderr)


def cmd_refresh(args):
    from scraper import sync_catalog
    from pipeline import init_db
    from build_workbook import build_workbook
    db = init_db()
    print("Syncing competition catalog...", flush=True)
    sync_catalog()
    comp_ids = _resolve_comps(args.comp, args.active, args.all)
    rebuild = getattr(args, "rebuild", False)
    # When a single competition is requested explicitly (--comp), always build
    # the workbook at the end regardless of whether new matches were found.
    # When running --active/--all (nightly job), only build if data changed.
    always_build = args.comp is not None
    fetched_by_comp = _do_fetch(comp_ids, db, rebuild=rebuild)
    db.close()
    rebuilt = []
    for comp_id, fetched in fetched_by_comp.items():
        if fetched > 0 or rebuild or always_build:
            try:
                build_workbook(comp_id)
                rebuilt.append(comp_id)
            except Exception as e:
                print(f"  ERROR build({comp_id}): {e}", file=sys.stderr)
        else:
            print(f"  [{comp_id}] No new matches — skipping build")
    if rebuilt:
        print(f"\nRebuilt workbooks: {rebuilt}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sync-catalog", help="Update competitions catalog")

    def _add_comp_args(p, rebuild=False):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--comp", type=int, metavar="N")
        g.add_argument("--active", action="store_true")
        g.add_argument("--all",    action="store_true")
        if rebuild:
            p.add_argument("--rebuild", action="store_true",
                           help="Discard stored data and re-fetch from scratch")

    p = sub.add_parser("sync-matches"); _add_comp_args(p)
    p = sub.add_parser("fetch");        _add_comp_args(p, rebuild=True)
    p = sub.add_parser("refresh");      _add_comp_args(p, rebuild=True)

    pb = sub.add_parser("build")
    g  = pb.add_mutually_exclusive_group()
    g.add_argument("--comp", type=int, metavar="N")
    g.add_argument("--all",  action="store_true")

    args = ap.parse_args()
    {
        "sync-catalog": cmd_sync_catalog,
        "sync-matches": cmd_sync_matches,
        "fetch":        cmd_fetch,
        "build":        cmd_build,
        "refresh":      cmd_refresh,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
