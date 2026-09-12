#!/usr/bin/env python3
"""
Step 5: emit app-ready flat files + a QA summary from the master DB.

    python export.py                      # uses $CK_DB_URL
    python export.py --db postgresql://...
"""
import argparse, csv, json, os, re

import ckdb

HERE = os.path.dirname(os.path.abspath(__file__))

QUERIES = {
    "pincode_master.csv": """
        SELECT pincode, postal_zone, sub_zone, sorting_district,
               primary_office, primary_district, primary_state,
               n_offices, n_delivery, is_deliverable, n_districts, n_states,
               centroid_lat, centroid_lon, min_lat, min_lon, max_lat, max_lon,
               area_sqkm, has_boundary, is_army_postal, status
        FROM pincode ORDER BY pincode""",
    "post_offices.csv": """
        SELECT office_key, office_name, pincode, office_type, delivery_status,
               circle_name, region_name, division_name, taluk,
               district_raw AS district, state_raw AS state,
               telephone, related_so, related_ho, latitude, longitude,
               valid_from, valid_to
        FROM post_office WHERE valid_to IS NULL ORDER BY pincode, office_name""",
    "pincode_centroids.csv": """
        SELECT pincode, centroid_lat, centroid_lon, area_sqkm
        FROM pincode WHERE centroid_lat IS NOT NULL ORDER BY pincode""",
    "states.csv": "SELECT state_code, state_name, state_type FROM state ORDER BY state_name",
    "districts.csv": """
        SELECT s.state_name, d.district_name, d.district_id
        FROM district d JOIN state s ON s.state_code = d.state_code
        ORDER BY s.state_name, d.district_name""",
    "pincode_district_map.csv": """
        SELECT DISTINCT pincode, district_raw AS district, state_raw AS state
        FROM post_office WHERE valid_to IS NULL AND district_raw IS NOT NULL
        ORDER BY pincode, district""",
    "qa_multi_district_pincodes.csv": """
        SELECT pincode, primary_district, primary_state, n_districts, n_states, n_offices
        FROM pincode WHERE n_districts > 1 ORDER BY n_districts DESC, pincode""",
    "qa_coverage_gaps.csv": "SELECT * FROM v_coverage_gaps ORDER BY pincode",
    # place-name -> PIN. source_id travels with each row because licences differ:
    # GODL (attribution) for office/LGD names, ODbL (share-alike) for any OSM ones.
    "pincode_locality_map.csv": """
        SELECT pincode, locality_name, locality_type, source_id
        FROM v_pincode_localities ORDER BY pincode, locality_name""",
    # closed offices are never deleted; this is the audit view of what went away
    "qa_closed_offices.csv": """
        SELECT office_key, office_name, pincode, district_raw AS district,
               state_raw AS state, valid_from, valid_to
        FROM post_office WHERE valid_to IS NOT NULL ORDER BY valid_to DESC, pincode""",
}


def r4(v):
    """Round a coordinate, preserving an exact 0.0 (a plain truthiness test
    turned a real 0.0 coordinate into null)."""
    return None if v is None else round(v, 4)


def strip_office_suffix(name):
    """Office name -> the place it is named after."""
    n = re.sub(r"\s*\([^)]*\)\s*$", "", (name or "").strip())
    return re.sub(r"\s*\b(B\.?O|S\.?O|H\.?O|G\.?P\.?O)\.?$", "", n).strip()


def format_address(locality, taluk, district, state, pincode):
    """Assemble an Indian postal address, collapsing components that repeat.

    Taluk is often identical to the district (Pune / Pune) or to the locality;
    printing it twice reads as a data error, so equal neighbours are dropped.
    """
    parts, seen = [], set()
    for p in (locality, taluk, district, state):
        if not p:
            continue
        k = p.strip().lower()
        if k and k not in seen:
            seen.add(k)
            parts.append(p.strip())
    return ", ".join(parts) + (" - " + pincode if pincode else "")


def write_full_addresses(db, ex):
    """One complete address line per locality.

    A PIN is not one address: 853204 resolves to 34 localities across 5
    districts and several taluks, so taluk and district differ line to line,
    not just the locality name.
    """
    p = os.path.join(ex, "pincode_full_addresses.csv")
    n = 0
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["pincode", "locality", "taluk", "district", "state",
                    "full_address", "post_office", "office_type", "deliverable",
                    "postal_circle", "postal_division"])
        for r in db.rows("""SELECT pincode, office_name, office_type, delivery_status,
                                   taluk, district_raw, state_raw, circle_name,
                                   division_name
                            FROM post_office WHERE valid_to IS NULL
                            ORDER BY pincode, office_name"""):
            loc = strip_office_suffix(r["office_name"])
            w.writerow([
                r["pincode"], loc, r["taluk"] or "", r["district_raw"] or "",
                r["state_raw"] or "",
                format_address(loc, r["taluk"], r["district_raw"], r["state_raw"],
                               r["pincode"]),
                r["office_name"], r["office_type"] or "",
                1 if (r["delivery_status"] or "").lower().startswith("delivery") else 0,
                r["circle_name"] or "", r["division_name"] or "",
            ])
            n += 1
    print(f"[i] {'pincode_full_addresses.csv':34s} {n:>8,} rows  "
          f"{os.path.getsize(p)/1e6:6.2f} MB")
    return n


def write_locality_lookup(db, ex):
    """One row per PIN with its place names collapsed into a single cell.

    A denormalised convenience view for onboarding autosuggest - NOT the
    storage model. locality_pincode stays many-to-many, because a name in a
    comma-separated cell cannot be indexed, licensed, or expired individually.
    Import this as a read-only lookup table and rebuild it on every refresh.

    Aggregated in Python on purpose: SQLite spells it GROUP_CONCAT and Postgres
    spells it STRING_AGG, and this has to run on both.
    """
    byline = {}
    for r in db.rows("""SELECT pincode, locality_name FROM v_pincode_localities
                        ORDER BY pincode, locality_name"""):
        byline.setdefault(r["pincode"], []).append(r["locality_name"])

    meta = {r["pincode"]: r for r in db.rows(
        "SELECT pincode, primary_district, primary_state, status FROM pincode")}

    p = os.path.join(ex, "pincode_localities.csv")
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["pincode", "primary_district", "primary_state",
                    "locality_names", "total_localities_count", "status"])
        for pin in sorted(byline):
            names = byline[pin]
            m = meta.get(pin, {})
            w.writerow([pin, m.get("primary_district") or "", m.get("primary_state") or "",
                        ", ".join(names), len(names), m.get("status") or ""])
    print(f"[i] {'pincode_localities.csv':34s} {len(byline):>8,} rows  "
          f"{os.path.getsize(p)/1e6:6.2f} MB")
    return len(byline)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="CK_DB_URL override")
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    a = ap.parse_args()

    url = a.db or os.environ.get("CK_DB_URL") or ckdb.DEFAULT_URL
    print(f"[i] source: {ckdb.describe(url)}", flush=True)
    db = ckdb.connect(url)

    ex = os.path.join(a.out, "exports")
    os.makedirs(ex, exist_ok=True)

    for name, sql in QUERIES.items():
        rows = db.rows(sql)
        p = os.path.join(ex, name)
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if rows:
                w.writerow(rows[0].keys())
                w.writerows(r.values() for r in rows)
        print(f"[i] {name:34s} {len(rows):>8,} rows  {os.path.getsize(p)/1e6:6.2f} MB")

    # compact JSON lookup for the app / edge cache
    lut = {r["pincode"]: [r["primary_district"], r["primary_state"], r["is_deliverable"],
                          r4(r["centroid_lat"]), r4(r["centroid_lon"])]
           for r in db.rows("SELECT pincode, primary_district, primary_state, "
                            "is_deliverable, centroid_lat, centroid_lon "
                            "FROM pincode WHERE status = 'active'")}
    p = os.path.join(ex, "pincode_lookup.min.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"schema": ["district", "state", "is_deliverable", "lat", "lon"],
                   "data": lut}, f, separators=(",", ":"))
    print(f"[i] {'pincode_lookup.min.json':34s} {len(lut):>8,} keys  "
          f"{os.path.getsize(p)/1e6:6.2f} MB")

    write_full_addresses(db, ex)
    write_locality_lookup(db, ex)
    write_qa(db, a.out)
    db.close()


def write_qa(db, outdir):
    q = db.q
    L = ["# CK PIN code master - QA summary", "",
         "| Metric | Value |", "|---|---|"]

    n_all = q("SELECT COUNT(*) FROM pincode")
    n_act = q("SELECT COUNT(*) FROM pincode WHERE status = 'active'")
    n_ret = q("SELECT COUNT(*) FROM pincode WHERE status = 'retired'")
    n_del = q("SELECT COUNT(*) FROM pincode WHERE is_deliverable = 1")
    n_off = q("SELECT COUNT(*) FROM post_office WHERE valid_to IS NULL")
    n_cls = q("SELECT COUNT(*) FROM post_office WHERE valid_to IS NOT NULL")
    n_pol = q("SELECT COUNT(*) FROM pincode WHERE has_boundary = 1")
    n_cen = q("SELECT COUNT(*) FROM pincode WHERE centroid_lat IS NOT NULL")
    n_md = q("SELECT COUNT(*) FROM pincode WHERE n_districts > 1")
    n_ms = q("SELECT COUNT(*) FROM pincode WHERE n_states > 1")
    n_st = q("SELECT COUNT(*) FROM state")
    n_di = q("SELECT COUNT(*) FROM district")
    n_lg = q("SELECT COUNT(*) FROM change_log")

    L += [f"| Distinct PIN codes (all) | {n_all:,} |",
          f"| Active PIN codes (have a live post office) | {n_act:,} |",
          f"| Deliverable PIN codes | {n_del:,} |",
          f"| Retired PIN codes (boundary-only / no live office) | {n_ret:,} |",
          f"| Post offices (current) | {n_off:,} |",
          f"| Post offices (closed, retained for audit) | {n_cls:,} |",
          f"| PIN codes with polygon | {n_pol:,} |",
          f"| PIN codes with centroid | {n_cen:,} |",
          f"| PIN codes spanning >1 district | {n_md:,} |",
          f"| PIN codes spanning >1 state | {n_ms:,} |",
          f"| States + UTs | {n_st:,} |",
          f"| Districts | {n_di:,} |",
          f"| change_log rows (all time) | {n_lg:,} |", ""]

    # integrity checks - these must all be zero
    bad_deliv = q("SELECT COUNT(*) FROM pincode WHERE status = 'retired' "
                  "AND (is_deliverable = 1 OR n_offices > 0)")
    bad_roll = q("""SELECT COUNT(*) FROM (
                      SELECT p.pincode FROM pincode p
                      LEFT JOIN post_office o ON o.pincode = p.pincode AND o.valid_to IS NULL
                      GROUP BY p.pincode, p.n_offices
                      HAVING p.n_offices <> COUNT(o.office_id)) x""")
    no_cent = q("SELECT COUNT(*) FROM pincode WHERE status = 'active' AND centroid_lat IS NULL")
    L += ["## Integrity checks", "", "| Check | Count | Expect |", "|---|---|---|",
          f"| Retired PINs still marked deliverable | {bad_deliv:,} | 0 |",
          f"| PINs whose n_offices disagrees with post_office | {bad_roll:,} | 0 |",
          f"| Active PINs with no centroid (radius lookup fails) | {no_cent:,} | investigate |",
          ""]

    L += ["## Post offices by type", "", "| Type | Count |", "|---|---|"]
    for r in db.rows("""SELECT COALESCE(office_type, '(unknown)') t, COUNT(*) n
                        FROM post_office WHERE valid_to IS NULL
                        GROUP BY COALESCE(office_type, '(unknown)') ORDER BY n DESC"""):
        L.append(f"| {r['t']} | {r['n']:,} |")

    # office counts per state, computed separately then joined: the original
    # correlated subquery referenced an ungrouped column and is invalid on Postgres.
    off_by_state = {r["state_raw"]: r["n"] for r in db.rows(
        "SELECT state_raw, COUNT(*) n FROM post_office WHERE valid_to IS NULL "
        "AND state_raw IS NOT NULL GROUP BY state_raw")}
    L += ["", "## Top 15 states by PIN code count", "",
          "| State / UT | PIN codes | Post offices |", "|---|---|---|"]
    for r in db.rows("""SELECT primary_state s, COUNT(*) p FROM pincode
                        WHERE primary_state IS NOT NULL
                        GROUP BY primary_state ORDER BY COUNT(*) DESC LIMIT 15"""):
        L.append(f"| {r['s']} | {r['p']:,} | {off_by_state.get(r['s'], 0):,} |")

    L += ["", "## Worst multi-district PIN codes (address-matching traps)", "",
          "| PIN | Primary district | Districts | States | Offices |", "|---|---|---|---|---|"]
    for r in db.rows("""SELECT pincode, primary_district, n_districts, n_states, n_offices
                        FROM pincode WHERE n_districts > 1
                        ORDER BY n_districts DESC, n_offices DESC LIMIT 15"""):
        L.append(f"| {r['pincode']} | {r['primary_district']} | {r['n_districts']} "
                 f"| {r['n_states']} | {r['n_offices']} |")

    L += ["", "## Provenance", "", "| Source | Dataset | Authoritative | Rows |",
          "|---|---|---|---|"]
    for r in db.rows("""SELECT s.source_id, s.dataset_title, s.authoritative, s.url,
                          (SELECT SUM(n.row_count) FROM snapshot n
                           WHERE n.source_id = s.source_id) rc
                        FROM source s ORDER BY s.authoritative DESC, s.source_id"""):
        rc = f"{r['rc']:,}" if r["rc"] else "-"
        auth = "yes" if r["authoritative"] else "NO"
        L.append(f"| `{r['source_id']}` | [{r['dataset_title']}]({r['url']}) | {auth} | {rc} |")

    p = os.path.join(outdir, "QA_SUMMARY.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print(f"[i] QA summary -> {p}")
    for label, v in (("retired-but-deliverable", bad_deliv), ("rollup mismatches", bad_roll)):
        print(f"[{'i' if v == 0 else '!'}] {label}: {v}")


if __name__ == "__main__":
    main()
