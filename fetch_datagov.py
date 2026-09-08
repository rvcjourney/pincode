#!/usr/bin/env python3
"""
Resumable, rate-limit-aware downloader for data.gov.in OGD resources.

Usage:
  python fetch_datagov.py <resource_id> <out.jsonl> [--key KEY] [--workers 8]

Notes:
  * The public sample key caps page size at 10 rows/request. Register a free
    personal key at https://data.gov.in/user/register to raise it (1000+/request),
    then export DATA_GOV_KEY=<your key>. The script auto-detects the real cap.
  * Resumable: re-running appends only missing offsets (state kept in <out>.done).
"""
import argparse, json, os, random, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

SAMPLE_KEY = "579b464db66ec23bdd000001cdd3946e44ce4aad7209ff7b23ac571b"
BASE = "https://api.data.gov.in/resource"


def call(rid, key, offset, limit, timeout=60):
    url = f"{BASE}/{rid}?api-key={key}&format=json&offset={offset}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "ck-pincode-etl/1.0"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def call_retry(rid, key, offset, limit, tries=8):
    delay = 1.0
    for i in range(tries):
        try:
            return call(rid, key, offset, limit)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise
        except Exception:
            pass
        time.sleep(delay + random.random())
        delay = min(delay * 1.8, 30)
    raise RuntimeError(f"offset {offset} failed after {tries} tries")


def detect_page_size(rid, key):
    for lim in (2000, 1000, 500, 100, 10):
        d = call_retry(rid, key, 0, lim)
        if d.get("count", 0) >= min(lim, d.get("total", 0)):
            return lim, d["total"]
        if d.get("count"):
            return d["count"], d["total"]
    raise RuntimeError("could not detect page size")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("resource_id")
    ap.add_argument("out")
    ap.add_argument("--key", default=os.environ.get("DATA_GOV_KEY", SAMPLE_KEY))
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    page, total = detect_page_size(a.resource_id, a.key)
    print(f"[i] page_size={page} total_rows={total}", flush=True)

    done_path = a.out + ".done"
    done = set()
    if os.path.exists(done_path):
        done = {int(x) for x in open(done_path).read().split()}
    offsets = [o for o in range(0, total, page) if o not in done]
    print(f"[i] {len(offsets)} pages to fetch ({len(done)} already done)", flush=True)

    fout = open(a.out, "a", encoding="utf-8")
    fdone = open(done_path, "a")
    n = 0
    t0 = time.time()
    remaining = total - len(done) * page

    def work(off):
        d = call_retry(a.resource_id, a.key, off, page)
        return off, d.get("records", [])

    try:
        with ThreadPoolExecutor(a.workers) as ex:
            for off, recs in ex.map(work, offsets):
                for r in recs:
                    fout.write(json.dumps(r, ensure_ascii=False) + "\n")
                # Durability ordering matters: the rows must be on disk BEFORE
                # the offset is marked done. Otherwise a crash between the two
                # leaves .done claiming an offset whose rows were never flushed,
                # and the resume silently skips them - a short pull that looks
                # complete. Flush data, fsync, then record the offset.
                fout.flush()
                os.fsync(fout.fileno())
                fdone.write(f"{off}\n")
                fdone.flush()
                n += len(recs)
                if n % 5000 < page:
                    el = max(time.time() - t0, 1e-6)
                    eta = (remaining - n) / max(n / el, 1e-6)
                    print(f"[i] {n:,}/{remaining:,} rows  {el:.0f}s  eta {eta:.0f}s",
                          flush=True)
    finally:
        fout.close()
        fdone.close()
    print(f"[done] wrote {n:,} rows to {a.out} in {time.time()-t0:.0f}s", flush=True)
    if n < remaining:
        print(f"[!] expected {remaining:,} rows but got {n:,}. Re-run to resume the "
              f"missing offsets before building - a short pull looks exactly like "
              f"a mass office closure.", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
