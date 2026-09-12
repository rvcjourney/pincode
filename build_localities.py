#!/usr/bin/env python3
"""
Derive a place-name -> PIN locality layer from the post office names we already
hold, so the app can resolve "Viman Nagar" instead of demanding 411014.

    python build_localities.py                 # uses $CK_DB_URL
    python build_localities.py --db postgresql://...
    python build_localities.py --dry-run       # show what would load

WHY THIS COMES FIRST
--------------------
India Post names its offices after neighbourhoods, so the authoritative office
layer already carries most urban locality names: Viman Nagar, Vadgaon Sheri,
HSR Layout, Koramangala, Whitefield, Powai are all office names in the master
today. Deriving the layer from them costs nothing, needs no new download, and
stays entirely under GODL - no ODbL share-alike question to answer.

Use this before reaching for OpenStreetMap. Run it, then check what is actually
still missing for your markets; the residual gap (informal colony names with no
post office of their own, e.g. Kharadi in Pune) is much smaller than it looks.

Idempotent: re-running upserts on locality_key and never duplicates.
"""
import argparse, os, re, sys
from datetime import datetime, timezone

import ckdb
import fuzzy
# one definition, shared with build_db.py's village loader - two writers with
# two different key formulas would silently create duplicate localities
from build_db import locality_key as key_of

HERE = os.path.dirname(os.path.abspath(__file__))
NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")

# Office-name suffixes that denote the office class, not the place.
SUFFIX = re.compile(r"\s*\b(B\.?O|S\.?O|H\.?O|G\.?P\.?O)\.?\s*$", re.I)
# India Post disambiguates duplicate names with a trailing bracket:
#   "Saket S.O (South Delhi)" -> the place is "Saket"
BRACKET = re.compile(r"\s*\([^)]*\)\s*$")
# Names that describe a facility rather than a locality.
NOISE = re.compile(r"^(sorting|rms|tmo|nsh|psd|cso|army|navy|air force)\b", re.I)


def locality_name(office_name):
    """Reduce an office name to the place it is named after. None if unusable."""
    n = (office_name or "").strip()
    for _ in range(2):                    # "Foo S.O (Bar)" needs both passes
        n = BRACKET.sub("", n)
        n = SUFFIX.sub("", n)
    n = re.sub(r"\s+", " ", n).strip(" .,-")
    if len(n) < 3 or not re.search(r"[A-Za-z]", n) or NOISE.match(n):
        return None
    return n


def migrate(db):
    """Bring an existing database up to the locality_key schema in place."""
    cols = db.columns("locality")
    if not cols:
        return                                   # fresh DB; schema.sql covers it
    if "locality_key" not in cols:
        print("[i] migrating locality: adding locality_key", flush=True)
        db.execute("ALTER TABLE locality ADD COLUMN locality_key TEXT")
        for r in db.rows("SELECT locality_id, locality_name, lgd_code, district_id "
                         "FROM locality"):
            db.execute("UPDATE locality SET locality_key=? WHERE locality_id=?",
                       (key_of(r["locality_name"], r["lgd_code"], r["district_id"]),
                        r["locality_id"]))
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_locality_key "
                   "ON locality(locality_key)")
    # Add any column this version expects but an older build lacks. Checked
    # one at a time on purpose: nesting them meant name_lower was only added
    # when fold_key was also missing, so a database midway through the upgrades
    # never got it.
    for col, decl in (("source_id", "TEXT"), ("name_lower", "TEXT"),
                      ("fold_key", "TEXT"), ("skel_key", "TEXT")):
        if col not in cols:
            print(f"[i] migrating locality: adding {col}", flush=True)
            db.execute(f"ALTER TABLE locality ADD COLUMN {col} {decl}")

    # backfill the search keys wherever they are missing
    todo = db.rows("""SELECT locality_id, locality_name FROM locality
                      WHERE name_lower IS NULL OR fold_key IS NULL OR skel_key IS NULL""")
    if todo:
        print(f"[i] computing search keys for {len(todo):,} localities", flush=True)
        db.executemany("UPDATE locality SET name_lower=?, fold_key=?, skel_key=? "
                       "WHERE locality_id=?",
                       [((r["locality_name"] or "").lower(),
                         fuzzy.fold(r["locality_name"]),
                         fuzzy.skeleton(r["locality_name"]),
                         r["locality_id"]) for r in todo])

    # Drop duplicate OPEN links before ux_locpin_current can be created. These
    # come from reloads made while valid_from was the only thing separating
    # rows; keep the earliest, which preserves the true first_seen.
    dupes = db.q("""SELECT COUNT(*) FROM locality_pincode lp WHERE lp.valid_to IS NULL
                    AND lp.valid_from > (SELECT MIN(x.valid_from) FROM locality_pincode x
                                         WHERE x.locality_id = lp.locality_id
                                           AND x.pincode = lp.pincode
                                           AND x.valid_to IS NULL)""")
    if dupes:
        print(f"[i] removing {dupes:,} duplicate locality-PIN links", flush=True)
        db.execute("""DELETE FROM locality_pincode WHERE valid_to IS NULL
                      AND valid_from > (SELECT MIN(x.valid_from) FROM locality_pincode x
                                        WHERE x.locality_id = locality_pincode.locality_id
                                          AND x.pincode = locality_pincode.pincode
                                          AND x.valid_to IS NULL)""")
    db.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="CK_DB_URL override")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    url = a.db or os.environ.get("CK_DB_URL") or ckdb.DEFAULT_URL
    print(f"[i] target: {ckdb.describe(url)}", flush=True)
    db = ckdb.connect(url)

    # Migrate BEFORE applying schema.sql: CREATE TABLE IF NOT EXISTS leaves an
    # existing table alone, so the new UNIQUE INDEX on locality_key would fail
    # against a database built before that column existed.
    migrate(db)
    with open(os.path.join(HERE, "schema.sql"), encoding="utf-8") as f:
        db.executescript(f.read())

    offices = db.rows("""SELECT office_name, pincode, district_id, district_raw, state_raw
                         FROM post_office WHERE valid_to IS NULL""")
    if not offices:
        sys.exit("[!] no current offices - run build_db.py first")

    # name -> place, deduped on (place, district); one place can serve many PINs
    locs, links, skipped = {}, set(), 0
    for o in offices:
        name = locality_name(o["office_name"])
        if not name:
            skipped += 1
            continue
        k = key_of(name, "", o["district_id"])
        locs[k] = (k, name, "office_name", "", o["district_id"], "datagov_directory",
                   name.lower(), fuzzy.fold(name), fuzzy.skeleton(name))
        links.add((k, o["pincode"]))

    print(f"[i] {len(offices):,} offices -> {len(locs):,} distinct localities, "
          f"{len(links):,} locality-PIN links ({skipped:,} names skipped)", flush=True)

    if a.dry_run:
        print("\n  sample:")
        for k in list(locs)[:10]:
            print(f"    {locs[k][1]}")
        print("[i] dry run, nothing written")
        db.close()
        return

    snap = db.insert_returning_id(
        "INSERT INTO snapshot (source_id, fetched_at, row_count, notes) VALUES (?,?,?,?)",
        ("datagov_directory", NOW, len(locs), "localities derived from office names"),
        "snapshot_id")

    db.executemany(
        """INSERT INTO locality (locality_key, locality_name, locality_type, lgd_code,
                                 district_id, source_id, name_lower, fold_key, skel_key)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT (locality_key) DO UPDATE SET
             locality_name = excluded.locality_name,
             locality_type = excluded.locality_type,
             district_id   = excluded.district_id,
             name_lower    = excluded.name_lower,
             fold_key      = excluded.fold_key,
             skel_key      = excluded.skel_key""",
        list(locs.values()))

    idmap = {r["locality_key"]: r["locality_id"]
             for r in db.rows("SELECT locality_key, locality_id FROM locality")}
    db.executemany(
        # conflict target is the partial index on OPEN links - not the PK, which
        # includes valid_from and so never collides across runs
        """INSERT INTO locality_pincode (locality_id, pincode, source_id, snapshot_id,
                                         valid_from, valid_to)
           VALUES (?,?,?,?,?,NULL)
           ON CONFLICT (locality_id, pincode) WHERE valid_to IS NULL DO NOTHING""",
        [(idmap[k], pin, "datagov_directory", snap, NOW) for k, pin in sorted(links)
         if k in idmap])
    db.commit()

    n_loc = db.q("SELECT COUNT(*) FROM locality")
    n_lnk = db.q("SELECT COUNT(*) FROM locality_pincode WHERE valid_to IS NULL")
    n_pin = db.q("SELECT COUNT(DISTINCT pincode) FROM locality_pincode WHERE valid_to IS NULL")
    print(f"[done] locality {n_loc:,} | links {n_lnk:,} | PINs covered {n_pin:,}")
    db.close()


if __name__ == "__main__":
    main()
