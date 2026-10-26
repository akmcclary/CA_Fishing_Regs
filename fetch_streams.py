"""
fetch_streams.py

Geocodes each regulated stream using an Overpass-first pipeline:
  1. Overpass API (CA-scoped)  → query all OSM ways with name="X" within
                                  California using a county area filter first,
                                  then a CA-wide fallback.  Uses exact OSM name
                                  tags so Pole Creek in Placer County cannot
                                  match a Pole Creek in another state.
  2. NLDI UM fallback          → if no OSM line geometry is found, snap a
                                  Nominatim point to the nearest NHD+ reach
                                  and walk 50 km upstream (main-stem only).
  3. Lake-tributary lookup     → for "X Lake tributaries" entries, geocode the
                                  lake itself and store as a point.

Boundary qualifiers ("above X", "below X", "from X to Y") are extracted from
the raw regulation name.  When Shapely is available, the geometry is
automatically clipped at the landmark using Nominatim geocoding.

Outputs:
  data/streams.geojson      -- GeoJSON FeatureCollection
  data/needs_review.json    -- regulations that need manual follow-up
  data/regulations.json     -- updated with lat/lng

Usage:
  python fetch_streams.py
  python fetch_streams.py --retry-not-found   # re-geocode previously missed streams

All remote calls are cached in data/cache/; progress is saved every 20 streams
so interrupted runs resume from their last checkpoint.
"""

import json
import os
import re
import sys
import time
import hashlib
import requests

try:
    from shapely.geometry import shape, mapping, Point as SPoint
    from shapely.ops import linemerge, substring as ssubstring
    SHAPELY_OK = True
except ImportError:
    SHAPELY_OK = False

REGS_PATH        = os.path.join("data", "regulations.json")
GEO_PATH         = os.path.join("data", "streams.geojson")
NEEDS_REVIEW_PATH = os.path.join("data", "needs_review.json")
CACHE_DIR        = os.path.join("data", "cache")

# All 58 California counties — used to disambiguate creeks that share the same
# name across multiple counties (e.g., Rock Creek, Mill Creek, Walker Creek).
CA_COUNTIES = {
    "Alameda", "Alpine", "Amador", "Butte", "Calaveras", "Colusa",
    "Contra Costa", "Del Norte", "El Dorado", "Fresno", "Glenn",
    "Humboldt", "Imperial", "Inyo", "Kern", "Kings", "Lake", "Lassen",
    "Los Angeles", "Madera", "Marin", "Mariposa", "Mendocino", "Merced",
    "Modoc", "Mono", "Monterey", "Napa", "Nevada", "Orange", "Placer",
    "Plumas", "Riverside", "Sacramento", "San Benito", "San Bernardino",
    "San Diego", "San Francisco", "San Joaquin", "San Luis Obispo",
    "San Mateo", "Santa Barbara", "Santa Clara", "Santa Cruz", "Shasta",
    "Sierra", "Siskiyou", "Solano", "Sonoma", "Stanislaus", "Sutter",
    "Tehama", "Trinity", "Tulare", "Tuolumne", "Ventura", "Yolo", "Yuba",
}


def parse_counties(county_str):
    """
    Extract a list of valid CA county names from a regulation's county string.

    Handles:
      "Placer"                                        → ["Placer"]
      "Humboldt, Lake, Mendocino and Trinity"          → 4 counties
      "Mono Co. tributary to West Walker River"        → ["Mono"]
      "Sonoma Co. tributary to Russian River"          → ["Sonoma"]
      "Butte, Sutter and Yuba"                         → ["Butte", "Sutter", "Yuba"]
    """
    if not county_str:
        return []
    found = []
    # Sort longer names first so "San Luis Obispo" matches before "San"
    for cnty in sorted(CA_COUNTIES, key=len, reverse=True):
        # Word-boundary match on county name
        pat = r'(?<![A-Za-z])' + re.escape(cnty) + r'(?![A-Za-z])'
        if re.search(pat, county_str):
            found.append(cnty)
    # Preserve appearance order
    found.sort(key=lambda c: county_str.find(c))
    return found

CA_BBOX = "-124.48,32.53,-114.13,42.01"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL  = "https://overpass-api.de/api/interpreter"
NLDI_BASE     = "https://api.water.usgs.gov/nldi/linked-data"

HEADERS = {"User-Agent": "CA-Fish-Reg-Map/1.0 (educational; contact: public data use)"}

# -- Name cleaning ------------------------------------------------------------

_STRIP_FOR_GEOCODE = re.compile(
    r"\s+(and\s+(all\s+)?tributaries?"
    r"|main\s*stem|mainstem|main\s+stream"
    r"|downstream.*|upstream.*"
    r"|from\s+.*|west\s+of\s+.*"
    r"|within\s+.*|above\s+.*|below\s+.*"
    r"|at\s+.*|near\s+.*)\s*$",
    re.IGNORECASE,
)


def clean_query_name(name):
    cleaned = _STRIP_FOR_GEOCODE.sub("", name).strip().rstrip(",.")
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", cleaned).strip()
    return cleaned if cleaned else name


def _has_tributaries(name):
    return bool(re.search(r"\band\s+(all\s+)?tributar", name, re.I))


# -- Caching ------------------------------------------------------------------

def _cache_key(tag):
    return hashlib.md5(tag.encode()).hexdigest()


def _load_cache(tag):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, _cache_key(tag) + ".json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def _save_cache(tag, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, _cache_key(tag) + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


# -- Nominatim ----------------------------------------------------------------

def _fork_queries(query_name):
    """
    For compound names like "Carson River, East Fork" or
    "American River, North Fork, Middle Fork, South Fork"
    generate alternate searches like "East Fork Carson River".
    """
    parts = [p.strip() for p in query_name.split(",")]
    if len(parts) < 2:
        return []
    base = parts[0]  # e.g. "American River"
    alts = []
    for fork in parts[1:]:
        fork = fork.strip()
        if fork:
            alts.append(f"{fork} {base}")  # "East Fork Carson River"
    alts.append(base)  # bare base name as last resort
    return alts


def _name_extra_variants(base_name):
    """
    Generate additional search variants for streams that OSM may tag differently.
      - "X or Y Creek" → try each alternative
      - "X River" / "X Creek" → try without the type suffix
      - "X River West Fork" → "West Fork X River" (OSM preferred form)
      - "North Fork X River" → "X River, North Fork" (NHD form)
    """
    variants = []

    # "X or Y" — try each side independently
    if re.search(r'\bor\b', base_name, re.I):
        parts = re.split(r'\s+or\s+', base_name, flags=re.I)
        for part in parts:
            part = part.strip()
            if part and part != base_name:
                variants.append(part)

    # Strip terminal waterway type: "Arroyo Seco River" → "Arroyo Seco"
    m = re.match(r'^(.+?)\s+(River|Creek|Stream|Run|Branch|Fork|Channel|Slough|Canal)$',
                 base_name, re.I)
    if m:
        stripped = m.group(1).strip()
        if stripped and stripped != base_name:
            variants.append(stripped)

    # "X River West Fork" → "West Fork X River" (OSM usually puts direction first)
    m = re.match(r'^(.+?)\s+(North|South|East|West|Middle)\s+Fork$', base_name, re.I)
    if m:
        variants.append(f"{m.group(2).capitalize()} Fork {m.group(1).strip()}")

    # "West Fork X River" → try "X River, West Fork" (NHD/alternate form)
    m = re.match(r'^(North|South|East|West|Middle)\s+Fork\s+(.+)$', base_name, re.I)
    if m:
        variants.append(f"{m.group(2).strip()}, {m.group(1).capitalize()} Fork")

    # "Non-anadromous tributaries of the X River" → "X River"
    m = re.match(r'^(?:non-anadromous\s+)?(?:anadromous\s+)?(?:waters?\s+of\s+)?(?:all\s+)?(?:tributaries?\s+of\s+)?(?:the\s+)?(.+)$',
                 base_name, re.I)
    if m:
        candidate = m.group(1).strip()
        if candidate and candidate != base_name and len(candidate) > 3:
            variants.append(candidate)

    # Deduplicate while preserving order
    seen = {base_name}
    return [v for v in variants if v and v not in seen and not seen.add(v)]


def _lake_name_from_tributary_entry(raw_name):
    """
    For entries like "Almanor Lake tributaries", "Bucks Lake tributaries",
    "Berryessa Lake tributaries", "Soulajule Reservoir tributaries", etc.,
    extract the lake/reservoir name for Nominatim geocoding.
    Returns (lake_search_name, lake_type) or (None, None).
    """
    # Patterns: "X Lake tributaries", "Lake X tributaries",
    #           "X Reservoir tributaries", "X tributaries" where X looks like a lake
    m = re.match(
        r'^(.+?)\s+(Lake|Reservoir)\s+tributaries?\s*$', raw_name, re.I)
    if m:
        return m.group(1).strip() + " " + m.group(2).capitalize(), m.group(2).lower()

    m = re.match(r'^Lake\s+(.+?)\s+tributaries?\s*$', raw_name, re.I)
    if m:
        return "Lake " + m.group(1).strip(), "lake"

    # "X Lake and all its tributaries"
    m = re.match(r'^(.+?)\s+(Lake|Reservoir)\s+and\s+all\s+its\s+tributaries?\s*$',
                 raw_name, re.I)
    if m:
        return m.group(1).strip() + " " + m.group(2).capitalize(), m.group(2).lower()

    return None, None


# -- Boundary qualifier parser ------------------------------------------------

_QUAL_PATTERNS = [
    re.compile(
        r"(?:^|\s)(from\s+.+?\s+(?:downstream|upstream)\s+to\s+.+?)(?:\s*\(|$)",
        re.I,
    ),
    re.compile(
        r"(?:^|\s)(from\s+.+?\s+to\s+.+?)(?:\s*\(|$)",
        re.I,
    ),
    re.compile(
        r"(?:^|\s)((?:above|upstream\s+(?:of|from))\s+\S.*?)(?:\s*\(|$)",
        re.I,
    ),
    re.compile(
        r"(?:^|\s)((?:below|downstream\s+(?:of|from))\s+\S.*?)(?:\s*\(|$)",
        re.I,
    ),
]


def parse_qualifiers(raw_name):
    """Return {'qualifier_text': str|None, 'base_name': str}."""
    # Strip trailing county portion in parentheses
    name_no_paren = re.sub(r"\s*\([^)]*\)\s*$", "", raw_name).strip()
    for pat in _QUAL_PATTERNS:
        m = pat.search(name_no_paren)
        if m:
            return {
                "qualifier_text": m.group(1).strip(),
                "base_name": clean_query_name(raw_name),
            }
    return {"qualifier_text": None, "base_name": clean_query_name(raw_name)}


# -- Overpass -----------------------------------------------------------------

def _overpass_elements_to_geom(elements):
    """Convert Overpass way/relation elements (with inline geometry) to MultiLineString."""
    lines = []
    for elem in elements:
        etype = elem.get("type")
        if etype == "way":
            nodes = elem.get("geometry", [])
            coords = [[n["lon"], n["lat"]] for n in nodes if "lat" in n and "lon" in n]
            if len(coords) >= 2:
                lines.append(coords)
        elif etype == "relation":
            for member in elem.get("members", []):
                if member.get("type") == "way" and member.get("geometry"):
                    coords = [
                        [n["lon"], n["lat"]]
                        for n in member["geometry"]
                        if "lat" in n and "lon" in n
                    ]
                    if len(coords) >= 2:
                        lines.append(coords)
    if not lines:
        return None
    if len(lines) == 1:
        return {"type": "LineString", "coordinates": lines[0]}
    return {"type": "MultiLineString", "coordinates": lines}


def _overpass_query(query):
    """Execute an Overpass QL query and return parsed JSON, or None on error.
    Retries up to 3 times with exponential back-off for rate-limit (429) errors.
    """
    for attempt in range(3):
        wait = 2.0 * (2 ** attempt)   # 2s, 4s, 8s
        time.sleep(wait)
        try:
            r = requests.post(
                OVERPASS_URL,
                data={"data": query},
                headers=HEADERS,
                timeout=60,
            )
            if r.status_code == 429:
                print(f"    Overpass 429, retrying in {wait*2:.0f}s…", flush=True)
                time.sleep(wait * 2)
                continue
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                print(f"    Overpass 429, retrying…", flush=True)
                continue
            print(f"    Overpass error: {e}")
            return None
        except Exception as e:
            print(f"    Overpass error: {e}")
            return None
    print("    Overpass: gave up after 3 retries")
    return None


def _build_multi_county_area_def(counties):
    """
    Build an Overpass area-definition block scoped to ALL the given CA counties.
    Returns (area_def_block, area_filter_clause) for use in the union query.
    Example output:
      area_def: 'area["name"="Mono County"]["admin_level"="6"]->.a0;\n
                 area["name"="Inyo County"]["admin_level"="6"]->.a1;\n'
      filter:   '(area.a0)(area.a1)'   <-- NOT what we want; instead we
      use a UNION (one query per area combined inside the ( ; ; ) block).
    """
    lines = []
    var_names = []
    for i, cnty in enumerate(counties):
        var = f"a{i}"
        var_names.append(var)
        lines.append(f'area["name"="{cnty} County"]["admin_level"="6"]->.{var};')
    return "\n".join(lines), var_names


def _overpass_stream(name, county):
    """
    Query Overpass for named waterways within California, restricted to the
    specific county (or counties) listed in the regulation.

    For names that exist in multiple CA counties (Rock Creek, Mill Creek,
    Walker Creek, Bear River, etc.) this prevents conflation by:
      1. Parsing the regulation's county string into a list of CA counties.
      2. Querying Overpass with `way[name=X](area.county1) ∪ ...(area.countyN)`.
      3. Skipping the CA-wide fallback whenever ≥1 valid county was extracted.
    Returns (geom, lat, lon) or (None, None, None).
    """
    base_name = clean_query_name(name)
    counties  = parse_counties(county)
    # Cache key is normalized to the parsed counties (sorted) so two regulations
    # with the same name + counties share a result, but different counties don't.
    counties_key = "|".join(sorted(counties)) if counties else "CA"
    cache_tag    = f"overpass2:{base_name}:{counties_key}"
    cached = _load_cache(cache_tag)
    if cached is not None:
        return cached.get("geom"), cached.get("lat"), cached.get("lon")

    # Build name variants
    name_variants = [base_name] + _fork_queries(base_name) + _name_extra_variants(base_name)
    seen_nv = set()
    name_variants = [v for v in name_variants if v and not (v in seen_nv or seen_nv.add(v))]

    # Build query plans:
    #   1. Multi-county union (only if ≥1 valid county)
    #   2. CA-wide fallback (only if NO valid counties — avoids conflation)
    query_plans = []  # list of (escaped_name, area_def, way_clauses, rel_clauses)

    for variant in name_variants:
        escaped = variant.replace('"', '\\"')

        if counties:
            area_def, var_names = _build_multi_county_area_def(counties)
            way_clauses = "\n".join(
                f'  way["waterway"]["name"="{escaped}"](area.{v});'
                for v in var_names
            )
            rel_clauses = "\n".join(
                f'  relation["waterway"]["name"="{escaped}"](area.{v});'
                for v in var_names
            )
            query_plans.append((escaped, area_def, way_clauses, rel_clauses))
        else:
            # No valid CA county was extracted — fall back to CA-wide search.
            ca_area = 'area["name"="California"]["admin_level"="4"]->.area;'
            way_clauses = f'  way["waterway"]["name"="{escaped}"](area.area);'
            rel_clauses = f'  relation["waterway"]["name"="{escaped}"](area.area);'
            query_plans.append((escaped, ca_area, way_clauses, rel_clauses))

    got_real_empty = False

    for escaped_name, area_def, way_clauses, rel_clauses in query_plans:
        query = (
            f"[out:json][timeout:45][maxsize:52428800];\n"
            f"{area_def}\n"
            f"(\n"
            f"{way_clauses}\n"
            f"{rel_clauses}\n"
            f");\n"
            f"out geom;"
        )
        data = _overpass_query(query)
        if data is None:
            continue  # server error — try next plan, don't cache
        elements = data.get("elements", [])
        if not elements:
            got_real_empty = True
            continue  # confirmed "not in OSM" — try next variant

        geom = _overpass_elements_to_geom(elements)
        if geom:
            geom = _filter_geom_to_ca(geom)
            if geom:
                geom = _decimate_geom(geom)
                lat, lon = _centroid_of_geometry(geom)
                _save_cache(cache_tag, {"geom": geom, "lat": lat, "lon": lon})
                return geom, lat, lon

    if got_real_empty:
        _save_cache(cache_tag, {"geom": None, "lat": None, "lon": None})
    return None, None, None


# -- Geometry utilities ------------------------------------------------------


def _filter_geom_to_ca(geom):
    """Filter LineString/MultiLineString coordinates to the CA bounding box."""
    def in_ca(c):
        return -124.48 <= c[0] <= -114.13 and 32.53 <= c[1] <= 42.01

    gtype = geom.get("type")
    if gtype == "LineString":
        coords = [c for c in geom["coordinates"] if in_ca(c)]
        return {"type": "LineString", "coordinates": coords} if len(coords) >= 2 else None
    if gtype == "MultiLineString":
        lines = []
        for line in geom["coordinates"]:
            ca = [c for c in line if in_ca(c)]
            if len(ca) >= 2:
                lines.append(ca)
        if not lines:
            return None
        return {"type": "MultiLineString", "coordinates": lines}
    return None


def _decimate_geom(geom, max_pts_per_line=300):
    """Reduce point density for large geometries to keep file size reasonable."""
    def thin(pts, cap):
        if len(pts) <= cap:
            return pts
        step = max(1, len(pts) // cap)
        thinned = pts[::step]
        if thinned[-1] != pts[-1]:
            thinned.append(pts[-1])
        return thinned

    gtype = geom.get("type")
    if gtype == "LineString":
        return {"type": "LineString",
                "coordinates": [[round(x, 4), round(y, 4)]
                                 for x, y in thin(geom["coordinates"], max_pts_per_line)]}
    if gtype == "MultiLineString":
        lines = []
        for line in geom["coordinates"]:
            thinned = thin(line, max_pts_per_line)
            lines.append([[round(x, 4), round(y, 4)] for x, y in thinned])
        return {"type": "MultiLineString", "coordinates": lines}
    return geom



def _nominatim_geocode(name, county):
    """Fallback: plain Nominatim point (no geometry), used when nom_geometry fails."""
    query_name = clean_query_name(name)
    cache_tag  = f"nom2:{query_name}:{county}"
    cached = _load_cache(cache_tag)
    if cached is not None:
        return cached.get("lat"), cached.get("lon")

    # Use first county only (multi-county strings confuse Nominatim)
    first_county = re.split(r"[,\s]+and\s+|,", county)[0].strip() if county else ""

    queries = []
    if first_county:
        queries.append(f"{query_name}, {first_county} County, California, USA")
    queries.append(f"{query_name}, California, USA")
    # Fallbacks for compound fork names
    for alt in _fork_queries(query_name):
        if first_county:
            queries.append(f"{alt}, {first_county} County, California, USA")
        queries.append(f"{alt}, California, USA")

    for q in queries:
        try:
            time.sleep(1.1)
            r = requests.get(
                NOMINATIM_URL,
                params={"q": q, "format": "json", "limit": 1, "countrycodes": "us"},
                headers=HEADERS,
                timeout=10,
            )
            r.raise_for_status()
            results = r.json()
            if results:
                lat = float(results[0]["lat"])
                lon = float(results[0]["lon"])
                if 32.53 <= lat <= 42.01 and -124.48 <= lon <= -114.13:
                    _save_cache(cache_tag, {"lat": lat, "lon": lon})
                    return lat, lon
        except Exception as e:
            print(f"    Nominatim error: {e}")

    _save_cache(cache_tag, {"lat": None, "lon": None})
    return None, None


# -- NLDI ---------------------------------------------------------------------

def _nldi_snap(lat, lon):
    cache_tag = f"nldi_snap:{lat:.5f}:{lon:.5f}"
    cached = _load_cache(cache_tag)
    if cached is not None:
        return cached.get("comid")

    try:
        r = requests.get(
            f"{NLDI_BASE}/comid/position",
            params={"coords": f"POINT({lon} {lat})"},
            headers=HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        feats = data.get("features", [])
        if feats:
            comid = str(feats[0]["properties"]["comid"])
            _save_cache(cache_tag, {"comid": comid})
            return comid
    except Exception as e:
        print(f"    NLDI snap error: {e}")

    _save_cache(cache_tag, {"comid": None})
    return None


def _nldi_network(comid, with_tributaries=False):
    mode = "UT" if with_tributaries else "UM"
    # 50 km for both modes: enough to cover the local regulated section without
    # pulling the entire state watershed (150 km = 4600+ reaches on American River).
    distance = "50"
    cache_tag = f"nldi_{mode.lower()}_50:{comid}"
    cached = _load_cache(cache_tag)
    if cached is not None:
        return cached

    try:
        r = requests.get(
            f"{NLDI_BASE}/comid/{comid}/navigation/{mode}/flowlines",
            params={"distance": distance},
            headers=HEADERS,
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        feats = data.get("features", [])
        _save_cache(cache_tag, feats)
        return feats
    except Exception as e:
        print(f"    NLDI network error: {e}")

    _save_cache(cache_tag, [])
    return []


def _round_coords(ring, dp=4):
    """Round coordinate precision to reduce file size while preserving visible detail."""
    return [[round(x, dp), round(y, dp)] for x, y in ring]


def _nldi_to_multilinestring(features):
    """Merge NLDI flowlines into a single MultiLineString (each reach = one ring)."""
    coords = []
    for feat in features:
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        if gtype == "LineString":
            rounded = _round_coords(geom["coordinates"])
            if rounded:
                coords.append(rounded)
        elif gtype == "MultiLineString":
            for line in geom["coordinates"]:
                rounded = _round_coords(line)
                if rounded:
                    coords.append(rounded)
    return {"type": "MultiLineString", "coordinates": coords} if coords else None


def _centroid_of_geometry(geom):
    if geom["type"] == "Point":
        c = geom["coordinates"]
        return c[1], c[0]
    all_pts = []
    if geom["type"] == "MultiLineString":
        for line in geom["coordinates"]:
            all_pts.extend(line)
    elif geom["type"] == "LineString":
        all_pts = geom["coordinates"]
    if not all_pts:
        return None, None
    mid = all_pts[len(all_pts) // 2]
    return mid[1], mid[0]


# -- Geographic qualifier clipping -------------------------------------------

def _parse_qualifier_direction(qualifier_text):
    """
    Parse a qualifier string into (direction, landmark_or_tuple).
    Returns:
      ('above',    'Los Padres Dam')
      ('below',    'Keswick Dam')
      ('from_to',  ('Belden Bridge', 'Cresta Powerhouse'))
    or (None, None) if not parseable.
    """
    qt = qualifier_text.strip()

    # "from X downstream/upstream to Y"
    m = re.match(r'^from\s+(.+?)\s+(?:downstream|upstream)\s+to\s+(.+)$', qt, re.I)
    if m:
        return 'from_to', (m.group(1).strip(), m.group(2).strip())

    # "from X to Y"
    m = re.match(r'^from\s+(.+?)\s+to\s+(.+)$', qt, re.I)
    if m:
        return 'from_to', (m.group(1).strip(), m.group(2).strip())

    # "above X" / "upstream of X" / "upstream from X"
    m = re.match(r'^(?:above|upstream\s+(?:of|from))\s+(.+)$', qt, re.I)
    if m:
        return 'above', m.group(1).strip()

    # "below X" / "downstream of X" / "downstream from X"
    m = re.match(r'^(?:below|downstream\s+(?:of|from))\s+(.+)$', qt, re.I)
    if m:
        return 'below', m.group(1).strip()

    return None, None


def _geocode_landmark(landmark, stream_name, county):
    """
    Geocode a landmark (dam, reservoir, bridge, etc.) near a stream using
    Nominatim.  Returns (lat, lon) or (None, None).
    """
    first_county = re.split(r"[,\s]+and\s+|,", county)[0].strip() if county else ""

    cache_tag = f"landmark:{landmark}:{first_county}"
    cached = _load_cache(cache_tag)
    if cached is not None:
        return cached.get("lat"), cached.get("lon")

    queries = []
    if first_county:
        queries.append(f"{landmark}, {first_county} County, California")
    queries.append(f"{landmark}, California")
    queries.append(landmark)

    for q in queries:
        try:
            time.sleep(1.1)
            r = requests.get(
                NOMINATIM_URL,
                params={"q": q, "format": "json", "limit": 1, "countrycodes": "us"},
                headers=HEADERS,
                timeout=12,
            )
            r.raise_for_status()
            results = r.json()
            if results:
                lat = float(results[0]["lat"])
                lon = float(results[0]["lon"])
                if 32.53 <= lat <= 42.01 and -124.48 <= lon <= -114.13:
                    _save_cache(cache_tag, {"lat": lat, "lon": lon})
                    return lat, lon
        except Exception as e:
            print(f"    Landmark geocode error ({landmark}): {e}", flush=True)

    _save_cache(cache_tag, {"lat": None, "lon": None})
    return None, None


def _avg_lon(shp_line):
    """Average longitude of a Shapely line geometry (proxy for inland-ness)."""
    coords = list(shp_line.coords)
    if not coords:
        return -120.0
    return sum(c[0] for c in coords) / len(coords)


def _clip_geom_at_qualifier(geom, qualifier_text, stream_name, county):
    """
    Clip stream geometry to the sub-segment specified by the qualifier.

    Uses Shapely to:
      1. Merge the (Multi)LineString
      2. Geocode the landmark(s)
      3. Split the line at the landmark's nearest point
      4. Keep the appropriate half:
           "above X" → upstream  = more inland (higher avg longitude in CA)
           "below X" → downstream = more coastal (lower avg longitude)
           "from X to Y" → the section between the two cut points

    Returns (clipped_geom_dict, lm_lat, lm_lon) or (None, None, None).
    """
    if not SHAPELY_OK:
        return None, None, None
    if geom is None or geom.get("type") not in ("LineString", "MultiLineString"):
        return None, None, None

    direction, landmark = _parse_qualifier_direction(qualifier_text)
    if direction is None:
        return None, None, None

    # ── Convert to Shapely and merge ─────────────────────────────────────────
    try:
        shp = shape(geom)
        if hasattr(shp, 'geoms'):
            merged = linemerge(shp)
        else:
            merged = shp
    except Exception:
        return None, None, None

    # If still MultiLineString (disconnected), we work with each piece
    if hasattr(merged, 'geoms'):
        lines = list(merged.geoms)
    else:
        lines = [merged]

    if not lines:
        return None, None, None

    # ── Single-landmark (above/below) ────────────────────────────────────────
    if direction in ('above', 'below'):
        lm_lat, lm_lon = _geocode_landmark(landmark, stream_name, county)
        if lm_lat is None:
            return None, None, None

        lm_pt = SPoint(lm_lon, lm_lat)

        # Pick the line piece closest to the landmark
        line = min(lines, key=lambda l: l.distance(lm_pt))

        frac = line.project(lm_pt, normalized=True)
        # If the landmark is very near an endpoint the qualifier adds no info
        if frac < 0.04 or frac > 0.96:
            return None, lm_lat, lm_lon  # signal: found landmark but can't clip

        try:
            seg_a = ssubstring(line, 0,    frac, normalized=True)
            seg_b = ssubstring(line, frac, 1,    normalized=True)
        except Exception:
            return None, lm_lat, lm_lon

        lon_a = _avg_lon(seg_a)
        lon_b = _avg_lon(seg_b)

        if direction == 'above':
            keep = seg_a if lon_a > lon_b else seg_b
        else:
            keep = seg_a if lon_a < lon_b else seg_b

        if keep.is_empty:
            return None, lm_lat, lm_lon

        clipped = mapping(keep)
        if clipped['type'] == 'LineString':
            clipped['coordinates'] = [
                [round(x, 4), round(y, 4)] for x, y in clipped['coordinates']
            ]
        return clipped, lm_lat, lm_lon

    # ── Two-landmark (from X to Y) ────────────────────────────────────────────
    if direction == 'from_to':
        lm1_name, lm2_name = landmark
        lm1_lat, lm1_lon = _geocode_landmark(lm1_name, stream_name, county)
        lm2_lat, lm2_lon = _geocode_landmark(lm2_name, stream_name, county)
        if lm1_lat is None or lm2_lat is None:
            return None, lm1_lat or lm2_lat, lm1_lon or lm2_lon

        lm1_pt = SPoint(lm1_lon, lm1_lat)
        lm2_pt = SPoint(lm2_lon, lm2_lat)

        # Use the longest connected piece
        line = max(lines, key=lambda l: l.length)

        frac1 = line.project(lm1_pt, normalized=True)
        frac2 = line.project(lm2_pt, normalized=True)

        lo = min(frac1, frac2)
        hi = max(frac1, frac2)

        if hi - lo < 0.03:  # landmarks too close together on this line
            return None, lm1_lat, lm1_lon

        try:
            keep = ssubstring(line, lo, hi, normalized=True)
        except Exception:
            return None, lm1_lat, lm1_lon

        if keep.is_empty:
            return None, lm1_lat, lm1_lon

        clipped = mapping(keep)
        if clipped['type'] == 'LineString':
            clipped['coordinates'] = [
                [round(x, 4), round(y, 4)] for x, y in clipped['coordinates']
            ]
        # Return midpoint of the two landmarks as the centroid
        mid_lat = (lm1_lat + lm2_lat) / 2
        mid_lon = (lm1_lon + lm2_lon) / 2
        return clipped, mid_lat, mid_lon

    return None, None, None


# -- GeoJSON writer -----------------------------------------------------------

def _write_geojson(features, regs):
    geojson = {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "source":  regs["source"],
            "updated": regs["updated"],
        },
    }
    with open(GEO_PATH, "w", encoding="utf-8") as f:
        json.dump(geojson, f, ensure_ascii=False)


# -- Main ---------------------------------------------------------------------

SAVE_EVERY = 20


def _find_conflated_ids(streams, existing_features):
    """
    Identify stream-ids whose geometry is likely conflated with another stream
    of the same base name in a different county.  These are returned so the
    caller can drop them from the resume cache and re-query Overpass with the
    new multi-county logic.
    """
    by_base = {}
    for s in streams:
        bn = clean_query_name(s["name"])
        by_base.setdefault(bn, []).append(s)

    # Streams currently in geojson, indexed by id
    have_geom = {f["properties"]["id"]: f for f in existing_features
                 if f.get("properties", {}).get("source") == "osm"}

    conflated = set()
    for bn, group in by_base.items():
        if len(group) < 2:
            continue
        # Use parsed counties to determine if siblings really differ
        counties_per = [tuple(sorted(parse_counties(s["county"]))) for s in group]
        if len(set(counties_per)) <= 1:
            continue  # all siblings list the same counties — not conflated
        for s in group:
            if s["id"] in have_geom:
                conflated.add(s["id"])
    return conflated


def _clear_not_found_cache(streams):
    """
    For every stream in the `streams` list, delete any cached Overpass/Nominatim
    'not found' result so the next run tries fresh queries.
    """
    cleared = 0
    for stream in streams:
        name   = stream["name"]
        county = stream["county"]
        base   = clean_query_name(name)
        counties = parse_counties(county)
        counties_key = "|".join(sorted(counties)) if counties else "CA"

        for tag in [
            f"overpass:{base}:{county}",       # legacy single-county key
            f"overpass2:{base}:{counties_key}", # new multi-county key
            f"nom2:{base}:{county}",
        ]:
            path = os.path.join(CACHE_DIR, _cache_key(tag) + ".json")
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                    # Only delete 'not found' entries (geom/lat/lon all None/null)
                    if data.get("geom") is None and data.get("lat") is None:
                        os.remove(path)
                        cleared += 1
                except Exception:
                    pass
    return cleared


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    os.makedirs("data", exist_ok=True)

    retry_not_found  = "--retry-not-found"  in sys.argv
    re_disambiguate  = "--re-disambiguate"  in sys.argv

    with open(REGS_PATH, encoding="utf-8") as f:
        regs = json.load(f)

    streams = regs["streams"]
    total   = len(streams)

    print(f"Processing {total} streams…")
    if SHAPELY_OK:
        print("Shapely available — qualifier clipping enabled.")
    else:
        print("Shapely NOT available — qualifier clipping disabled.")

    # ── Load existing results (resume support) ────────────────────────────────
    # Reuse any geometry that came from the real geocoding pipeline (OSM, NLDI,
    # point, lake).  We do NOT reuse plain Nominatim-only "point" entries that
    # predate the lake-tributary or NLDI improvements so those get retried.
    existing = {}
    if os.path.exists(GEO_PATH):
        with open(GEO_PATH, encoding="utf-8") as f:
            try:
                existing_geo = json.load(f)
                for feat in existing_geo.get("features", []):
                    geom_type = (feat.get("geometry") or {}).get("type", "")
                    src       = feat.get("properties", {}).get("source", "")
                    sid       = feat["properties"].get("id", "")
                    if not sid:
                        continue
                    # Keep real line geometry (osm / nldi)
                    # Keep lake points explicitly tagged source=lake
                    # Retry plain 'point' fallbacks — they may now get NLDI lines
                    if geom_type in ("MultiLineString", "LineString") and src in ("osm", "nldi"):
                        existing[sid] = feat
                    elif geom_type == "Point" and src == "lake":
                        existing[sid] = feat
            except Exception:
                pass

    # ── --retry-not-found: purge cached failures for missing streams ──────────
    if retry_not_found:
        geo_ids    = set(existing.keys())
        missing    = [s for s in streams if s["id"] not in geo_ids]
        cleared    = _clear_not_found_cache(missing)
        print(f"--retry-not-found: cleared {cleared} cached 'not found' results "
              f"for {len(missing)} missing streams.\n")

    # ── --re-disambiguate: drop OSM features with conflated multi-county names ──
    if re_disambiguate:
        existing_features = list(existing.values())
        conflated = _find_conflated_ids(streams, existing_features)
        # Also purge legacy single-county Overpass cache entries for these
        for s in streams:
            if s["id"] not in conflated:
                continue
            base = clean_query_name(s["name"])
            for tag in [f"overpass:{base}:{s['county']}"]:
                p = os.path.join(CACHE_DIR, _cache_key(tag) + ".json")
                if os.path.exists(p):
                    try: os.remove(p)
                    except Exception: pass
        for sid in conflated:
            existing.pop(sid, None)
        if conflated:
            print(f"--re-disambiguate: dropping {len(conflated)} potentially conflated "
                  f"OSM features for re-query.")
            for s in streams:
                if s["id"] in conflated:
                    print(f"    {s['id']:12s} | {s['name']} ({s['county']})")
            print()

    print(f"Resuming with {len(existing)} previously-geocoded features.\n")

    features       = []
    new_since_save = 0
    counts         = {"osm": 0, "nldi": 0, "lake": 0, "point": 0, "none": 0}
    needs_review_list = []

    for i, stream in enumerate(streams):
        name   = stream["name"]
        county = stream["county"]
        sid    = stream["id"]
        print(f"[{i+1}/{total}] {name} ({county})", end=" … ", flush=True)

        # Parse boundary qualifiers from raw regulation name
        quals          = parse_qualifiers(name)
        qualifier_text = quals["qualifier_text"]

        # ── Already geocoded — reuse ──────────────────────────────────────────
        if sid in existing:
            feat = existing[sid]
            stream["lat"] = feat["properties"].get("lat")
            stream["lng"] = feat["properties"].get("lng")
            feat["properties"]["qualifier_text"] = qualifier_text
            feat["properties"]["needs_review"]   = qualifier_text is not None
            features.append(feat)
            src_tag = feat["properties"].get("source", "?")
            n = len((feat.get("geometry") or {}).get("coordinates", []))
            print(f"cached/{src_tag} ({n} parts) ✓", flush=True)
            counts[src_tag if src_tag in counts else "osm"] += 1
            if qualifier_text:
                needs_review_list.append({
                    "id": sid, "name": name, "county": county,
                    "reason": "has_qualifier", "qualifier_text": qualifier_text,
                })
            continue

        geometry = None
        source   = None
        lat = lon = None

        # ── Stage 1: Overpass — exact OSM waterway name match ────────────────
        geom, lat, lon = _overpass_stream(name, county)
        if geom and geom.get("type") in ("LineString", "MultiLineString"):
            geometry = geom
            source   = "osm"
            n = len(geom["coordinates"]) if geom["type"] == "MultiLineString" else 1
            print(f"Overpass ({n} parts)", flush=True)
            counts["osm"] += 1

        # ── Stage 1b: Lake/reservoir tributary lookup ─────────────────────────
        # Handles "X Lake tributaries", "X Reservoir tributaries", etc.
        if geometry is None:
            lake_search, _ = _lake_name_from_tributary_entry(name)
            if lake_search:
                lake_lat, lake_lon = _nominatim_geocode(lake_search, county)
                if lake_lat is not None:
                    geometry = {"type": "Point", "coordinates": [lake_lon, lake_lat]}
                    lat, lon = lake_lat, lake_lon
                    source   = "lake"
                    print(f"Lake point ({lake_search})", flush=True)
                    counts["lake"] += 1

        # ── Stage 2: NLDI UM — snap Nominatim point → NHD reach network ──────
        if geometry is None:
            lat, lon = _nominatim_geocode(name, county)
            if lat is not None:
                comid = _nldi_snap(lat, lon)
                if comid:
                    network = _nldi_network(comid, with_tributaries=False)
                    if network:
                        geometry = _nldi_to_multilinestring(network)
                        if geometry:
                            lat, lon = _centroid_of_geometry(geometry)
                            source   = "nldi"
                            print(f"NLDI UM ({len(network)} reaches)", flush=True)
                            counts["nldi"] += 1

                if geometry is None:
                    geometry = {"type": "Point", "coordinates": [lon, lat]}
                    source   = "point"
                    print("Nominatim point", flush=True)
                    counts["point"] += 1
            else:
                print("not found", flush=True)
                counts["none"] += 1
                needs_review_list.append({
                    "id": sid, "name": name, "county": county,
                    "reason": "not_found", "qualifier_text": qualifier_text,
                })

        stream["lat"] = lat
        stream["lng"] = lon

        if geometry is None:
            continue

        if qualifier_text:
            needs_review_list.append({
                "id": sid, "name": name, "county": county,
                "reason": "has_qualifier", "qualifier_text": qualifier_text,
            })

        feature = {
            "type": "Feature",
            "geometry": geometry,
            "properties": {
                "id":             sid,
                "name":           name,
                "county":         county,
                "section":        stream["section"],
                "status":         stream["status"],
                "segments":       stream["segments"],
                "lat":            lat,
                "lng":            lon,
                "source":         source,
                "qualifier_text": qualifier_text,
                "needs_review":   qualifier_text is not None,
                "clipped":        False,
            },
        }
        features.append(feature)
        new_since_save += 1

        if new_since_save >= SAVE_EVERY:
            _write_geojson(features, regs)
            new_since_save = 0
            print(f"  [checkpoint saved — {len(features)} features]", flush=True)

    # ── Post-process: qualifier clipping ──────────────────────────────────────
    if SHAPELY_OK:
        print(f"\n── Qualifier clipping pass ──────────────────────────────", flush=True)
        clip_ok = clip_skip = clip_fail = 0
        for feat in features:
            props = feat["properties"]
            qt    = props.get("qualifier_text")
            if not qt or props.get("clipped"):
                continue
            geom_type = (feat.get("geometry") or {}).get("type", "")
            if geom_type not in ("LineString", "MultiLineString"):
                continue  # can't clip points

            clipped, lm_lat, lm_lon = _clip_geom_at_qualifier(
                feat["geometry"], qt, props["name"], props["county"]
            )
            if clipped:
                feat["geometry"]             = clipped
                props["clipped"]             = True
                props["landmark_lat"]        = lm_lat
                props["landmark_lon"]        = lm_lon
                # Update centroid
                new_lat, new_lon = _centroid_of_geometry(clipped)
                if new_lat:
                    props["lat"] = new_lat
                    props["lng"] = new_lon
                print(f"  ✂  {props['name']} → {qt}", flush=True)
                clip_ok += 1
            elif lm_lat is not None:
                # Landmark found but clipping not useful (near endpoint)
                props["landmark_lat"] = lm_lat
                props["landmark_lon"] = lm_lon
                print(f"  ⚠  {props['name']} — landmark found, clip skipped (near end)", flush=True)
                clip_skip += 1
            else:
                clip_fail += 1
        print(f"  Clipped: {clip_ok}  Skipped: {clip_skip}  Failed: {clip_fail}", flush=True)

    # ── Final save ────────────────────────────────────────────────────────────
    _write_geojson(features, regs)

    with open(REGS_PATH, "w", encoding="utf-8") as f:
        json.dump(regs, f, indent=2, ensure_ascii=False)

    # Deduplicate needs_review list
    seen_ids = set()
    deduped  = []
    for entry in needs_review_list:
        if entry["id"] not in seen_ids:
            seen_ids.add(entry["id"])
            deduped.append(entry)
    with open(NEEDS_REVIEW_PATH, "w", encoding="utf-8") as f:
        json.dump({"count": len(deduped), "items": deduped}, f, indent=2, ensure_ascii=False)

    found = len(features)
    print(f"\n{'='*50}")
    print(f"Done! {found}/{total} streams geocoded")
    print(f"  Overpass OSM   : {counts['osm']}")
    print(f"  NLDI UM        : {counts['nldi']}")
    print(f"  Lake point     : {counts['lake']}")
    print(f"  Nominatim pt   : {counts['point']}")
    print(f"  Not found      : {counts['none']}")
    print(f"  Needs review   : {len(deduped)} (qualifiers + not-found)")
    print(f"Saved to {GEO_PATH}")
    print(f"Review list → {NEEDS_REVIEW_PATH}")


if __name__ == "__main__":
    main()
