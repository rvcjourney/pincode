#!/usr/bin/env python3
"""
Reconstruct a raw/ office source from the committed exports so the pipeline can
be built and tested offline, without a data.gov.in key.

    python tools/bootstrap_raw.py

Reads   out/exports/post_offices.csv   (154,781 current offices)
Writes  raw/mirror_pincode.csv         (bulk-snapshot column shape)

This is a TEST FIXTURE, not an audit-grade source:
  * it has no lat/long (the bulk snapshot never had any), and
  * it is already missing the 16 offices the old office_key collision dropped,
so numbers built from it will not match a real data.gov.in pull. Use
fetch_datagov.py with a real key for anything that matters.
"""
import csv, os, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(HERE, "out", "exports", "post_offices.csv")
RAW = os.path.join(HERE, "raw")
DST = os.path.join(RAW, "mirror_pincode.csv")

# raw/mirror_pincode.csv column shape expected by build_db.read_offices()
COLS = ["officename", "pincode", "officeType", "Deliverystatus", "circlename",
        "regionname", "divisionname", "Taluk", "Districtname", "statename",
        "Telephone", "RelatedSuboffice", "RelatedHeadoffice"]

MAP = {"officename": "office_name", "pincode": "pincode", "officeType": "office_type",
       "Deliverystatus": "delivery_status", "circlename": "circle_name",
       "regionname": "region_name", "divisionname": "division_name", "Taluk": "taluk",
       "Districtname": "district", "statename": "state", "Telephone": "telephone",
       "RelatedSuboffice": "related_so", "RelatedHeadoffice": "related_ho"}


def main():
    if not os.path.exists(SRC):
        sys.exit(f"[!] {SRC} not found - nothing to bootstrap from")
    os.makedirs(RAW, exist_ok=True)
    n = 0
    with open(SRC, encoding="utf-8", newline="") as fin, \
         open(DST, "w", encoding="utf-8", newline="") as fout:
        w = csv.DictWriter(fout, fieldnames=COLS)
        w.writeheader()
        for r in csv.DictReader(fin):
            w.writerow({c: (r.get(MAP[c]) or "") for c in COLS})
            n += 1
    print(f"[done] {n:,} offices -> {DST} ({os.path.getsize(DST)/1e6:.1f} MB)")
    print("[note] test fixture only: no lat/long, missing the 16 collision-dropped rows")


if __name__ == "__main__":
    main()
