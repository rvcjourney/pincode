#!/usr/bin/env python3
"""
Step 2 of the CK PIN code pipeline: turn the raw PIN code boundary GeoJSON
into app-usable artefacts.

Inputs  (raw/):
  india-pincode.geojson   PIN code polygons, 1 feature per PIN, properties EMPTY,
                          feature order matches the row order of pincode_area.xlsx
  pincode_area.xlsx       index, pincode, area(sq km)  -> supplies the PIN label

Outputs (out/):
  pincode_geo.csv         pincode, area_sqkm, centroid_lat, centroid_lon,
                          min_lat, min_lon, max_lat, max_lon, n_parts
  pincode_boundaries.geojson         full-fidelity polygons WITH pincode property
  pincode_boundaries.simplified.geojson  ~0.001 deg tolerance, for map rendering
"""
import json, os, sys
import openpyxl
from shapely.geometry import shape, mapping

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw")
OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)

SIMPLIFY_TOL = 0.001  # ~110 m; good enough for choropleths / serviceability maps


def load_pin_index():
    wb = openpyxl.load_workbook(os.path.join(RAW, "pincode_area.xlsx"), read_only=True)
    ws = wb.active
    rows = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            continue  # header
        idx, pin, area = row[0], row[1], row[2]
        rows.append((int(idx), str(pin).strip(), float(area) if area is not None else None))
    rows.sort(key=lambda r: r[0])
    return rows


def main():
    pins = load_pin_index()
    print(f"[i] {len(pins)} pincodes in area index", flush=True)

    with open(os.path.join(RAW, "india-pincode.geojson"), encoding="utf-8") as f:
        gj = json.load(f)
    feats = gj["features"]
    print(f"[i] {len(feats)} boundary features", flush=True)

    if len(feats) != len(pins):
        print(f"[!] COUNT MISMATCH features={len(feats)} pins={len(pins)} - "
              f"index join is unsafe, aborting", file=sys.stderr)
        sys.exit(2)

    csv_rows = ["pincode,area_sqkm,centroid_lat,centroid_lon,min_lat,min_lon,max_lat,max_lon,n_parts"]
    full, simp = [], []
    bad = 0

    for (idx, pin, area), feat in zip(pins, feats):
        try:
            geom = shape(feat["geometry"])
            if not geom.is_valid:
                geom = geom.buffer(0)
            c = geom.representative_point() if geom.centroid.is_empty else geom.centroid
            if not geom.contains(c):
                c = geom.representative_point()
            minx, miny, maxx, maxy = geom.bounds
            n_parts = len(geom.geoms) if geom.geom_type.startswith("Multi") else 1
            csv_rows.append(
                f"{pin},{'' if area is None else round(area,4)},"
                f"{c.y:.6f},{c.x:.6f},{miny:.6f},{minx:.6f},{maxy:.6f},{maxx:.6f},{n_parts}"
            )
            props = {"pincode": pin, "area_sqkm": None if area is None else round(area, 4)}
            full.append({"type": "Feature", "properties": props, "geometry": feat["geometry"]})
            sg = geom.simplify(SIMPLIFY_TOL, preserve_topology=True)
            if sg.is_empty:
                sg = geom
            simp.append({"type": "Feature", "properties": props, "geometry": mapping(sg)})
        except Exception as e:
            bad += 1
            print(f"[!] pin {pin} idx {idx}: {e}", file=sys.stderr)

    with open(os.path.join(OUT, "pincode_geo.csv"), "w", encoding="utf-8") as f:
        f.write("\n".join(csv_rows) + "\n")

    def dump(name, features):
        p = os.path.join(OUT, name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "name": "india-pincode",
                       "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
                       "features": features}, f)
        print(f"[i] {name}: {os.path.getsize(p)/1e6:.1f} MB", flush=True)

    dump("pincode_boundaries.geojson", full)
    dump("pincode_boundaries.simplified.geojson", simp)
    print(f"[done] {len(csv_rows)-1} pincodes with geometry, {bad} failures", flush=True)


if __name__ == "__main__":
    main()
