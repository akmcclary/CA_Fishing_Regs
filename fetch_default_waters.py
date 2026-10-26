"""
fetch_default_waters.py

For California waters NOT specifically called out in the CDFW special-regulation
dataset (Lake Tahoe, the unmodified portions of the Yuba and Feather rivers,
etc.), fetch their geometry from OSM (which is largely NHD-derived) and attach
the applicable *district* regulations from the CDFW `region` polygons.

Pipeline:
  1. Load CDFW data (streams.geojson) and extract every CDFW `region` polygon
     larger than ~0.05 sq-degrees — these are the actual district polygons
     ('North Coast District', 'Sierra District', etc.) plus county-level
     polygons.  Each carries its own regulation segments.
  2. Build a fuzzy name set of CDFW features so we can avoid duplicating
     features that already exist as specific reaches.
  3. Query Overpass for every named CA waterway=river|stream and lake.
  4. For each OSM feature, intersect its geometry against the district polygons
     and copy each matching district's regulation segments onto the feature.
  5. Output `data/default_waters.geojson` containing one feature per OSM
     waterway, with `source="nhd"` and the joined district segments.

The `merge_streams.py` script then adds these to the main `data/streams.geojson`
as a low-opacity background layer that the special-regulation reaches can sit
on top of.

Usage:
  python fetch_default_waters.py             # incremental (uses cache)
  python fetch_default_waters.py --no-cache  # bypass cache
"""

import hashlib
import json
import os
import re
import sys
import time

import requests

try:
    from shapely.geometry import shape, mapping, MultiLineString as SMultiLineString, \
                                  LineString as SLineString
    from shapely.ops import unary_union, linemerge
    from shapely.strtree import STRtree
    SHAPELY_OK = True
except ImportError:
    SHAPELY_OK = False
    print("Shapely is required for spatial joins.  Install with: pip install shapely")
    sys.exit(1)


OVERPASS_URL    = "https://overpass-api.de/api/interpreter"
CDFW_FEATS_PATH = os.path.join("data", "streams.geojson")
OUT_PATH        = os.path.join("data", "default_waters.geojson")
CACHE_DIR       = os.path.join("data", "cache_default")

HEADERS = {"User-Agent": "CA-Fish-Reg-Map/1.0 (contact akmcclary@gmail.com)"}

CA_BBOX = (-124.48, 32.53, -114.13, 42.01)  # west, south, east, north


# ── Overpass helpers ─────────────────────────────────────────────────────────

def _cache_path(tag):
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, hashlib.md5(tag.encode()).hexdigest() + ".json")


def overpass_get(query, tag, use_cache=True):
    """POST an Overpass QL query.  Cached on disk per tag."""
    cp = _cache_path(tag)
    if use_cache and os.path.exists(cp):
        with open(cp, encoding="utf-8") as f:
            return json.load(f)
    print(f"  POST {tag}…", flush=True)
    for attempt in range(4):
        wait = 2.0 * (2 ** attempt)
        time.sleep(wait)
        try:
            r = requests.post(
                OVERPASS_URL,
                data={"data": query},
                headers=HEADERS,
                timeout=300,
            )
            if r.status_code == 429:
                print(f"    429, retrying in {wait*2:.0f}s", flush=True)
                time.sleep(wait * 2)
                continue
            r.raise_for_status()
            data = r.json()
            with open(cp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            return data
        except Exception as e:
            print(f"    overpass error: {e}", flush=True)
    return None


# ── Element → GeoJSON conversion ─────────────────────────────────────────────

def _elements_to_features(elements):
    """
    Convert Overpass elements (way / relation, with inline `geometry`) into
    GeoJSON features.  Polygons for `natural=water` (closed rings), LineStrings
    for `waterway=*`.
    """
    feats = []
    for el in elements:
        tags  = el.get("tags", {}) or {}
        name  = tags.get("name", "")
        et    = el.get("type")

        if et == "way":
            geom_pts = el.get("geometry") or []
            coords = [[p["lon"], p["lat"]] for p in geom_pts if "lat" in p]
            if len(coords) < 2:
                continue
            is_water_polygon = (
                tags.get("natural") == "water"
                and len(coords) >= 4
                and coords[0] == coords[-1]
            )
            if is_water_polygon:
                geom = {"type": "Polygon", "coordinates": [coords]}
            else:
                geom = {"type": "LineString", "coordinates": coords}
            feats.append({
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "name": name,
                    "_osm_id": f"way/{el.get('id')}",
                    "_tags":  tags,
                },
            })

        elif et == "relation":
            # Multi-polygon for water bodies, multi-line for rivers
            outer_rings = []
            inner_rings = []
            line_parts  = []
            for member in el.get("members", []) or []:
                if member.get("type") != "way":
                    continue
                geom_pts = member.get("geometry") or []
                coords = [[p["lon"], p["lat"]] for p in geom_pts if "lat" in p]
                if len(coords) < 2:
                    continue
                role = member.get("role", "")
                if len(coords) >= 4 and coords[0] == coords[-1]:
                    if role == "inner":
                        inner_rings.append(coords)
                    else:
                        outer_rings.append(coords)
                else:
                    line_parts.append(coords)

            if outer_rings:
                if len(outer_rings) == 1 and not inner_rings:
                    geom = {"type": "Polygon", "coordinates": [outer_rings[0]]}
                else:
                    polys = [[r] for r in outer_rings]
                    geom = {"type": "MultiPolygon", "coordinates": polys}
                feats.append({
                    "type": "Feature",
                    "geometry": geom,
                    "properties": {"name": name, "_osm_id": f"rel/{el.get('id')}",
                                    "_tags": tags},
                })
            elif line_parts:
                if len(line_parts) == 1:
                    geom = {"type": "LineString", "coordinates": line_parts[0]}
                else:
                    geom = {"type": "MultiLineString", "coordinates": line_parts}
                feats.append({
                    "type": "Feature",
                    "geometry": geom,
                    "properties": {"name": name, "_osm_id": f"rel/{el.get('id')}",
                                    "_tags": tags},
                })
    return feats


# ── Overpass queries ─────────────────────────────────────────────────────────
# We split rivers/streams from lakes so each query stays within timeout.

QUERY_RIVERS = """
[out:json][timeout:240][maxsize:268435456];
area["name"="California"]["admin_level"="4"]->.area;
(
  way["waterway"="river"]["name"](area.area);
  relation["waterway"="river"]["name"](area.area);
);
out geom;
"""

QUERY_STREAMS = """
[out:json][timeout:240][maxsize:268435456];
area["name"="California"]["admin_level"="4"]->.area;
(
  way["waterway"="stream"]["name"](area.area);
);
out geom;
"""

QUERY_LAKES = """
[out:json][timeout:240][maxsize:268435456];
area["name"="California"]["admin_level"="4"]->.area;
(
  way["natural"="water"]["water"~"^(lake|reservoir|pond)$"]["name"](area.area);
  relation["natural"="water"]["water"~"^(lake|reservoir|pond)$"]["name"](area.area);
);
out geom;
"""


# ── District extraction & spatial join ───────────────────────────────────────

# Districts large enough to consider for spatial-join default rules.
# Smaller polygons (< MIN_AREA) are specific reaches, not regulatory areas.
MIN_DISTRICT_AREA = 0.05   # square degrees — roughly 30×30 km


def load_districts():
    """
    Pull the CDFW `region` polygons that cover broad regulatory areas, along
    with their regulation segments.  Returns a list of dicts ready for
    spatial-join lookup.
    """
    with open(CDFW_FEATS_PATH, encoding="utf-8") as f:
        fc = json.load(f)

    districts = []
    for feat in fc["features"]:
        p = feat["properties"]
        if p.get("category") != "region":
            continue
        g = feat.get("geometry")
        if not g or g.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        try:
            sh = shape(g)
        except Exception:
            continue
        if sh.area < MIN_DISTRICT_AREA:
            continue
        districts.append({
            "id":       p["id"],
            "name":     p["name"],
            "shape":    sh,
            "segments": p.get("segments") or [],
            "section":  p.get("section") or "",
        })

    print(f"  {len(districts)} district polygons (area ≥ {MIN_DISTRICT_AREA} sq-deg)")
    return districts


def build_district_index(districts):
    """
    Build a Shapely STRtree spatial index over the district polygons so each
    waterway lookup is O(log N) instead of O(N).  Returns (tree, geometries,
    districts_in_order) — query by geometry, get back indices into districts.
    """
    geoms = [d["shape"] for d in districts]
    if not geoms:
        return None, [], []
    tree = STRtree(geoms)
    return tree, geoms, districts


def join_to_districts(feat_shape, tree, geoms, districts):
    """Return list of districts whose polygon intersects the feature.

    Uses STRtree to narrow candidates by bounding box, then verifies with
    a true intersection check.
    """
    if tree is None:
        return []
    try:
        cand_idx = tree.query(feat_shape)
    except Exception:
        return []
    matched = []
    seen = set()
    for c in cand_idx:
        # Shapely 2.x returns a numpy array of int indices; 1.x returned
        # geometries.  Handle both.
        try:
            idx = int(c)               # numpy.int64 / int → int
        except (TypeError, ValueError):
            try:
                idx = geoms.index(c)   # geometry → look up index
            except ValueError:
                continue
        if idx in seen or idx >= len(geoms):
            continue
        seen.add(idx)
        try:
            if geoms[idx].intersects(feat_shape):
                matched.append(districts[idx])
        except Exception:
            pass
    return matched


# ── CDFW name index for de-emphasis ──────────────────────────────────────────

def cdfw_name_set():
    """
    Build a lower-cased set of name fragments that already appear in CDFW
    special-regulation features so we can flag duplicates (but NOT skip them —
    the user wants to see the whole river even if some reaches are special).
    """
    with open(CDFW_FEATS_PATH, encoding="utf-8") as f:
        fc = json.load(f)
    names = set()
    for feat in fc["features"]:
        nm = (feat["properties"].get("name") or "").lower()
        if not nm:
            continue
        names.add(nm)
        # Also a "primary" name like "American River" from
        # "American River (Sacramento Co.) From Nimbus Dam to ..."
        m = re.match(r"^([^()\.,]+?)(?:\s*\(|\s+(?:from|between|above|below|"
                     r"upstream|downstream|main\s*stem|tributaries))",
                     nm, re.I)
        if m:
            names.add(m.group(1).strip())
    return names


# ── Status derivation (mirror of cdfw_to_streams.py / index.html) ────────────

_RESTRICTED_KW = re.compile(
    r"\b(?:artificial|barbless|catch[- ]and[- ]release|"
    r"size\s+limit|minimum\s+size|bag\s+(?:and|or)\s+possession|"
    r"spearfishing|bow\s+and\s+arrow|crayfish)\b",
    re.I,
)


def stream_status(segments):
    statuses = set()
    for seg in segments:
        text = ((seg.get("open_season") or "") + " " +
                (seg.get("bag_limit")   or "") + " " +
                (seg.get("description") or "") + " " +
                (seg.get("title")       or "")).lower()
        if "closed to all fishing" in text:
            statuses.add("closed")
        elif _RESTRICTED_KW.search(text):
            statuses.add("restricted")
        elif seg.get("open_season"):
            statuses.add("open")
    if not statuses:
        return "unknown"
    if len(statuses) == 1:
        return statuses.pop()
    return "mixed"


# ── Main ─────────────────────────────────────────────────────────────────────

def _norm_name_key(name):
    """Lower-cased, whitespace-collapsed name for grouping OSM ways."""
    if not name:
        return ""
    return re.sub(r"\s+", " ", name).strip().lower()


def _dedup_by_name(features, label):
    """
    OSM splits long rivers / creeks into many `way`s sharing the same name.
    Group them into a single MultiLineString / MultiPolygon per name so the
    output is one feature per actual waterway.
    """
    groups = {}
    for f in features:
        nm = _norm_name_key(f["properties"]["name"])
        if not nm:
            continue
        groups.setdefault(nm, []).append(f)

    merged = []
    for nm, group in groups.items():
        # Use first feature's display name as canonical
        display = group[0]["properties"]["name"]
        # Combine geometries
        try:
            geoms = [shape(g["geometry"]) for g in group]
        except Exception:
            continue
        if not geoms:
            continue
        if len(geoms) == 1:
            combined = geoms[0]
        else:
            try:
                combined = unary_union(geoms)
            except Exception:
                combined = geoms[0]
        # Skip empty or invalid
        if combined.is_empty:
            continue
        merged.append({
            "type": "Feature",
            "geometry": mapping(combined),
            "properties": {
                "name": display,
                "_osm_count": len(group),
            },
        })
    return merged


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    use_cache = "--no-cache" not in sys.argv

    print("Loading CDFW district polygons…")
    districts = load_districts()
    tree, geoms, dlist = build_district_index(districts)

    cdfw_names = cdfw_name_set()
    print(f"  {len(cdfw_names)} CDFW name fragments")

    print(f"\nFetching OSM rivers / lakes (use_cache={use_cache})…")
    rivers_resp  = overpass_get(QUERY_RIVERS,  "rivers_v1",  use_cache)
    lakes_resp   = overpass_get(QUERY_LAKES,   "lakes_v1",   use_cache)
    streams_resp = overpass_get(QUERY_STREAMS, "streams_v1", use_cache)

    river_feats  = _elements_to_features(rivers_resp.get("elements",  []) if rivers_resp  else [])
    lake_feats   = _elements_to_features(lakes_resp.get("elements",   []) if lakes_resp   else [])
    stream_feats = _elements_to_features(streams_resp.get("elements", []) if streams_resp else [])

    print(f"  rivers   (raw): {len(river_feats):>6}")
    print(f"  streams  (raw): {len(stream_feats):>6}")
    print(f"  lakes    (raw): {len(lake_feats):>6}")

    # ── Dedup by name (OSM splits rivers into many ways) ─────────────────────
    print("\nDeduplicating by name…", flush=True)
    rivers_d  = _dedup_by_name(river_feats,  "river")
    streams_d = _dedup_by_name(stream_feats, "stream")
    lakes_d   = _dedup_by_name(lake_feats,   "lake")
    print(f"  rivers   (deduped): {len(rivers_d):>6}")
    print(f"  streams  (deduped): {len(streams_d):>6}")
    print(f"  lakes    (deduped): {len(lakes_d):>6}")

    # ── Filter streams to ones with non-trivial size ──────────────────────────
    # Keeps named tributaries of any meaningful length; drops the long tail of
    # tiny unnamed-feeder ditches that bloat the file size.
    MIN_STREAM_DEG = 0.005   # ~500m
    filtered_streams = []
    for f in streams_d:
        try:
            sh = shape(f["geometry"])
            if sh.length >= MIN_STREAM_DEG:
                filtered_streams.append(f)
        except Exception:
            pass
    print(f"  streams kept (≥ {MIN_STREAM_DEG} deg ≈ 500m): {len(filtered_streams):>6}")
    streams_d = filtered_streams

    # ── Build features with district joins ───────────────────────────────────
    print("\nJoining to district polygons (STRtree)…", flush=True)

    out_features = []
    join_stats = {"with_district": 0, "without_district": 0}
    name_dups  = 0
    t0 = time.time()
    n_processed = 0

    for src_label, feats in (
        ("river",  rivers_d),
        ("stream", streams_d),
        ("lake",   lakes_d),
    ):
        print(f"  {src_label}: {len(feats)} features…", flush=True)
        for f in feats:
            name = f["properties"]["name"]
            if not name:
                continue
            n_processed += 1
            if n_processed % 1000 == 0:
                rate = n_processed / max(time.time() - t0, 0.001)
                print(f"    …{n_processed} processed ({rate:.0f}/s)", flush=True)

            try:
                sh = shape(f["geometry"])
            except Exception:
                continue
            if sh.is_empty:
                continue
            cx, cy = sh.centroid.x, sh.centroid.y
            if not (CA_BBOX[0] <= cx <= CA_BBOX[2] and CA_BBOX[1] <= cy <= CA_BBOX[3]):
                continue

            in_cdfw = _norm_name_key(name) in cdfw_names

            matched = join_to_districts(sh, tree, geoms, dlist)
            join_stats["with_district" if matched else "without_district"] += 1

            segs = []
            for d in matched:
                for seg in d["segments"]:
                    new_seg = dict(seg)
                    new_seg["_district_name"] = d["name"]
                    new_seg["_district_id"]   = d["id"]
                    segs.append(new_seg)
            segs = segs[:10]

            status = stream_status(segs) if segs else "unknown"

            sid = "nhd_" + hashlib.md5(
                (name + src_label).encode()
            ).hexdigest()[:10]

            out_features.append({
                "type":     "Feature",
                "geometry": f["geometry"],
                "properties": {
                    "id":        sid,
                    "name":      name,
                    "county":    "",
                    "section":   "(district default)",
                    "status":    status,
                    "category":  f"default-{src_label}",
                    "geom_kind": src_label,
                    "segments":  segs,
                    "lat":       cy,
                    "lng":       cx,
                    "source":    "nhd",
                    "qualifier_text": None,
                    "needs_review":   not bool(segs),
                    "clipped":        False,
                    "has_special_reach": in_cdfw,
                    "districts":  [d["name"] for d in matched],
                },
            })
            if in_cdfw:
                name_dups += 1

    elapsed = time.time() - t0
    print(f"\n  spatial join done in {elapsed:.1f}s")
    print(f"  features with ≥1 district match: {join_stats['with_district']:>5}")
    print(f"  features with NO district match: {join_stats['without_district']:>5}")
    print(f"  features whose name overlaps CDFW: {name_dups}")
    print(f"\nTotal default features: {len(out_features)}")

    # Status histogram
    by_status = {}
    for f in out_features:
        s = f["properties"]["status"]
        by_status[s] = by_status.get(s, 0) + 1
    print("By status:")
    for s in sorted(by_status):
        print(f"  {s:<12s} {by_status[s]}")

    out = {
        "type": "FeatureCollection",
        "metadata": {
            "source":   "OSM via Overpass — joined to CDFW district polygons",
            "fetched":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "features": out_features,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"\nSaved → {OUT_PATH}")


if __name__ == "__main__":
    main()
