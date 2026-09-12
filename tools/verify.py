#!/usr/bin/env python3
"""
Read-only integrity check for the CK PIN code master.

    python tools/verify.py                  # uses $CK_DB_URL
    python tools/verify.py --db postgresql://...

Writes nothing. Exit 0 if every check passes, 1 otherwise. Safe to run against
production and to wire into a deploy healthcheck or a cron alert.
"""
import argparse, os, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import ckdb  # noqa: E402

# Expected active-PIN count, the cross-check that the pipeline is neither
# inventing nor dropping PINs.
#
# 19,586 is what the official data.gov.in directory yields (165,627 offices,
# pulled 2026-09-11). The widely cited 19,101 comes from Wikipedia and matches
# older community mirrors; against the live feed it is simply out of date, so
# comparing to it flagged a correct build as wrong. Re-baseline this whenever a
# refresh moves it legitimately - a sudden drop is the thing worth catching,
# because a truncated pull looks exactly like a mass closure.
NATIONAL_PINS = 19586
TOLERANCE = 300


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    a = ap.parse_args()

    url = a.db or os.environ.get("CK_DB_URL") or ckdb.DEFAULT_URL
    print(f"[i] verifying {ckdb.describe(url)}\n")
    db = ckdb.connect(url)
    fails, warns = [], []

    def check(label, got, want, hard=True):
        ok = got == want
        print(f"  {'PASS' if ok else 'FAIL':4}  {label:52s} {got:>10,}  (expect {want})")
        if not ok:
            (fails if hard else warns).append(label)

    def info(label, sql):
        v = db.q(sql)
        print(f"  ----  {label:52s} {v:>10,}")
        return v

    # ---- structural
    for t in ("source", "snapshot", "state", "district", "post_office", "pincode",
              "change_log"):
        if not db.table_exists(t):
            print(f"  FAIL  missing table: {t}")
            fails.append(f"missing table {t}")
    if fails:
        print("\n[!] schema incomplete - run build_db.py")
        return 1

    print("Counts")
    n_off = info("post offices (current)",
                 "SELECT COUNT(*) FROM post_office WHERE valid_to IS NULL")
    n_cls = info("post offices (closed, retained)",
                 "SELECT COUNT(*) FROM post_office WHERE valid_to IS NOT NULL")
    n_act = info("active PIN codes", "SELECT COUNT(*) FROM pincode WHERE status='active'")
    info("retired PIN codes", "SELECT COUNT(*) FROM pincode WHERE status='retired'")
    info("change_log rows (all time)", "SELECT COUNT(*) FROM change_log")

    print("\nIntegrity")
    check("retired PINs still marked deliverable",
          db.q("SELECT COUNT(*) FROM pincode WHERE status='retired' "
               "AND (is_deliverable=1 OR n_offices>0)"), 0)
    check("n_offices disagrees with post_office",
          db.q("""SELECT COUNT(*) FROM (
                    SELECT p.pincode FROM pincode p
                    LEFT JOIN post_office o
                      ON o.pincode = p.pincode AND o.valid_to IS NULL
                    GROUP BY p.pincode, p.n_offices
                    HAVING p.n_offices <> COUNT(o.office_id)) x"""), 0)
    check("duplicate open rows for one office",
          db.q("""SELECT COUNT(*) FROM (
                    SELECT office_key FROM post_office WHERE valid_to IS NULL
                    GROUP BY office_key HAVING COUNT(*) > 1) x"""), 0)
    check("active PINs with zero offices",
          db.q("SELECT COUNT(*) FROM pincode WHERE status='active' AND n_offices=0"), 0)
    check("offices with a malformed PIN",
          db.q("SELECT COUNT(*) FROM post_office WHERE LENGTH(pincode) <> 6"), 0)
    check("coordinates outside India",
          db.q("""SELECT COUNT(*) FROM post_office
                  WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                    AND (latitude NOT BETWEEN 6.0 AND 37.5
                      OR longitude NOT BETWEEN 68.0 AND 97.5)"""), 0)
    check("orphaned boundary rows",
          db.q("""SELECT COUNT(*) FROM pincode_boundary b
                  WHERE NOT EXISTS (SELECT 1 FROM pincode p WHERE p.pincode = b.pincode)"""), 0)
    check("duplicate locality keys",
          db.q("""SELECT COUNT(*) FROM (
                    SELECT locality_key FROM locality
                    GROUP BY locality_key HAVING COUNT(*) > 1) x"""), 0)
    check("duplicate open locality-PIN links",
          db.q("""SELECT COUNT(*) FROM (
                    SELECT locality_id, pincode FROM locality_pincode
                    WHERE valid_to IS NULL
                    GROUP BY locality_id, pincode HAVING COUNT(*) > 1) x"""), 0)
    check("locality links pointing at no locality",
          db.q("""SELECT COUNT(*) FROM locality_pincode lp WHERE NOT EXISTS
                  (SELECT 1 FROM locality l WHERE l.locality_id = lp.locality_id)"""), 0)

    print("\nCross-checks (warnings only)")
    drift = abs(n_act - NATIONAL_PINS)
    ok = drift <= TOLERANCE
    print(f"  {'PASS' if ok else 'WARN':4}  {'active PINs vs published national count':52s} "
          f"{n_act:>10,}  (expect {NATIONAL_PINS} +/-{TOLERANCE})")
    if not ok:
        warns.append("national PIN count drift")

    no_cent = db.q("SELECT COUNT(*) FROM pincode WHERE status='active' AND centroid_lat IS NULL")
    print(f"  {'PASS' if no_cent == 0 else 'WARN':4}  "
          f"{'active PINs with no centroid':52s} {no_cent:>10,}  (expect 0)")
    if no_cent:
        warns.append(f"{no_cent} active PINs have no centroid - radius lookup fails for them")

    if n_off == 0:
        fails.append("no current offices")

    print()
    if fails:
        print(f"[!] {len(fails)} check(s) FAILED: " + "; ".join(fails))
    if warns:
        print(f"[i] {len(warns)} warning(s): " + "; ".join(warns))
    if not fails:
        print(f"[ok] integrity verified: {n_off:,} offices, {n_act:,} active PINs, "
              f"{n_cls:,} closed offices retained")
    db.close()
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
