#!/usr/bin/env python3
"""
Build the standalone PIN Code Explorer page by injecting a compact data payload
into web/explorer.html.

    python tools/build_explorer.py            # -> web/explorer.built.html

The template ships with a /*__DATA__*/ placeholder inside its
<script type="application/json"> block; this fills it. Keeping the two separate
means the 1.4 MB payload never has to be hand-edited, and the page can be
rebuilt after any refresh.

State and district names are interned to integer indices, which is what keeps
19,936 PIN codes under the 16 MB artifact ceiling (~1.45 MB built).
"""
import json, os, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import ckdb  # noqa: E402

TEMPLATE = os.path.join(HERE, "web", "explorer.html")
OUT = os.path.join(HERE, "web", "explorer.built.html")
LIMIT = 16 * 1024 * 1024


def build_payload(db):
    states = [r["state_name"] for r in db.rows(
        "SELECT state_name FROM state ORDER BY state_name")]
    si = {s: i for i, s in enumerate(states)}
    districts = sorted({r["district_name"] for r in db.rows(
        "SELECT district_name FROM district")})
    di = {d: i for i, d in enumerate(districts)}

    rows = []
    for p in db.rows("""SELECT pincode, primary_office, primary_district, primary_state,
                               n_offices, n_delivery, is_deliverable, n_districts, n_states,
                               centroid_lat, centroid_lon, area_sqkm, has_boundary, status,
                               is_army_postal
                        FROM pincode ORDER BY pincode"""):
        rows.append([
            p["pincode"], p["primary_office"] or "",
            di.get(p["primary_district"], -1), si.get(p["primary_state"], -1),
            p["n_offices"], p["n_delivery"], p["is_deliverable"],
            p["n_districts"], p["n_states"],
            None if p["centroid_lat"] is None else round(p["centroid_lat"], 4),
            None if p["centroid_lon"] is None else round(p["centroid_lon"], 4),
            None if p["area_sqkm"] is None else round(p["area_sqkm"], 1),
            p["has_boundary"], 1 if p["status"] == "active" else 0, p["is_army_postal"],
        ])

    # which districts each multi-district PIN actually spans - the trap list
    spans = {}
    for r in db.rows("""SELECT DISTINCT pincode, district_raw d, state_raw s
                        FROM post_office
                        WHERE valid_to IS NULL AND district_raw IS NOT NULL
                          AND pincode IN (SELECT pincode FROM pincode WHERE n_districts > 1)"""):
        spans.setdefault(r["pincode"], []).append([di.get(r["d"], -1), si.get(r["s"], -1)])

    meta = {
        "built": db.q("SELECT MAX(fetched_at) FROM snapshot"),
        "offices": db.q("SELECT COUNT(*) FROM post_office WHERE valid_to IS NULL"),
        "sources": db.rows("SELECT source_id, dataset_title, url, authoritative "
                           "FROM source ORDER BY authoritative DESC, source_id"),
        "by_type": db.rows("""SELECT COALESCE(office_type,'(unknown)') t, COUNT(*) n
                              FROM post_office WHERE valid_to IS NULL
                              GROUP BY COALESCE(office_type,'(unknown)') ORDER BY n DESC"""),
        "off_by_state": {r["state_raw"]: r["n"] for r in db.rows(
            "SELECT state_raw, COUNT(*) n FROM post_office "
            "WHERE valid_to IS NULL AND state_raw IS NOT NULL GROUP BY state_raw")},
        "gaps": db.q("SELECT COUNT(*) FROM v_coverage_gaps"),
    }
    return {"schema": ["pin", "office", "d", "s", "n_off", "n_del", "deliv", "n_dist",
                       "n_st", "lat", "lon", "area", "geo", "active", "army"],
            "states": states, "districts": districts,
            "rows": rows, "spans": spans, "meta": meta}


def main():
    db = ckdb.connect(sys.argv[1] if len(sys.argv) > 1 else None)
    payload = build_payload(db)
    db.close()

    data = json.dumps(payload, separators=(",", ":"))
    # the payload lives inside a <script> block, so it must not be able to close it
    if "</" in data:
        sys.exit("[!] payload contains '</' and would terminate the script tag")

    with open(TEMPLATE, encoding="utf-8") as f:
        tmpl = f.read()
    if "/*__DATA__*/" not in tmpl:
        sys.exit(f"[!] placeholder /*__DATA__*/ missing from {TEMPLATE}")
    out = tmpl.replace("/*__DATA__*/", data)

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(out)
    n = len(out.encode("utf-8"))
    print(f"[done] {OUT}")
    print(f"       {len(payload['rows']):,} PINs, {n:,} bytes ({n/1048576:.2f} MB)")
    if n > LIMIT:
        sys.exit(f"[!] exceeds the {LIMIT/1048576:.0f} MB artifact limit")


if __name__ == "__main__":
    main()
