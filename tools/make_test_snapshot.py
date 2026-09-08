#!/usr/bin/env python3
"""
Generate a synthetic data.gov.in snapshot from the current DB, with known
deltas, so refresh.py can be exercised without an API key.

    python tools/make_test_snapshot.py raw/test_snapshot.jsonl \
        --add 5 --close 30 --modify 118

Offices are emitted in the data.gov.in JSON field names that refresh.load_new
expects, so this drives the real code path.
"""
import argparse, json, os, random, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import ckdb  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--db", default=None)
    ap.add_argument("--add", type=int, default=5)
    ap.add_argument("--close", type=int, default=30)
    ap.add_argument("--modify", type=int, default=118)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    random.seed(a.seed)
    db = ckdb.connect(a.db)
    rows = db.rows("""SELECT office_name, pincode, office_type, delivery_status,
                             circle_name, region_name, division_name, taluk,
                             district_raw, state_raw, latitude, longitude
                      FROM post_office WHERE valid_to IS NULL ORDER BY office_key""")
    db.close()
    if not rows:
        sys.exit("[!] no offices in DB - build first")

    idx = list(range(len(rows)))
    random.shuffle(idx)
    closed = set(idx[:a.close])
    modded = set(idx[a.close:a.close + a.modify])

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    n = 0
    with open(a.out, "w", encoding="utf-8") as f:
        for i, r in enumerate(rows):
            if i in closed:
                continue
            if i in modded:
                # flip delivery status - a real, meaningful field change
                r["delivery_status"] = ("Non-Delivery"
                                        if (r["delivery_status"] or "").lower()
                                        .startswith("delivery") else "Delivery")
            f.write(json.dumps({
                "officename": r["office_name"], "pincode": r["pincode"],
                "officetype": r["office_type"] or "", "delivery": r["delivery_status"],
                "circlename": r["circle_name"], "regionname": r["region_name"],
                "divisionname": r["division_name"], "taluk": r["taluk"],
                "districtname": r["district_raw"], "statename": r["state_raw"],
                "latitude": r["latitude"], "longitude": r["longitude"],
            }, ensure_ascii=False) + "\n")
            n += 1
        for j in range(a.add):
            f.write(json.dumps({
                "officename": f"CK Test New {j} B.O", "pincode": f"41100{j}",
                "officetype": "BO", "delivery": "Delivery", "circlename": "Maharashtra",
                "regionname": "Pune", "divisionname": "Pune", "taluk": "Haveli",
                "districtname": "Pune", "statename": "Maharashtra",
                "latitude": 18.52, "longitude": 73.85,
            }) + "\n")
            n += 1
    print(f"[done] {n:,} offices -> {a.out}  "
          f"(closed {a.close}, modified {a.modify}, added {a.add})")


if __name__ == "__main__":
    main()
