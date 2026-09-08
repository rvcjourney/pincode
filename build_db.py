#!/usr/bin/env python3
"""
Step 3: build / load the CK PIN code master.

    python build_db.py                          # upsert into $CK_DB_URL
    python build_db.py --db postgresql://...    # explicit target
    python build_db.py --close-missing          # full-snapshot semantics

IDEMPOTENT BY DEFAULT. Re-running upserts; it never drops, never deletes and
never recreates the database. The original build called os.remove(db) on every
run, which destroyed change_log and every valid_to closure - i.e. the entire
audit trail - each time the README's own instructions were followed. Wiping is
now opt-in via --fresh and requires --yes-wipe as well.

Sources (all optional; the official JSONL wins over the bulk CSV):
    raw/postoffices.jsonl     data.gov.in pull      (preferred)
    raw/mirror_pincode.csv    bulk snapshot         (fallback)
    out/pincode_geo.csv       centroids + bboxes
    out/pincode_boundaries.simplified.geojson
    raw/lgd_villages.jsonl    locality layer        (optional)
"""
import argparse, csv, hashlib, json, os, re, sys
from datetime import datetime, timezone

import ckdb
import rollup

HERE = os.path.dirname(os.path.abspath(__file__))
NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")

SOURCES = [
    ("datagov_directory", "Department of Posts / data.gov.in",
     "All India Pincode Directory till last month",
     "5c2f62fe-5afa-4119-a499-fec9d604d5bd",
     "https://www.data.gov.in/resource/all-india-pincode-directory-till-last-month",
     "Government Open Data Licence - India (GODL)", 1),
    ("bulk_snapshot", "Community mirror of Department of Posts directory",
     "All India Pincode Directory (bulk CSV snapshot)", None,
     "https://github.com/saravanakumargn/All-India-Pincode-Directory",
     "Derived from GODL data; treat as non-authoritative", 0),
    ("pincode_boundary", "Community geospatial compilation of India Post PIN areas",
     "India PIN code boundary polygons + area", None,
     "https://github.com/er-data-storage/postal-code-data",
     "Open data; verify before regulatory use", 0),
    ("lgd_villages", "Ministry of Panchayati Raj / data.gov.in",
     "Local Government Directory (LGD) - Villages with PIN Codes",
     "f17a1608-5f10-4610-bb50-a63c80d83974",
     "https://www.data.gov.in/", "GODL", 1),
    ("lgd_local_bodies", "Ministry of Panchayati Raj / data.gov.in",
     "Local Government Directory (LGD) - Local Bodies with PIN Codes",
     "71818d1a-c114-46cb-aa9b-56ed70d4bc4a",
     "https://www.data.gov.in/", "GODL", 1),
]

UTS = {"Andaman & Nicobar Islands", "Chandigarh",
       "Dadra & Nagar Haveli And Daman & Diu", "Delhi", "Jammu & Kashmir",
       "Ladakh", "Lakshadweep", "Puducherry"}

STATE_FIX = {
    "ANDAMAN & NICOBAR ISLANDS": "Andaman & Nicobar Islands",
    "ANDAMAN AND NICOBAR ISLANDS": "Andaman & Nicobar Islands",
    "DADRA & NAGAR HAVELI": "Dadra & Nagar Haveli And Daman & Diu",
    "DAMAN & DIU": "Dadra & Nagar Haveli And Daman & Diu",
    "DADRA AND NAGAR HAVELI AND DAMAN AND DIU": "Dadra & Nagar Haveli And Daman & Diu",
    "DELHI": "Delhi", "NCT OF DELHI": "Delhi",
    "JAMMU AND KASHMIR": "Jammu & Kashmir", "JAMMU & KASHMIR": "Jammu & Kashmir",
    "ORISSA": "Odisha", "PONDICHERRY": "Puducherry",
    "UTTARANCHAL": "Uttarakhand", "TAMILNADU": "Tamil Nadu",
}

OFFICE_TYPE = {"B.O": "BO", "S.O": "SO", "H.O": "HO", "BO": "BO", "SO": "SO", "HO": "HO",
               "GPO": "HO", "G.P.O": "HO"}

# Mainland + islands bounding box, used only to warn about swapped lat/lon.
INDIA_BBOX = (6.0, 68.0, 37.5, 97.5)


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def title(s):
    if not s:
        return None
    s = re.sub(r"\s+", " ", str(s)).strip()
    s = s.replace(" AND ", " & ").replace(" and ", " & ")
    out = []
    for w in s.split(" "):
        out.append(w if (len(w) <= 3 and w.isupper() and w not in ("AND",)) else w.capitalize())
    return " ".join(out)


def canon_state(raw):
    if not raw:
        return None
    k = re.sub(r"\s+", " ", str(raw)).strip().upper()
    k = k.replace(" CIRCLE", "")
    return STATE_FIX.get(k) or title(k)


def norm_office(name):
    """Office name with the B.O/S.O/H.O suffix stripped. Used for fuzzy display
    grouping only - NOT for identity. See office_key()."""
    n = re.sub(r"\s+", " ", (name or "")).strip().upper()
    n = re.sub(r"\b(B\.?O|S\.?O|H\.?O|G\.?P\.?O)\.?$", "", n).strip()
    return re.sub(r"[^A-Z0-9]+", "", n)


def office_key(name, pincode):
    """Stable identity for one post office.

    The original stripped the B.O/S.O/H.O suffix before keying, so 'Airoli S.O'
    and 'Airoli B.O' - genuinely different offices sharing a PIN - collapsed to
    the same key and INSERT OR IGNORE silently dropped one of them. 16 offices
    disappeared that way. The suffix is part of the identity and is kept.
    """
    return f"{re.sub(r'[^A-Z0-9]+', '', (name or '').upper())}|{pincode}"


def _f(v, lo=-90.0, hi=180.0):
    """Parse a coordinate. Returns None for junk, NaN, inf or out-of-range.

    The original read `x if -90 <= x <= 90 or True else None`; the trailing
    `or True` made the bound check unconditional, so '999' and '1e400' passed
    straight through as 999.0 and inf.
    """
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return x if lo <= x <= hi else None


def _lat(v):
    return _f(v, -90.0, 90.0)


def _lon(v):
    return _f(v, -180.0, 180.0)


def read_offices(jsonl, csvp):
    """Yield (source_id, row-dict). The official JSONL wins over the bulk CSV."""
    if jsonl and os.path.exists(jsonl) and os.path.getsize(jsonl) > 0:
        with open(jsonl, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                yield "datagov_directory", {
                    "office_name": (r.get("officename") or "").strip(),
                    "pincode": str(r.get("pincode") or "").strip().split(".")[0].zfill(6),
                    "office_type": OFFICE_TYPE.get((r.get("officetype") or "").upper().strip()),
                    "delivery_status": r.get("delivery") or r.get("deliverystatus"),
                    "circle_name": r.get("circlename"), "region_name": r.get("regionname"),
                    "division_name": r.get("divisionname"), "taluk": title(r.get("taluk")),
                    "district_raw": title(r.get("district") or r.get("districtname")),
                    "state_raw": canon_state(r.get("statename")),
                    "telephone": None, "related_so": None, "related_ho": None,
                    "latitude": _lat(r.get("latitude")), "longitude": _lon(r.get("longitude")),
                }
        return
    if not (csvp and os.path.exists(csvp)):
        sys.exit("[!] no office source found - run tools/bootstrap_raw.py or "
                 "fetch_datagov.py first")
    with open(csvp, encoding="utf-8", errors="replace", newline="") as f:
        for r in csv.DictReader(f):
            tel = (r.get("Telephone") or "").strip()
            yield "bulk_snapshot", {
                "office_name": (r.get("officename") or "").strip(),
                "pincode": str(r.get("pincode") or "").strip().split(".")[0].zfill(6),
                "office_type": OFFICE_TYPE.get((r.get("officeType") or "").upper().strip()),
                "delivery_status": r.get("Deliverystatus"),
                "circle_name": r.get("circlename"), "region_name": r.get("regionname"),
                "division_name": r.get("divisionname"), "taluk": title(r.get("Taluk")),
                "district_raw": title(r.get("Districtname")),
                "state_raw": canon_state(r.get("statename")),
                "telephone": None if tel in ("", "NA") else tel,
                "related_so": r.get("RelatedSuboffice") or None,
                "related_ho": r.get("RelatedHeadoffice") or None,
                "latitude": None, "longitude": None,
            }


def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


# --------------------------------------------------------------------- loaders
def load_sources(db):
    db.executemany(
        """INSERT INTO source (source_id, publisher, dataset_title, resource_id, url,
                               licence, authoritative)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT (source_id) DO UPDATE SET
             publisher=excluded.publisher, dataset_title=excluded.dataset_title,
             resource_id=excluded.resource_id, url=excluded.url,
             licence=excluded.licence, authoritative=excluded.authoritative""",
        SOURCES)


def dedupe(rows):
    """Collapse input rows that share an office_key, loudly.

    Never silent: the original dropped collisions via INSERT OR IGNORE and the
    rollup then counted them anyway, so the totals disagreed with the table.
    """
    seen, out, collisions = {}, [], []
    for src, r in rows:
        k = office_key(r["office_name"], r["pincode"])
        if k in seen:
            collisions.append((k, seen[k]["office_name"], r["office_name"]))
            continue
        seen[k] = r
        out.append((k, src, r))
    if collisions:
        print(f"[!] {len(collisions)} duplicate office_key(s) collapsed:", flush=True)
        for k, a, b in collisions[:10]:
            print(f"      {k}  kept {a!r}  dropped {b!r}", flush=True)
        if len(collisions) > 10:
            print(f"      ... and {len(collisions)-10} more", flush=True)
    return out, collisions


def ensure_dimensions(db, office_rows):
    """Insert any state / district / alias the source introduces, and return the
    (state_name, district_name) -> district_id map.

    refresh.py must call this too. The original refresh never did, so offices
    added by a monthly refresh were inserted with district_id NULL and any newly
    created district never reached the `district` table at all.
    """
    states = {}
    for r in office_rows:
        if r["state_raw"]:
            states[r["state_raw"]] = "UT" if r["state_raw"] in UTS else "State"
    # bare DO NOTHING: state has a UNIQUE on state_name as well as the PK
    db.executemany("INSERT INTO state (state_code, state_name, state_type) VALUES (?,?,?) "
                   "ON CONFLICT DO NOTHING",
                   [(slug(s), s, t) for s, t in sorted(states.items())])

    dists = {(r["state_raw"], r["district_raw"]) for r in office_rows
             if r["state_raw"] and r["district_raw"]}
    db.executemany("INSERT INTO district (state_code, district_name) VALUES (?,?) "
                   "ON CONFLICT (state_code, district_name) DO NOTHING",
                   [(slug(s), d) for s, d in sorted(dists)])

    alias = set()
    for r in office_rows:
        if r["state_raw"]:
            alias.add(("state", r["state_raw"].upper(), r["state_raw"], None))
        if r["district_raw"] and r["state_raw"]:
            alias.add(("district", r["district_raw"].upper(), r["district_raw"],
                       slug(r["state_raw"])))
    # state_code is part of the PK and NULL never equals NULL in a unique index,
    # so the state rows are inserted with an explicit sentinel instead.
    db.executemany("INSERT INTO name_alias (entity, name_raw, name_canonical, state_code) "
                   "VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                   sorted((e, n, c, sc or "-") for e, n, c, sc in alias))
    return district_map(db)


def load_offices(db, rows, snap_id, close_missing):
    """Upsert current offices. Optionally close the ones absent from the source."""
    keyed, _ = dedupe(rows)
    dmap = ensure_dimensions(db, [r for _, _, r in keyed])

    warn = 0
    for _, _, r in keyed:
        la, lo = r["latitude"], r["longitude"]
        if la is not None and lo is not None:
            if not (INDIA_BBOX[0] <= la <= INDIA_BBOX[2]
                    and INDIA_BBOX[1] <= lo <= INDIA_BBOX[3]):
                warn += 1
    if warn:
        print(f"[!] {warn} office coordinate(s) fall outside India - possible "
              f"lat/lon swap in the source", flush=True)

    db.executemany(
        """INSERT INTO post_office
             (office_key, office_name, pincode, office_type, delivery_status, circle_name,
              region_name, division_name, taluk, district_id, district_raw, state_raw,
              telephone, related_so, related_ho, latitude, longitude,
              source_id, snapshot_id, valid_from)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (office_key) WHERE valid_to IS NULL DO UPDATE SET
             office_name=excluded.office_name, office_type=excluded.office_type,
             delivery_status=excluded.delivery_status, circle_name=excluded.circle_name,
             region_name=excluded.region_name, division_name=excluded.division_name,
             taluk=excluded.taluk, district_id=excluded.district_id,
             district_raw=excluded.district_raw, state_raw=excluded.state_raw,
             telephone=excluded.telephone, related_so=excluded.related_so,
             related_ho=excluded.related_ho, latitude=excluded.latitude,
             longitude=excluded.longitude, source_id=excluded.source_id,
             snapshot_id=excluded.snapshot_id""",
        [(k, r["office_name"], r["pincode"], r["office_type"], r["delivery_status"],
          r["circle_name"], r["region_name"], r["division_name"], r["taluk"],
          dmap.get((r["state_raw"], r["district_raw"])), r["district_raw"], r["state_raw"],
          r["telephone"], r["related_so"], r["related_ho"], r["latitude"], r["longitude"],
          src, snap_id, NOW) for k, src, r in keyed])

    if close_missing:
        live = {k for k, _, _ in keyed}
        current = {r["office_key"] for r in db.rows(
            "SELECT office_key FROM post_office WHERE valid_to IS NULL")}
        stale = sorted(current - live)
        if stale and len(stale) > 0.02 * max(len(current), 1):
            sys.exit(f"[!] --close-missing would close {len(stale):,} of {len(current):,} "
                     f"offices (>2%). Refusing; this looks like a truncated source.")
        db.executemany("UPDATE post_office SET valid_to=? WHERE office_key=? "
                       "AND valid_to IS NULL", [(NOW[:10], k) for k in stale])
        if stale:
            print(f"[i] closed {len(stale):,} offices absent from this source", flush=True)

    return len(keyed)


def district_map(db):
    return {(r["state_name"], r["district_name"]): r["district_id"] for r in db.rows(
        "SELECT d.district_id, s.state_name, d.district_name "
        "FROM district d JOIN state s ON s.state_code = d.state_code")}


def load_geometry(db, geo_path, bnd_path):
    if not os.path.exists(geo_path):
        print("[i] no pincode_geo.csv - skipping geometry", flush=True)
        return 0
    snap = db.insert_returning_id(
        "INSERT INTO snapshot (source_id, fetched_at, sha256, notes) VALUES (?,?,?,?)",
        ("pincode_boundary", NOW, sha_file(geo_path), os.path.basename(geo_path)),
        "snapshot_id")
    with open(geo_path, encoding="utf-8", newline="") as f:
        geo = list(csv.DictReader(f))

    # PINs known only from the boundary file are real but retired: no live office.
    db.executemany(
        """INSERT INTO pincode (pincode, postal_zone, sub_zone, sorting_district,
                                is_army_postal, status, first_seen, last_seen)
           VALUES (?,?,?,?,?, 'retired', ?, ?) ON CONFLICT (pincode) DO NOTHING""",
        [(g["pincode"], g["pincode"][:1], g["pincode"][:2], g["pincode"][:3],
          1 if g["pincode"][:1] == "9" else 0, NOW, NOW) for g in geo])

    db.executemany(
        """UPDATE pincode SET centroid_lat=?, centroid_lon=?, min_lat=?, min_lon=?,
             max_lat=?, max_lon=?, area_sqkm=?, has_boundary=1 WHERE pincode=?""",
        [(_lat(g["centroid_lat"]), _lon(g["centroid_lon"]), _lat(g["min_lat"]),
          _lon(g["min_lon"]), _lat(g["max_lat"]), _lon(g["max_lon"]),
          _f(g["area_sqkm"], 0, 1e9) if g["area_sqkm"] else None, g["pincode"])
         for g in geo])
    print(f"[i] geometry attached for {len(geo):,} pincodes", flush=True)

    if os.path.exists(bnd_path):
        with open(bnd_path, encoding="utf-8") as f:
            gj = json.load(f)
        db.executemany(
            """INSERT INTO pincode_boundary (pincode, geojson, n_parts, source_id, snapshot_id)
               VALUES (?,?,?,?,?)
               ON CONFLICT (pincode) DO UPDATE SET
                 geojson=excluded.geojson, n_parts=excluded.n_parts,
                 source_id=excluded.source_id, snapshot_id=excluded.snapshot_id""",
            [(f_["properties"]["pincode"], json.dumps(f_["geometry"]),
              len(f_["geometry"].get("coordinates", [])), "pincode_boundary", snap)
             for f_ in gj["features"]])
        print(f"[i] {len(gj['features']):,} polygons stored", flush=True)
    return len(geo)


def fill_missing_centroids(db):
    """Mean office coordinate for PINs with no polygon. Only helps when the
    office source actually carries lat/long (the bulk snapshot does not)."""
    rows = db.rows("""SELECT p.pincode, AVG(o.latitude) la, AVG(o.longitude) lo
                      FROM pincode p JOIN post_office o ON o.pincode = p.pincode
                      WHERE p.centroid_lat IS NULL AND o.valid_to IS NULL
                        AND o.latitude IS NOT NULL AND o.longitude IS NOT NULL
                      GROUP BY p.pincode""")
    if rows:
        db.executemany("UPDATE pincode SET centroid_lat=?, centroid_lon=? WHERE pincode=?",
                       [(r["la"], r["lo"], r["pincode"]) for r in rows])
    print(f"[i] centroid fallback from office coords: {len(rows):,} pincodes", flush=True)
    return len(rows)


def load_villages(db, path):
    if not (os.path.exists(path) and os.path.getsize(path) > 0):
        return 0
    snap = db.insert_returning_id(
        "INSERT INTO snapshot (source_id, fetched_at, notes) VALUES (?,?,?)",
        ("lgd_villages", NOW, os.path.basename(path)), "snapshot_id")
    dmap, n, batch = district_map(db), 0, []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            pin = str(r.get("pincode") or "").split(".")[0].zfill(6)
            name = r.get("villageNameEnglish")
            if not name or not pin.isdigit() or len(pin) != 6:
                continue
            batch.append((title(name), "village", str(r.get("villageCode") or ""),
                          dmap.get((canon_state(r.get("stateNameEnglish")),
                                    title(r.get("districtNameEnglish")))), pin))
            if len(batch) >= 20000:
                n += _load_loc(db, batch, snap)
                batch = []
    n += _load_loc(db, batch, snap)
    print(f"[i] localities linked: {n:,}", flush=True)
    return n


def _load_loc(db, batch, snap):
    if not batch:
        return 0
    db.executemany("INSERT INTO locality (locality_name, locality_type, lgd_code, district_id) "
                   "VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                   [(b[0], b[1], b[2], b[3]) for b in batch])
    # key on (name, lgd_code, district_id) - the full uniqueness tuple. Keying on
    # (name, lgd_code) alone mislinked villages whose lgd_code was blank.
    lmap, keys = {}, sorted({(b[0], b[2], b[3]) for b in batch})
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        ph = ",".join("?" for _ in chunk)
        for r in db.rows(
                f"SELECT locality_id, locality_name, lgd_code, district_id FROM locality "
                f"WHERE locality_name IN ({ph})", [c[0] for c in chunk]):
            lmap[(r["locality_name"], r["lgd_code"] or "", r["district_id"])] = r["locality_id"]
    db.executemany(
        "INSERT INTO locality_pincode (locality_id, pincode, source_id, snapshot_id, "
        "valid_from, valid_to) VALUES (?,?,?,?,?,NULL) ON CONFLICT DO NOTHING",
        [(lmap[(b[0], b[2], b[3])], b[4], "lgd_villages", snap, NOW)
         for b in batch if (b[0], b[2], b[3]) in lmap])
    return len(batch)


# ------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="CK_DB_URL override")
    ap.add_argument("--offices", default=os.path.join(HERE, "raw", "postoffices.jsonl"))
    ap.add_argument("--offices-csv", default=os.path.join(HERE, "raw", "mirror_pincode.csv"))
    ap.add_argument("--geo", default=os.path.join(HERE, "out", "pincode_geo.csv"))
    ap.add_argument("--boundaries",
                    default=os.path.join(HERE, "out", "pincode_boundaries.simplified.geojson"))
    ap.add_argument("--villages", default=os.path.join(HERE, "raw", "lgd_villages.jsonl"))
    ap.add_argument("--close-missing", action="store_true",
                    help="close offices absent from this source (full-snapshot semantics)")
    ap.add_argument("--fresh", action="store_true", help="DESTRUCTIVE: drop all data first")
    ap.add_argument("--yes-wipe", action="store_true", help="required alongside --fresh")
    a = ap.parse_args()

    url = a.db or os.environ.get("CK_DB_URL") or ckdb.DEFAULT_URL
    print(f"[i] target: {ckdb.describe(url)}", flush=True)
    db = ckdb.connect(url)

    if a.fresh:
        if not a.yes_wipe:
            sys.exit("[!] --fresh destroys change_log and all valid_to history.\n"
                     "    Re-run with --fresh --yes-wipe if you really mean it.")
        print("[!] --fresh: dropping all CK tables", flush=True)
        for t in ["locality_pincode", "locality", "change_log", "pincode_boundary",
                  "pincode", "post_office", "name_alias", "district", "state",
                  "snapshot", "source"]:
            db.execute(f"DROP TABLE IF EXISTS {t} CASCADE" if db.is_pg
                       else f"DROP TABLE IF EXISTS {t}")
        for v in ["v_pincode_lookup", "v_pincode_offices", "v_coverage_gaps"]:
            db.execute(f"DROP VIEW IF EXISTS {v}")
        db.commit()

    with open(os.path.join(HERE, "schema.sql"), encoding="utf-8") as f:
        db.executescript(f.read())
    load_sources(db)

    rows = list(read_offices(a.offices, a.offices_csv))
    if not rows:
        sys.exit("[!] zero office rows in source")
    off_src = rows[0][0]
    raw_path = a.offices if off_src == "datagov_directory" else a.offices_csv
    snap_off = db.insert_returning_id(
        "INSERT INTO snapshot (source_id, fetched_at, row_count, sha256, notes) "
        "VALUES (?,?,?,?,?)",
        (off_src, NOW, len(rows), sha_file(raw_path), os.path.basename(raw_path)),
        "snapshot_id")
    print(f"[i] offices: {len(rows):,} rows from {off_src}", flush=True)

    n_off = load_offices(db, rows, snap_off, a.close_missing)
    db.commit()

    rollup.recompute(db, NOW, snapshot_id=snap_off)
    db.commit()

    load_geometry(db, a.geo, a.boundaries)
    fill_missing_centroids(db)
    load_villages(db, a.villages)
    db.commit()

    report(db)
    db.close()
    print(f"[done] {n_off:,} offices loaded into {ckdb.describe(url)}")


def report(db):
    print("\n=== CK PIN code master ===")
    stats = [
        ("post offices (current)", "SELECT COUNT(*) FROM post_office WHERE valid_to IS NULL"),
        ("distinct PIN codes", "SELECT COUNT(*) FROM pincode"),
        ("  active (has office)", "SELECT COUNT(*) FROM pincode WHERE status='active'"),
        ("  deliverable", "SELECT COUNT(*) FROM pincode WHERE is_deliverable=1"),
        ("  with polygon", "SELECT COUNT(*) FROM pincode WHERE has_boundary=1"),
        ("  with centroid", "SELECT COUNT(*) FROM pincode WHERE centroid_lat IS NOT NULL"),
        ("  straddle >1 district", "SELECT COUNT(*) FROM pincode WHERE n_districts>1"),
        ("  straddle >1 state", "SELECT COUNT(*) FROM pincode WHERE n_states>1"),
        ("  army postal (zone 9)", "SELECT COUNT(*) FROM pincode WHERE is_army_postal=1"),
        ("states / UTs", "SELECT COUNT(*) FROM state"),
        ("districts", "SELECT COUNT(*) FROM district"),
        ("localities", "SELECT COUNT(*) FROM locality"),
        ("coverage gaps", "SELECT COUNT(*) FROM v_coverage_gaps"),
        ("change_log rows", "SELECT COUNT(*) FROM change_log"),
    ]
    for label, sql in stats:
        print(f"  {label:24s}: {db.q(sql):,}")


if __name__ == "__main__":
    main()
