#!/usr/bin/env python3
"""
Verify a data.gov.in API key before relying on it.

    set DATA_GOV_KEY=<your key>        # Windows:  $env:DATA_GOV_KEY = "..."
    python tools/check_key.py

Answers the three things that actually matter:
  1. Is the key accepted at all?
  2. Is it YOUR key, or the public sample key everyone shares? The sample key is
     capped at 10 rows per request and is usually rate-limited to zero, which
     makes a 165k-row pull impossible.
  3. What page size does the server really allow, and how long will the pull take?

Never prints the key.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.data.gov.in/resource"
SAMPLE_KEY = "579b464db66ec23bdd000001cdd3946e44ce4aad7209ff7b23ac571b"
RESOURCES = {
    "All India Pincode Directory": "5c2f62fe-5afa-4119-a499-fec9d604d5bd",
    "LGD Villages with PIN Codes": "f17a1608-5f10-4610-bb50-a63c80d83974",
}


def call(rid, key, limit, timeout=45):
    url = f"{BASE}/{rid}?api-key={urllib.parse.quote(key)}&format=json&offset=0&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "ck-pincode-etl/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def main():
    key = os.environ.get("DATA_GOV_KEY", "").strip()
    if not key:
        print("[!] DATA_GOV_KEY is not set.\n"
              "    PowerShell: $env:DATA_GOV_KEY = \"<your key>\"\n"
              "    bash      : export DATA_GOV_KEY=<your key>", file=sys.stderr)
        return 2

    print(f"[i] key length {len(key)} chars, ends ...{key[-4:]}")
    if key == SAMPLE_KEY:
        print("[!] That is the PUBLIC SAMPLE KEY, not your own.\n"
              "    It is capped at 10 rows/request and usually rate-limited to zero.\n"
              "    Get your own at https://data.gov.in/user -> My Account -> APIs",
              file=sys.stderr)
        return 1

    ok = True
    for label, rid in RESOURCES.items():
        print(f"\n--- {label}")
        try:
            d = call(rid, key, 1)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:160]
            except Exception:
                pass
            if e.code in (401, 403):
                print(f"  FAIL  key rejected (HTTP {e.code}). Check it was copied whole.")
            elif e.code == 429:
                print("  FAIL  rate-limited (HTTP 429). Wait a minute and retry.")
            else:
                print(f"  FAIL  HTTP {e.code} {body}")
            ok = False
            continue
        except Exception as e:
            print(f"  FAIL  {type(e).__name__}: {e}")
            ok = False
            continue

        total = d.get("total")
        if total is None:
            print(f"  FAIL  unexpected response: {str(d)[:160]}")
            ok = False
            continue
        print(f"  PASS  accepted. {int(total):,} rows available.")

        # what page size does the server actually honour?
        page = None
        for lim in (2000, 1000, 500, 100, 10):
            try:
                t0 = time.time()
                r = call(rid, key, lim)
                got = r.get("count", 0)
                el = time.time() - t0
            except Exception:
                continue
            if got:
                page = got
                print(f"  page size: asked {lim}, got {got} in {el:.1f}s")
                if got >= lim or lim == 10:
                    break
        if not page:
            print("  FAIL  could not fetch any rows")
            ok = False
            continue
        if page <= 10:
            print("  WARN  only 10 rows/request - that is the sample-key cap.\n"
                  "        A full pull would need "
                  f"{int(total)//10:,} requests. Check you used your own key.")
            ok = False
        else:
            pages = (int(total) + page - 1) // page
            print(f"  estimate: {pages:,} requests at {page}/request "
                  f"-> roughly {max(1, pages * el / 8 / 60):.0f} min with 8 workers")

    print()
    if ok:
        print("[ok] key works. Next:\n"
              "     python fetch_datagov.py 5c2f62fe-5afa-4119-a499-fec9d604d5bd "
              "raw/postoffices.jsonl\n"
              "     python fetch_datagov.py f17a1608-5f10-4610-bb50-a63c80d83974 "
              "raw/lgd_villages.jsonl\n"
              "     python build_db.py && python build_localities.py && python export.py")
    else:
        print("[!] not usable yet - see the failures above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
