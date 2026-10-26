"""
fetch_nhd.py
============
Fetches NHD stream geometry and clips each regulation sub-segment to its
actual geographic extent using landmark geocoding + Shapely line interpolation.

For streams with sub-segments like:
  (A) From Nimbus Dam to USGS gauging station  → CLOSED
  (B) From USGS gauging station to Ancil Hoffman → RESTRICTED
  (C) From Ancil Hoffman to Jibboom St.         → OPEN

This script produces THREE separate GeoJSON features, each colored by status,
clipped to the correct reach of the NHD line.

Usage:
    python fetch_nhd.py           # resumes from checkpoint
    python fetch_nhd.py --reset   # start fresh

Requirements:
    pip install shapely
"""

import json, os, re, sys, time
import urllib.request, urllib.parse
import math
from shapely.geometry import LineString, MultiLineString, Point, shape
import shapely.ops as sops

REGS_PATH  = 'data/regulations.json'
OUT_PATH   = 'data/streams.geojson'
CHECKPOINT = 'data/nhd_checkpoint.json'

NHD_BASE = (
    'https://hydro.nationalmap.gov/arcgis/rest/services/'
    'NHDPlus_HR/MapServer/3/query'   # Layer 3 = NetworkNHDFlowline (was /2 = NHDPoint)
)
CA_BBOX = (-124.5, 32.4, -114.0, 42.1)
UA = 'CA-Fishing-Reg-Map/1.0'


# ── Utilities ─────────────────────────────────────────────────────────────────

def clean(s):
    return re.sub(r'\s+', ' ', str(s or '')).strip()

def get_json(url, params=None, data=None, timeout=10):
    if params:
        url = url + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
    if data:
        req.data = data if isinstance(data, bytes) else data.encode()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def seg_status(seg):
    t = ((seg.get('open_season', '')) + ' ' + (seg.get('bag_limit', ''))).lower()
    if 'closed to all fishing all year' in t or ('closed' in t and 'all year' in t):
        return 'closed'
    if re.search(r'artificial|barbless|catch and release|minimum size', t):
        return 'restricted'
    if re.search(r'\bjan\.|\bfeb\.|\bmar\.|\bapr\.|\bjun\.|\bjul\.|\baug\.|\bsep\.|\boct\.|\bnov\.|\bdec\.|all year', t):
        return 'open'
    return 'unknown'


# ── NHD Fetching ──────────────────────────────────────────────────────────────

def nhd_variants(name):
    """Name variants to try against GNIS_Name."""
    v = [name]

    # Strip geographic qualifiers like "Deer Creek below dam" or "River and tributaries"
    # Use .* (not .+) so "River and tributaries" (nothing after keyword) is also stripped
    stripped = re.sub(
        r'\s+(above|below|upstream|downstream|and tributaries|and tributary).*$',
        '', name, flags=re.IGNORECASE
    ).strip()
    if stripped != name:
        v.append(stripped)

    # Strip "main stem" suffix: "Sacramento River main stem" → "Sacramento River"
    for nm in list(v):
        s2 = re.sub(r'\s+main\s+stem\b.*$', '', nm, flags=re.IGNORECASE).strip()
        if s2 != nm:
            v.append(s2)

    # Strip trailing parenthetical county info added by some reg parsers
    for nm in list(v):
        s2 = re.sub(r'\s*\(.*\)\s*$', '', nm).strip()
        if s2 and s2 != nm:
            v.append(s2)

    # Fork re-ordering: "North Fork American River" → "American River, North Fork"
    for nm in list(v):
        m = re.match(r'^(North|South|East|West|Middle)\s+Fork\s+(.+)', nm, re.IGNORECASE)
        if m:
            v.append(f"{m.group(2)}, {m.group(1)} Fork")

    # Deduplicate while preserving order (shortest/simplest names first after base)
    seen = set()
    result = []
    for x in v:
        if x and x not in seen:
            seen.add(x)
            result.append(x)
    return result

def nhd_search(name):
    # Layer 3 (NetworkNHDFlowline) uses lowercase field names; no STATES field.
    # Use CA bounding box as a spatial filter instead.
    safe = name.replace("'", "''")
    try:
        data = get_json(NHD_BASE, params={
            'where':        f"gnis_name = '{safe}'",
            'geometry':     f'{CA_BBOX[0]},{CA_BBOX[1]},{CA_BBOX[2]},{CA_BBOX[3]}',
            'geometryType': 'esriGeometryEnvelope',
            'spatialRel':   'esriSpatialRelIntersects',
            'inSR':         '4326',
            'outSR':        '4326',
            'outFields':    'gnis_name,lengthkm,streamleve,reachcode',
            'returnGeometry': 'true',
            'f':            'geojson',
            'resultRecordCount': '100',
        })
        err = data.get('error')
        if err:
            print(f'      NHD API error: {err}')
            return []
        return data.get('features', [])
    except Exception as e:
        print(f'      NHD error: {e}')
        return []

def fetch_stream_line(stream):
    """
    Returns (shapely_geometry, length_km) for a stream, or (None, 0).
    Tries all name variants, merges all matching NHD flowlines.
    """
    all_feats = []
    for variant in nhd_variants(stream['name']):
        feats = nhd_search(variant)
        if feats:
            all_feats.extend(feats)
            break  # stop after first hit to avoid mixing different streams
        time.sleep(0.25)

    if not all_feats:
        return None, 0

    # Build Shapely geometries
    lines = []
    total_km = 0
    for f in all_feats:
        geom = shape(f['geometry'])
        km = f.get('properties', {}).get('lengthkm', 0) or 0
        total_km += km
        if geom.geom_type == 'LineString':
            lines.append(geom)
        elif geom.geom_type == 'MultiLineString':
            lines.extend(geom.geoms)

    if not lines:
        return None, 0

    if len(lines) == 1:
        return lines[0], total_km

    merged = sops.linemerge(lines)
    return merged, total_km


def to_work_line(geom):
    """Get a single LineString to work with (longest piece of MultiLineString)."""
    if geom is None:
        return None
    if geom.geom_type == 'LineString':
        return geom
    if geom.geom_type == 'MultiLineString':
        merged = sops.linemerge(geom)
        if merged.geom_type == 'LineString':
            return merged
        return max(geom.geoms, key=lambda g: g.length)
    return None


# ── Landmark geocoding ────────────────────────────────────────────────────────

_GEO_CACHE = {}

# Patterns that mean "start/end of stream" — don't geocode these
_IS_MOUTH     = re.compile(r'^(the\s+)?mouth\b', re.IGNORECASE)
_IS_CONFLU    = re.compile(r'^(its\s+)?(junction|confluence)\s+with', re.IGNORECASE)
_IS_STATE_LINE= re.compile(r'state\s+line', re.IGNORECASE)

def simplify_landmark(raw):
    """Strip leading articles and qualifiers, return a geocodable string."""
    s = clean(raw)
    # Remove leading "the "
    s = re.sub(r'^the\s+', '', s, flags=re.IGNORECASE)
    # Truncate at over-specific locators like "about 300 yards downstream"
    s = re.sub(r'\s+(approximately|about|located|at its|at the|cable crossing|paved junction).+$', '', s, flags=re.IGNORECASE)
    # Remove trailing "Also see..." notes
    s = re.sub(r'\.\s*(Also|Note|See).+$', '', s, flags=re.IGNORECASE)
    s = s.rstrip('.,; ')
    return s

def geocode(text, stream_name, county):
    """
    Geocode a landmark string near a stream.
    Returns (lng, lat) or (None, None).
    """
    key = f'{text}|{county}'
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]

    simple = simplify_landmark(text)
    if not simple or len(simple) < 3:
        _GEO_CACHE[key] = (None, None)
        return None, None

    queries = [
        f'{simple}, {county} County, California',
        f'{simple} near {stream_name}, California',
        f'{simple}, California',
    ]

    for q in queries:
        try:
            results = get_json('https://nominatim.openstreetmap.org/search', params={
                'q': q, 'format': 'json', 'limit': 1, 'countrycodes': 'us',
            }, timeout=10)
            if results:
                lng = float(results[0]['lon'])
                lat = float(results[0]['lat'])
                print(f'        📍 "{simple[:45]}" → {lat:.4f},{lng:.4f}')
                _GEO_CACHE[key] = (lng, lat)
                return lng, lat
            time.sleep(0.6)
        except Exception as e:
            print(f'        Geocode error: {e}')
            time.sleep(1.0)

    _GEO_CACHE[key] = (None, None)
    return None, None


# ── Line clipping ─────────────────────────────────────────────────────────────

def frac_on_line(line, lng, lat):
    """Normalized (0–1) projection of point onto line."""
    return line.project(Point(lng, lat), normalized=True)

def clip(line, f0, f1):
    """Clip a LineString to [f0, f1] (normalized fractions)."""
    f0, f1 = sorted([max(0.0, min(1.0, f0)), max(0.0, min(1.0, f1))])
    if f1 - f0 < 1e-5:
        return None
    try:
        c = sops.substring(line, f0, f1, normalized=True)
        return c if not c.is_empty else None
    except Exception:
        return None

def rep_point(geom):
    """Representative (lng, lat) midpoint of a line geometry."""
    if geom.geom_type == 'LineString':
        coords = list(geom.coords)
    else:
        coords = list(list(geom.geoms)[0].coords)
    mid = coords[len(coords) // 2]
    return mid[0], mid[1]

def to_geojson_geom(geom):
    """Convert Shapely geometry to GeoJSON dict."""
    if geom.geom_type == 'LineString':
        return {'type': 'LineString', 'coordinates': [list(c[:2]) for c in geom.coords]}
    elif geom.geom_type == 'MultiLineString':
        return {'type': 'MultiLineString',
                'coordinates': [[list(c[:2]) for c in g.coords] for g in geom.geoms]}
    return None


# ── Feature builders ──────────────────────────────────────────────────────────

def seg_feature(stream, seg, seg_idx, status, geom_dict, rep_lng, rep_lat, source):
    return {
        'type': 'Feature',
        'geometry': geom_dict,
        'properties': {
            'id':          f'{stream["id"]}_s{seg_idx}',
            'stream_id':   stream['id'],
            'seg_idx':     seg_idx,
            'seg_label':   seg.get('label', ''),
            'name':        stream['name'],
            'county':      stream['county'],
            'section':     stream['section'],
            'status':      status,
            'source':      source,
            'lat':         rep_lat,
            'lng':         rep_lng,
            'description': seg.get('description', ''),
            'open_season': seg.get('open_season', ''),
            'bag_limit':   seg.get('bag_limit', ''),
            'bounds':      seg.get('bounds', {}),
            # Full segments list carried for detail panel
            'segments':    stream.get('segments', []),
        }
    }

def whole_stream_feature(stream, geom_dict, rep_lng, rep_lat, source):
    """Single feature for the entire stream when not splitting."""
    segs = stream.get('segments', [])
    first_seg = segs[0] if segs else {}
    status = seg_status(first_seg) if segs else stream.get('status', 'unknown')
    return {
        'type': 'Feature',
        'geometry': geom_dict,
        'properties': {
            'id':        stream['id'],
            'stream_id': stream['id'],
            'seg_idx':   -1,
            'name':      stream['name'],
            'county':    stream['county'],
            'section':   stream['section'],
            'status':    status,
            'source':    source,
            'lat':       rep_lat,
            'lng':       rep_lng,
            'segments':  segs,
        }
    }

def point_feature(stream, lng, lat):
    return {
        'type': 'Feature',
        'geometry': {'type': 'Point', 'coordinates': [lng, lat]},
        'properties': {
            'id':       stream['id'],
            'stream_id':stream['id'],
            'name':     stream['name'],
            'county':   stream['county'],
            'section':  stream['section'],
            'status':   stream.get('status', 'unknown'),
            'source':   'nominatim',
            'lat': lat, 'lng': lng,
            'segments': stream.get('segments', []),
        }
    }


# ── Per-stream processing ─────────────────────────────────────────────────────

def process(stream):
    """
    Fetch NHD geometry and return list of GeoJSON features (one per segment).
    Falls back to whole-line or point if clipping isn't possible.
    """
    name   = stream['name']
    county = stream['county']
    segs   = stream.get('segments', [])

    # ── Fetch NHD geometry ──────────────────────────────────────────────────
    nhd_geom, nhd_km = fetch_stream_line(stream)
    if nhd_geom is None:
        return []

    work = to_work_line(nhd_geom)
    if work is None:
        return []

    geom_dict = to_geojson_geom(nhd_geom)
    r_lng, r_lat = rep_point(work)
    print(f'      NHD ✓  {nhd_km:.1f} km, {nhd_geom.geom_type}')

    # ── Single-segment or no labeled sub-segments → whole line ──────────────
    labeled = [s for s in segs if s.get('label')]
    if not labeled or len(labeled) < 2:
        return [whole_stream_feature(stream, geom_dict, r_lng, r_lat, 'nhd')]

    # ── Multi-segment: compute per-segment fractions ─────────────────────────
    print(f'      Clipping {len(labeled)} labeled segments…')
    n = len(labeled)

    # Step 1: resolve landmark fractions where possible
    resolved = []
    for seg in labeled:
        b = seg.get('bounds', {})
        f0 = f1 = None

        if b.get('from_landmark') and b.get('to_landmark'):
            lm_from = b['from_landmark']
            lm_to   = b['to_landmark']

            if _IS_MOUTH.match(lm_from) or _IS_CONFLU.match(lm_from):
                f0 = 0.0
            else:
                lng0, lat0 = geocode(lm_from, name, county)
                if lng0: f0 = frac_on_line(work, lng0, lat0)

            if _IS_MOUTH.match(lm_to):
                f1 = 1.0
            elif _IS_CONFLU.match(lm_to) or _IS_STATE_LINE.search(lm_to):
                f1 = 0.0
            else:
                lng1, lat1 = geocode(lm_to, name, county)
                if lng1: f1 = frac_on_line(work, lng1, lat1)

        elif b.get('qualifier') and b.get('landmark'):
            lm = b['landmark']
            lng_l, lat_l = geocode(lm, name, county)
            if lng_l:
                fp = frac_on_line(work, lng_l, lat_l)
                if b['qualifier'] == 'above':
                    f0, f1 = fp, 1.0
                else:
                    f0, f1 = 0.0, fp

        resolved.append((f0, f1, seg))

    # Step 2: fill gaps with proportional estimates
    step = 1.0 / n
    cursor = 0.0
    features = []

    for i, (f0, f1, seg) in enumerate(resolved):
        status = seg_status(seg)

        if f0 is not None and f1 is not None and abs(f0 - f1) > 0.01:
            start = min(f0, f1)
            end   = max(f0, f1)
        elif f1 is not None and f0 is None:
            start = cursor
            end   = f1
        elif f0 is not None and f1 is None:
            start = f0
            end   = min(f0 + step, 1.0)
        else:
            # Proportional fallback
            start = i * step
            end   = (i + 1) * step

        start = max(0.0, min(1.0, start))
        end   = max(0.0, min(1.0, end))
        cursor = end

        clipped = clip(work, start, end)
        if clipped is None:
            print(f'        ({seg.get("label","?")}) degenerate, skipping')
            continue

        cg = to_geojson_geom(clipped)
        if cg is None:
            continue

        rl, rlat = rep_point(clipped if clipped.geom_type == 'LineString'
                             else list(clipped.geoms)[0])
        feat = seg_feature(stream, seg, i, status, cg, rl, rlat,
                           'nhd' if (f0 is not None or f1 is not None) else 'nhd-approx')
        features.append(feat)

        approx = '' if (f0 is not None or f1 is not None) else ' (approx)'
        print(f'        ({seg.get("label","?")}): {status}{approx} [{start:.2f}–{end:.2f}]')

    # Fall back to whole-line if all segments failed to clip
    if not features:
        features.append(whole_stream_feature(stream, geom_dict, r_lng, r_lat, 'nhd'))

    return features


# ── Nominatim fallback ────────────────────────────────────────────────────────

def nominatim_point(stream):
    raw_name = stream['name']
    # Use only the first county when multiple are listed (e.g. "Humboldt, Lake, Mendocino and Trinity")
    county_raw = stream.get('county', '')
    first_county = re.split(r',|\band\b', county_raw, maxsplit=1)[0].strip()

    # Also try a simplified stream name (strip qualifiers)
    simple_name = re.sub(
        r'\s+(above|below|upstream|downstream|and tributaries|and tributary|main stem).+$',
        '', raw_name, flags=re.IGNORECASE
    ).strip()

    queries = [
        f'{simple_name}, {first_county} County, California',
        f'{raw_name}, California',
        f'{simple_name}, California',
    ]

    for q in queries:
        try:
            results = get_json('https://nominatim.openstreetmap.org/search', params={
                'q': q, 'format': 'json', 'limit': 1, 'countrycodes': 'us',
            })
            if results:
                return float(results[0]['lon']), float(results[0]['lat'])
            time.sleep(0.6)
        except Exception as e:
            print(f'    Nominatim error: {e}')
            time.sleep(1.0)

    return None, None


# ── Checkpoint ────────────────────────────────────────────────────────────────

def load_cp():
    if os.path.exists(CHECKPOINT):
        with open(CHECKPOINT) as f:
            return json.load(f)
    return {}

def save_cp(done):
    with open(CHECKPOINT, 'w') as f:
        json.dump(done, f)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
    reset = '--reset' in sys.argv
    os.makedirs('data', exist_ok=True)

    with open(REGS_PATH) as f:
        regs = json.load(f)
    streams = regs['streams']

    retry_failed = '--retry-failed' in sys.argv
    done = {} if reset else load_cp()
    if reset and os.path.exists(CHECKPOINT):
        os.remove(CHECKPOINT)
    # Clear cached failures so they get re-tried with improved logic
    if retry_failed:
        cleared = sum(1 for v in done.values() if v == [])
        done = {k: v for k, v in done.items() if v != []}
        print(f'Cleared {cleared} cached failures for retry.')

    all_feats = []
    no_geo    = []
    total     = len(streams)

    for i, stream in enumerate(streams):
        sid = stream['id']
        print(f'\n[{i+1}/{total}] {stream["name"]} ({stream["county"]}) §{stream["section"]}')

        if sid in done:
            cached = done[sid]
            if cached:
                all_feats.extend(cached)
                print(f'    [cached: {len(cached)} feature(s)]')
            else:
                no_geo.append(stream['name'])
                print(f'    [cached: no geometry]')
            continue

        feats = process(stream)

        if feats:
            done[sid] = feats
            all_feats.extend(feats)
        else:
            # Nominatim fallback
            time.sleep(1.0)
            lng, lat = nominatim_point(stream)
            if lng:
                feat = point_feature(stream, lng, lat)
                done[sid] = [feat]
                all_feats.append(feat)
                print(f'    ~ Point fallback: {lat:.4f},{lng:.4f}')
            else:
                done[sid] = []
                no_geo.append(stream['name'])
                print(f'    ✗ No geometry found')

        if (i + 1) % 10 == 0:
            save_cp(done)
            print(f'    [checkpoint saved]')

    save_cp(done)

    geojson = {'type': 'FeatureCollection', 'features': all_feats}
    with open(OUT_PATH, 'w') as f:
        json.dump(geojson, f)

    print(f'\n{"="*55}')
    print(f'Total features:  {len(all_feats)}')
    print(f'Streams w/ geo:  {total - len(no_geo)} / {total}')
    print(f'No geometry:     {len(no_geo)}')
    if no_geo:
        print('\nUnmapped:')
        for n in no_geo[:20]:
            print(f'  - {n}')
    print(f'\nSaved: {OUT_PATH}')


if __name__ == '__main__':
    main()
