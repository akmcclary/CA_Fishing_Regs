"""
build_map_data.py — build the statewide regulation map data.

Inputs (already in data/):
  cdfw_features.geojson(.gz) official CDFW RFRService reaches/areas + linked regulation rows
  default_waters.geojson(.gz) OSM named streams/rivers/lakes for California
  special_additions.json    hand-built entries for special waters CDFW has no geometry for

Outputs (data/map/):
  rules.json      every regulation (by CCR code) + "regimes" (the governing trout/
                  salmon rule for a piece of water, with a status for each day of 2026)
                  + "rulesets" (the other area-based rules — hours, bait, gear, species)
  overview.json   simplified rivers, big lakes and all special waters (low zoom)
  tiles/X_Y.json  full-detail stream pieces per 0.5° tile (high zoom)
  search.json     name index for the search box
  qa_report.txt   coverage numbers and anything that needs a human look

How each piece of water gets its rules
  1. Streams are split wherever a special reach, special area, district or other
     regulation area begins or ends, so every piece has one consistent set of rules.
  2. The governing ("primary") rule is chosen in this order:
       special reach line matched by name  (§7.40(b) / §7.50(b))
       special area polygon / traced tributary network (smallest wins)
       district general regulation (§7.00) for that area
       statewide trout regulation (§5.85(a)(1) lakes, (a)(2) streams)
  3. Everything else that applies by area (fishing hours §3, bait §4, gear §2,
     species limits §5, low-flow closures §8) is listed alongside.
"""

import gzip
import json
import math
import os
import re
import sys
import time
from collections import defaultdict, Counter

import shapely
import shapely.ops
import shapely.prepared
from shapely.geometry import shape, mapping, LineString, MultiLineString, Point, Polygon, MultiPolygon, box
from shapely.ops import linemerge, unary_union
from shapely.strtree import STRtree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seasons

ROOT = os.environ.get("MAP_ROOT", ".")
DATA = os.path.join(ROOT, "data")
OUT = os.environ.get("MAP_OUT", os.path.join(DATA, "map"))
TILE = 0.5           # degrees
SNAP = 0.0003        # ~30 m: same-name line parts closer than this are one stream
LINE_BUF = 0.0009    # ~80-100 m: corridor around a CDFW special reach line (geometry-only match)
NAME_BUF = 0.0025    # ~220 m: corridor when the stream name matches (CDFW lines are generalized)
MIN_PIECE = 0.0002   # drop slivers shorter than ~20 m created by boundary noise

T0 = time.time()
STATEWIDE_CODES = []


def load_geojson(name):
    """Read data/<name>, or data/<name>.gz (the form kept in git: GitHub rejects files over 100 MB)."""
    path = os.path.join(DATA, name)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    with gzip.open(path + ".gz", "rt", encoding="utf-8") as f:
        return json.load(f)


def log(*a):
    print(f"[{time.time() - T0:6.1f}s]", *a, flush=True)


# ════════════════════════════════════════════════════════════════════════════
# Regulation text
# ════════════════════════════════════════════════════════════════════════════

_HDR = re.compile(r"\[start_(?:header|subHeader)\](.*?)\[end_(?:header|subHeader)\]", re.S)
_ROWBLK = re.compile(r"\[start_row\]\[row\](.*?)\[row\]\[end_row\]", re.S)


def clean(s):
    s = re.sub(r"\[/?(?:start_|end_)?(?:row|header|subHeader|col)\]|\[(?:start|end)PreTab\]", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()


def parse_verbatim(v):
    """→ (rows, text). rows = [{loc, season, bag}], text = plain paragraph."""
    v = v or ""
    if "[start_col]" in v:          # table header row ("District/Water | Open Season | Bag")
        m = re.search(r"\[startPreTab\](.*?)\[endPreTab\]", v)
        return [], clean(m.group(1)) if m else ""
    rows, last = [], ""
    for blk in _ROWBLK.findall(v):
        for sub in re.split(r"\[row\]\[row\]", blk):
            f = [clean(x) for x in sub.split("|")] + ["", "", ""]
            loc, season, bag = [("" if x == "_" else x) for x in f[:3]]
            if loc:
                last = loc
            if loc or season or bag:
                rows.append({"loc": loc or last, "season": season, "bag": bag, "cont": not loc})
    if rows:
        return rows, ""
    hdr = _HDR.findall(v)
    if hdr:
        return [], " — ".join(clean(h) for h in hdr)
    return [], clean(v)


def code_key(c):
    """Sort CCR codes naturally: 7.50(b)(9) < 7.50(b)(10)."""
    parts = re.findall(r"\d+\.\d+|\(([^)]+)\)", c)
    out = []
    for p in re.findall(r"(\d+)\.(\d+)|\(([^)]+)\)", c):
        if p[0]:
            out += [(0, int(p[0]), ""), (0, int(p[1]), "")]
        else:
            x = p[2]
            out.append((0, int(x), "") if x.isdigit() else (1, 0, x))
    return out


def section_of(code):
    m = re.match(r"(\d+\.\d+)", code or "")
    return m.group(1) if m else ""


def category_of(code, tags=""):
    s = section_of(code)
    try:
        major = float(s)
    except ValueError:
        return "other"
    if s in ("7.40", "7.50"):
        return "special"
    if s == "7.00":
        return "district"
    if s.startswith("6.3"):
        return "definition"
    if 2.0 <= major < 3.0:
        return "gear"
    if 3.0 <= major < 4.0:
        return "hours"
    if 4.0 <= major < 5.0:
        return "bait"
    if 5.0 <= major < 6.0:
        return "species"
    if 8.0 <= major < 9.0:
        return "lowflow"
    return "other"


# ════════════════════════════════════════════════════════════════════════════
# Names
# ════════════════════════════════════════════════════════════════════════════

ABBR = {"n": "north", "no": "north", "s": "south", "so": "south", "e": "east", "w": "west",
        "m": "middle", "mid": "middle", "mf": "middle fork", "nf": "north fork", "sf": "south fork",
        "ef": "east fork", "wf": "west fork", "fk": "fork", "cr": "creek", "ck": "creek", "r": "river",
        "lk": "lake", "res": "reservoir", "mt": "mount", "st": "saint"}
GENERIC = {"the", "and", "of", "tributaries", "tributary", "main", "stem", "mainstem", "its", "a", "all",
           "co", "cos", "county", "counties", "including", "portion", "section", "waters"}
HEAD_CUT = re.compile(r"\s(?:from|upstream|downstream|above|below|between|within|for\s+\d|except|"
                      r"to\s+the|at\s+the|including|excluding|near|in\s+the|tributaries|and\s+its|"
                      r"and\s+all|main\s+stem|mainstem|upper|lower)\b|\(|,\s*(?:and|from|above|below)\b|:", re.I)


def tokens(name):
    n = (name or "").lower().replace("'", "").replace("’", "")
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    out = []
    for t in n.split():
        t = ABBR.get(t, t)
        out += t.split()
    return out


def core(name):
    return frozenset(t for t in tokens(name) if t not in GENERIC)


def head_of(cdfw_name):
    m = re.search(r"\)\s*[-–]?\s*([A-Z][\w'’ .]+?)\.?\s*$", cdfw_name or "")
    if m and re.search(r"complex|and tributaries", cdfw_name, re.I) and not re.search(r"\b(from|above|below|upstream|downstream|between|within)\b", m.group(1), re.I):
        return m.group(1).strip()
    return _head_of(cdfw_name)


def _head_of(cdfw_name):
    """'Upper Truckee River and tributaries upstream from …' → 'Upper Truckee River'
    'American River, South Fork, from …' → 'American River, South Fork'"""
    n = re.sub(r"^\(\w+\)\s*", "", cdfw_name or "").strip()
    # keep "Upper"/"Lower" when they lead the name (Upper Truckee River)
    lead = ""
    m = re.match(r"(upper|lower|little|big|north|south|east|west|middle)\s+", n, re.I)
    if m:
        lead, n = n[: m.end()], n[m.end():]
    m = HEAD_CUT.search(n)
    if m:
        n = n[: m.start()]
    return (lead + n).strip(" ,.;")


def names_match(osm_name, cdfw_name):
    a, b = core(osm_name), core(head_of(cdfw_name))
    if not a or not b:
        return False
    if a == b:
        return True
    # "Hot Creek" vs "Hot Creek Ranch" etc: allow OSM ⊂ head when the extra words
    # aren't directional/fork words that would make it a different stream
    DIR = {"north", "south", "east", "west", "middle", "fork", "little", "upper", "lower", "big"}
    if a < b and not ((b - a) & DIR):
        return True
    if b < a and not ((a - b) & DIR):
        return True
    return False


def lake_named_in_area(lake_name, area_name):
    """True when a special area's text covers the lake itself ("Goose Lake and tributaries",
    "... including Lexington Reservoir"), not only the waters flowing into it."""
    area_name = area_name or ""
    if re.search(r"\band\s+(?:all\s+)?(?:its\s+|their\s+)?(?:all\s+)?tributar", area_name, re.I) and names_match(lake_name, area_name):
        return True
    m = re.search(r"\bincluding\s+([A-Z][\w'’ ]*?(?:Lake|Reservoir))", area_name)
    return bool(m) and core(m.group(1)) == core(lake_name)


JUNK_LAKE = re.compile(r"sewage|treatment|wastewater|evaporation|settling|tailings|detention|retention|"
                       r"methane|percolation|recharge|clarifier|aeration|oxidation|leach|effluent|sludge|"
                       r"cooling|lagoon\s*\d|^\W*$|^[\d\W]+$|^pond\s*\d|cell\s*\d|basin\s*\d|sediment", re.I)


# ════════════════════════════════════════════════════════════════════════════
# Geometry helpers
# ════════════════════════════════════════════════════════════════════════════

def lines_of(g):
    if g is None or g.is_empty:
        return []
    t = g.geom_type
    if t == "LineString":
        return [g]
    if t in ("MultiLineString", "GeometryCollection"):
        out = []
        for x in g.geoms:
            out += lines_of(x)
        return out
    return []


def polys_of(g):
    if g is None or g.is_empty:
        return []
    t = g.geom_type
    if t == "Polygon":
        return [g]
    if t in ("MultiPolygon", "GeometryCollection"):
        out = []
        for x in g.geoms:
            out += polys_of(x)
        return out
    return []


def as_lines(g):
    ls = [l for l in lines_of(g) if l.length > 0]
    if not ls:
        return None
    m = linemerge(MultiLineString(ls)) if len(ls) > 1 else ls[0]
    return m


def rnd(coords, nd=5):
    return [[round(x, nd), round(y, nd)] for x, y in coords]


def geom_json(g, nd=5):
    """Compact GeoJSON geometry (rounded)."""
    t = g.geom_type
    if t == "LineString":
        return {"type": "LineString", "coordinates": rnd(g.coords, nd)}
    if t == "MultiLineString":
        return {"type": "MultiLineString", "coordinates": [rnd(l.coords, nd) for l in g.geoms]}
    if t == "Polygon":
        return {"type": "Polygon", "coordinates": [rnd(g.exterior.coords, nd)] + [rnd(i.coords, nd) for i in g.interiors]}
    if t == "MultiPolygon":
        return {"type": "MultiPolygon", "coordinates": [[rnd(p.exterior.coords, nd)] + [rnd(i.coords, nd) for i in p.interiors] for p in g.geoms]}
    if t == "GeometryCollection":
        ls = lines_of(g)
        if ls:
            return geom_json(MultiLineString(ls) if len(ls) > 1 else ls[0], nd)
        ps = polys_of(g)
        if ps:
            return geom_json(MultiPolygon(ps) if len(ps) > 1 else ps[0], nd)
    raise ValueError(t)


def km(deg_len, lat=37.5):
    return deg_len * 111.0 * (0.5 * (1 + math.cos(math.radians(lat))))


# ════════════════════════════════════════════════════════════════════════════
# 1. Load CDFW
# ════════════════════════════════════════════════════════════════════════════

def load_cdfw():
    log("Loading CDFW features…")
    d = load_geojson("cdfw_features.geojson")
    rules = {}          # code → rule record
    zones = []          # regulation zones (geometry + codes)
    state = None
    for f in d["features"]:
        p = f["properties"]
        if not f.get("geometry"):
            continue
        g = shape(f["geometry"])
        if not g.is_valid:
            g = shapely.make_valid(g)
        codes = []
        for r in p.get("regulations") or []:
            c = (r.get("Code") or "").strip()
            if not c:
                continue
            rows, text = parse_verbatim(r.get("Verbatim"))
            rec = rules.get(c)
            if rec is None or (not rec["rows"] and not rec["text"]):
                rules[c] = {"code": c, "title": clean(r.get("Title")), "tags": clean(r.get("Tags")),
                            "rows": rows, "text": text, "cat": category_of(c)}
            codes.append(c)
        zones.append({"fid": p["id"], "name": clean(p.get("name")), "category": p.get("category"),
                      "geom": g, "codes": sorted(set(codes), key=code_key),
                      "counties": p.get("counties") or []})
        if p.get("name", "").strip() == "State Boundary":
            state = g
            global STATEWIDE_CODES
            STATEWIDE_CODES = [c for c in sorted(set(codes), key=code_key)
                               if category_of(c) not in ("definition", "other", "special")]
    log(f"  {len(zones)} zones, {len(rules)} regulation codes")
    return rules, zones, state


# Polygons CDFW publishes without linked rows — map them to their §7.00 subsection by name.
# Entries whose whole text is a pointer to another entry (already mapped there)
REFERS = {"7.40(83)": "Klamath River 7.40(b)(50)", "7.40(101)": "Klamath River 7.40(b)(50)",
          "7.40(103)": "Klamath River 7.40(b)(50)", "7.40(114)": "Klamath River 7.40(b)(50)",
          "7.40(118)": "Eel River 7.40(b)(40)"}
UNMAPPED = [{"code": "7.50(b)(96)", "name": "Miller Canyon Creek from Silverwood Lake upstream (San Bernardino Co.)",
             "rows": [{"loc": "Miller Canyon Creek from Silverwood Lake upstream (San Bernardino Co.)", "season": "All year.", "bag": "5 trout"}],
             "lat": 34.29, "lng": -117.27,
             "note": "Neither CDFW's map data nor OpenStreetMap has a stream by this name, so it is not drawn. The marker is approximate (Silverwood Lake)."}]

UNLINKED_700 = [
    ("North Coast District.  Anadromous waters of the Klamath", "7.00(a)(1)"),
    ("North Coast District. All anadromous waters tributary to Humboldt", "7.00(a)(2)"),
    ("Sierra District. All rivers and associated tributaries above Lake Shasta", "7.00(b)(1)"),
    ("Sierra District. Anadromous waters of Tehama and Shasta", "7.00(b)(2)"),
    ("All North Central District streams except", "7.00(c)(1)"),
    ("The North Central District stream tidewaters", "7.00(c)(2)"),
    ("Valley District. All anadromous waters", "7.00(d)(1)"),
    ("South Central District. That portion of any stream west of any Highway 1", "7.00(e)(1)"),
    ("South Central District. All anadromous streams", "7.00(e)(2)"),
    ("Southern District. All anadromous waters", "7.00(f)(1)"),
]
DISTRICT_DEF = {"6.31": "North Coast", "6.32": "North Central", "6.33": "South Central", "6.34": "Southern",
                "6.35": "Sierra", "6.36": "Valley", "6.37": "Colorado River"}
DISTRICT_ZONE_NAMES = {"North Coast District": "North Coast", "North Central District": "North Central",
                       "South Central District": "South Central", "Southern District": "Southern",
                       "Sierra District": "Sierra", "Valley District": "Valley",
                       "The Colorado River District": "Colorado River"}


# CDFW links a few rules to a bigger polygon than the rule text describes.
# code → name prefix of the polygon that matches the text.
CODE_ZONE_OVERRIDE = {
    "5.80(i)": "Shasta, Tehama, Butte and Glenn",   # "(Shasta, Tehama, Butte and Glenn cos)"
}


# CDFW geometry that contradicts its own regulation text. code → (OSM name, bbox to take)
GEOMETRY_FIX = {
    # "From Nimbus Dam to the USGS gauging station cable crossing about 300 yards downstream
    # from the Nimbus Hatchery fish rack site" — CDFW draws this at Folsom Dam, 6 km upstream.
    "7.40(b)(4)(A)": ("American River", (-121.2290, 38.6330, -121.2192, 38.6420)),
}


def apply_geometry_fixes(zones, waters, qa):
    for code, (wname, bb) in GEOMETRY_FIX.items():
        for z in zones:
            if code in z.get("primary_codes", []) and "Line" in z["geom"].geom_type:
                g = unary_union([w["geom"].intersection(box(*bb)) for w in waters if w["name"] == wname])
                g = as_lines(g)
                if g is not None:
                    z["geom"] = g
                    z["note"] = (z.get("note", "") + " Location corrected: CDFW's map data places this reach at Folsom Dam; "
                                 "the regulation text puts it just below Nimbus Dam.").strip()
                    qa.append(f"GEOMETRY CORRECTED  {code}  {z['name'][:70]}")


def classify_zones(rules, zones):
    for code, prefix in CODE_ZONE_OVERRIDE.items():
        for z in zones:
            if code in z["codes"]:
                z["codes"] = [c for c in z["codes"] if c != code]
            if z["name"].startswith(prefix):
                z["codes"] = sorted(set(z["codes"] + [code]), key=code_key)
    """Give every zone a role:
         special_line / special_poly / special_mouth  → §7.40(b)/§7.50(b)
         d700:<code>                                   → §7.00 primary area
         district:<name>                               → district outline
         area                                          → other area rules
         statewide (skipped)"""
    out = []
    for z in zones:
        name, cat, g = z["name"], z["category"], z["geom"]
        z["primary_codes"] = []
        z["area_codes"] = []
        special = [c for c in z["codes"] if re.match(r"7\.[45]0\(b\)", c)]
        if name == "State Boundary":
            continue
        if name in DISTRICT_ZONE_NAMES:
            z["district"] = DISTRICT_ZONE_NAMES[name]
        for prefix, code in UNLINKED_700:
            if name.startswith(prefix):
                z["role"] = "d700"
                z["primary_codes"] = [code]
        if z.get("role") == "d700":
            z["area_codes"] = [c for c in z["codes"] if not c.startswith("7.00")]
            out.append(z)
            continue
        if special:
            # Deepest codes are the governing rows; parents are headers.
            deep = [c for c in special if not any(o != c and (o.startswith(c + "(") or re.match(re.escape(c) + r"\d", o)) for o in special)]
            z["primary_codes"] = deep
            z["header_codes"] = [c for c in special if c not in deep]
            z["area_codes"] = [c for c in z["codes"] if c not in special]
            gt = g.geom_type
            has_season = any(r.get("season") for c in deep for r in (rules.get(c) or {}).get("rows", []))
            if name.upper().startswith("KLAMATH RIVER BASIN"):
                z["role"] = "special_poly"
                z["klamath_basin"] = True
                z["primary_codes"] = ["7.40(b)(50)(E)"]
                z["header_codes"] = ["7.40(b)(50)"]
                z["info_codes"] = [c for c in special if c not in ("7.40(b)(50)(E)", "7.40(b)(50)")]
                out.append(z)
                continue
            if not has_season:
                # Header-only or text-only entries (quota tables, "Sacramento River and
                # mainstem below Keswick Dam" header): show as information, never govern.
                z["role"] = "special_info_line" if "Line" in gt else "special_info"
                z["info_codes"] = special
                out.append(z)
                continue
            if "Line" in gt:
                z["role"] = "special_line"
            elif cat == "tributaryArea":
                z["role"] = "special_poly"
            else:
                # freshWaterBody polygons: lakes, or the tiny "mouth of tributaries" polygons
                z["role"] = "special_lake"
            out.append(z)
            continue
        # §7.00 codes linked to whole-district polygons are handled by the specific
        # sub-area polygons above; keep the rest as area rules.
        z["area_codes"] = [c for c in z["codes"] if not c.startswith("7.00(")]
        if not z["area_codes"] and not z.get("district"):
            continue
        z["role"] = "area_line" if "Line" in g.geom_type else "area"
        out.append(z)
    c = Counter(z["role"] for z in out)
    log("  zone roles:", dict(c))
    return out


# ════════════════════════════════════════════════════════════════════════════
# 2. Base waters from OSM
# ════════════════════════════════════════════════════════════════════════════

def load_osm(state):
    log("Loading OSM waters…")
    d = load_geojson("default_waters.geojson")
    state_p = shapely.prepared.prep(state.buffer(0.002)) if state is not None else None
    by_name = defaultdict(list)
    lakes = []
    for f in d["features"]:
        p = f["properties"]
        name = (p.get("name") or "").strip()
        if not name:
            continue
        g = shape(f["geometry"])
        cat = p.get("category")
        if cat == "default-lake":
            if JUNK_LAKE.search(name):
                continue
            pgs = polys_of(g)
            ls = lines_of(g)
            if ls:
                # Lakes OSM stores as shoreline ways (e.g. Lake Tahoe): rebuild the polygon
                try:
                    merged = linemerge(MultiLineString(ls))
                    pgs += list(shapely.ops.polygonize(lines_of(unary_union(merged))))
                except Exception:
                    pass
            for pg in pgs:
                if not pg.is_valid:
                    pg = shapely.make_valid(pg)
                    pg = unary_union(polys_of(pg)) if polys_of(pg) else None
                    if pg is None:
                        continue
                lakes.append({"name": name, "geom": pg})
            continue
        kind = "river" if cat == "default-river" else "stream"
        for l in lines_of(g):
            if l.length > 0:
                by_name[name].append((l, kind))
    log(f"  {sum(len(v) for v in by_name.values())} line parts across {len(by_name)} names; {len(lakes)} lake polygons")

    # Merge lake polygons with the same name that touch (OSM multipolygon pieces)
    # and clip to California.
    lakes_out = []
    lk_by = defaultdict(list)
    for l in lakes:
        lk_by[l["name"]].append(l["geom"])
    for name, gs in lk_by.items():
        u = unary_union(gs)
        for pg in polys_of(u):
            if state is not None:
                if not state_p.intersects(pg):
                    continue
                if not state_p.contains(pg):
                    pg = pg.intersection(state)
                    if pg.is_empty:
                        continue
            for q in polys_of(pg):
                if q.area > 2e-7:          # ~0.2 ha
                    lakes_out.append({"name": name, "geom": q})
    log(f"  {len(lakes_out)} lakes after merge/clip")

    # Split same-name line parts into connected streams (Trout Creek near Tahoe ≠
    # Trout Creek in San Diego County).
    waters = []
    for name, parts in by_name.items():
        geoms = [p[0] for p in parts]
        kinds = [p[1] for p in parts]
        n = len(geoms)
        parent = list(range(n))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        if n > 1:
            tree = STRtree(geoms)
            a, b = tree.query(geoms, predicate="dwithin", distance=SNAP)
            for i, j in zip(a, b):
                if i != j:
                    ri, rj = find(i), find(j)
                    if ri != rj:
                        parent[ri] = rj
        comps = defaultdict(list)
        for i in range(n):
            comps[find(i)].append(i)
        for idx in comps.values():
            g = as_lines(MultiLineString([geoms[i] for i in idx]))
            if g is None:
                continue
            if state is not None and not state_p.contains(g):
                g = g.intersection(state)
                g = as_lines(g)
                if g is None:
                    continue
            k = "river" if any(kinds[i] == "river" for i in idx) else "stream"
            waters.append({"name": name, "geom": g, "kind": k})
    log(f"  {len(waters)} connected streams/rivers")

    # Streams drawn through reservoirs/lakes (OSM centerlines): the lake's rules
    # govern there, so remove the part of the line inside lakes larger than ~5 ha.
    big = [l["geom"] for l in lakes_out if l["geom"].area > 5e-6]
    ltree = STRtree(big)
    cut_km = 0.0
    kept = []
    for w in waters:
        idx = ltree.query(w["geom"], predicate="intersects")
        if len(idx):
            inside = unary_union([big[i] for i in idx])
            g = as_lines(w["geom"].difference(inside))
            if g is None or g.length < MIN_PIECE:
                cut_km += km(w["geom"].length)
                continue
            cut_km += km(w["geom"].length - g.length)
            w["geom"] = g
        kept.append(w)
    log(f"  removed {cut_km:.0f} km of stream centerline inside lakes; {len(kept)} streams")
    return kept, lakes_out


# ════════════════════════════════════════════════════════════════════════════
# 3. Derived zones (tributary networks, waters CDFW hasn't mapped)
# ════════════════════════════════════════════════════════════════════════════

def trace_upstream(waters, lakes, start_lake_names, bbox, stop_at_lakes=True, exclude_names=(), seeds=None, exclude_geom=None):
    """Return the geometry of every stream connected to the start lake(s)/seed lines
    inside bbox, without passing through any other lake ("upstream to the first lake").
    Network edges are stream endpoints that touch another stream (within SNAP)."""
    bb = box(*bbox)
    start = unary_union([l["geom"] for l in lakes if l["name"] in start_lake_names and l["geom"].intersects(bb)]) if start_lake_names else None
    other_lakes = [l["geom"] for l in lakes if l["name"] not in start_lake_names and l["geom"].intersects(bb) and l["geom"].area > 1e-6]
    other = unary_union(other_lakes).buffer(0.0002) if other_lakes else None
    cand = [w for w in waters if w["geom"].intersects(bb) and w["name"] not in exclude_names]
    pieces = []
    for w in cand:
        g = w["geom"].intersection(bb)
        if other is not None and stop_at_lakes:
            g = g.difference(other)
        if exclude_geom is not None:
            g = g.difference(exclude_geom)
        for l in lines_of(g):
            if l.length > MIN_PIECE:
                pieces.append(l)
    if not pieces:
        return None
    tree = STRtree(pieces)
    frontier = set()
    if start is not None:
        frontier |= set(tree.query(start.buffer(SNAP * 2), predicate="intersects"))
    if seeds is not None:
        frontier |= set(tree.query(seeds.buffer(SNAP * 2), predicate="intersects"))
    seen = set(frontier)
    while frontier:
        nxt = set()
        for i in frontier:
            l = pieces[i]
            for pt in (Point(l.coords[0]), Point(l.coords[-1])):
                for j in tree.query(pt, predicate="dwithin", distance=SNAP):
                    if j not in seen:
                        seen.add(j); nxt.add(j)
            # tributaries whose own endpoint touches this line
            for j in tree.query(l, predicate="dwithin", distance=SNAP):
                if j in seen:
                    continue
                o = pieces[j]
                if Point(o.coords[0]).distance(l) < SNAP or Point(o.coords[-1]).distance(l) < SNAP:
                    seen.add(j); nxt.add(j)
        frontier = nxt
    return unary_union([pieces[i] for i in seen])


def add_derived_zones(zones, rules, waters, lakes):
    """Zones CDFW doesn't draw but the regulations clearly define."""
    added = []
    # §7.50(b)(152)(A) Tahoe Lake tributaries upstream to the first lake.
    tahoe_bbox = (-120.25, 38.70, -119.88, 39.30)
    g = trace_upstream(waters, lakes, {"Lake Tahoe"}, tahoe_bbox, exclude_names={"Truckee River"})
    if g is not None and not g.is_empty:
        added.append({"fid": "derived_tahoe_tribs", "name": "Tahoe Lake tributaries upstream to the first lake.",
                      "category": "derived", "geom": g.buffer(LINE_BUF / 3), "codes": ["7.50(b)(152)", "7.50(b)(152)(A)"],
                      "role": "special_poly", "primary_codes": ["7.50(b)(152)(A)"], "header_codes": ["7.50(b)(152)"],
                      "area_codes": [], "derived": True,
                      "note": "Traced from Lake Tahoe up every connected stream, stopping at the first lake."})
        log(f"  Tahoe tributaries traced: {km(g.length, 39):.0f} km of stream")

    # Special waters CDFW has no feature for (from special_additions.json)
    path = os.path.join(DATA, "special_additions.json")
    if os.path.exists(path):
        adds = json.load(open(path, encoding="utf-8"))
        for a in adds:
            for c, rec in a.get("rules", {}).items():
                rules[c] = {"code": c, "title": rec.get("title", ""), "tags": "", "rows": rec.get("rows", []),
                            "text": rec.get("text", ""), "cat": "special", "source": "pdf"}
            bbox = a["bbox"]
            bb = box(*bbox)
            names = set(a["osm_names"])
            if a.get("tributaries"):
                seeds = unary_union([w["geom"].intersection(bb) for w in waters if w["name"] in names and w["geom"].intersects(bb)])
                excl = None
                if a.get("cut"):
                    cutter = unary_union([w["geom"].intersection(bb) for w in waters if w["name"] == a["cut"]["by"] and w["geom"].intersects(bb)])
                    cutter = unary_union([c for c in lines_of(cutter) if c.distance(seeds) < SNAP * 3])
                    if cutter.is_empty:
                        log(f"  ! cut feature {a['cut']['by']} not found near {a['name']}")
                    else:
                        pt = shapely.ops.nearest_points(seeds, cutter)[0]
                        parts = lines_of(seeds.difference(pt.buffer(SNAP)))
                        keep_s = a["cut"]["keep"] == "south"
                        up = [q for q in parts if (q.centroid.y < pt.y) == keep_s]
                        down = [q for q in parts if (q.centroid.y < pt.y) != keep_s]
                        seeds = unary_union(up)
                        excl = unary_union(down + [cutter]).buffer(SNAP * 1.5) if down else cutter.buffer(SNAP * 1.5)
                g = trace_upstream(waters, lakes, set(a.get("start_lakes", [])), bbox, stop_at_lakes=False, seeds=seeds,
                                   exclude_names=set(a.get("exclude", [])), exclude_geom=excl)
            else:
                g = unary_union([w["geom"].intersection(bb) for w in waters if w["name"] in names and w["geom"].intersects(bb)]
                                + [l["geom"].intersection(bb) for l in lakes if l["name"] in names and l["geom"].intersects(bb)])
            if g is None or g.is_empty:
                log(f"  ! no geometry found for {a['name']}")
                continue
            role = "special_lake" if g.geom_type in ("Polygon", "MultiPolygon") else "special_poly"
            geom = g if role == "special_lake" else g.buffer(LINE_BUF / 3)
            added.append({"fid": "added_" + re.sub(r"\W+", "_", a["name"]).strip("_").lower()[:40], "name": a["name"],
                          "category": "added", "geom": geom, "codes": a["codes"], "role": role,
                          "primary_codes": a["primary_codes"], "header_codes": a.get("header_codes", []),
                          "area_codes": [], "derived": True, "note": a.get("note", "")})
            log(f"  added {a['name']}: {g.geom_type}")
    zones += added
    return added


# ════════════════════════════════════════════════════════════════════════════
# 4. Split + assign
# ════════════════════════════════════════════════════════════════════════════

def match_line_zones(az, waters):
    """For each line-based zone decide which OSM streams it applies to:
    streams whose name matches and that run inside the corridor; if none match
    by name, the stream(s) that follow the CDFW line for most of its length."""
    wtree = STRtree([w["geom"] for w in waters])
    for z in az:
        if not z["name_req"]:
            continue
        L = max(z["geom"].length, 1e-9)
        cand = []
        for i in wtree.query(z["poly"], predicate="intersects"):
            w = waters[i]
            ov = w["geom"].intersection(z["poly"]).length
            if ov > MIN_PIECE:
                cand.append((i, ov, names_match(w["name"], z["name"])))
        ok = {i for i, ov, nm in cand if nm}
        if not ok:
            # geometric fallback: streams that follow the reach (narrow corridor) for ≥ 50% of its length
            ok = {i for i, ov, nm in cand if waters[i]["geom"].intersection(z["poly_narrow"]).length >= 0.5 * L}
            z["matched_by"] = "geometry" if ok else "none"
            if ok:
                z["poly"] = z["poly_narrow"]
        else:
            z["matched_by"] = "name"
        z["allowed"] = ok


def build_assignment_zones(zones):
    """Turn zones into polygons used to split water lines.
    Special/area *lines* become a corridor polygon that only applies to streams
    whose name matches."""
    az = []
    for z in zones:
        g = z["geom"]
        role = z["role"]
        if role in ("special_line", "area_line", "special_info_line"):
            if g.length <= 0:
                continue
            az.append({**z, "poly": g.buffer(NAME_BUF, cap_style="flat"), "poly_narrow": g.buffer(LINE_BUF, cap_style="flat"),
                       "name_req": True})
        elif role == "special_lake":
            if g.area < 2e-6:
                # "within 300 feet of the mouth of its tributaries" – lake-surface strips; display only
                z["display_only"] = True
                continue
            az.append({**z, "poly": g, "name_req": False, "lakes_only": True})
        else:
            az.append({**z, "poly": g, "name_req": False})
    for i, z in enumerate(az):
        z["idx"] = i
        if z["name_req"]:
            z["ends"] = [Point(c) for l in lines_of(z["geom"]) for c in (l.coords[0], l.coords[-1])]
            z["poly"] = z["geom"].buffer(NEAR_TOL, cap_style="flat") if z.get("matched_by") != "geometry" else z["poly"]
        z["prep"] = shapely.prepared.prep(z["poly"])
        z["area"] = z["poly"].area
    return az


def _segments(line, step=0.001):
    """Cut a LineString into ~100 m segments."""
    coords = list(line.coords)
    out, cur, acc = [], [coords[0]], 0.0
    for a, b in zip(coords, coords[1:]):
        cur.append(b)
        acc += math.hypot(b[0] - a[0], b[1] - a[1])
        if acc >= step:
            out.append(LineString(cur))
            cur, acc = [b], 0.0
    if len(cur) > 1:
        out.append(LineString(cur))
    return out


NEAR_TOL = 0.005     # ~450 m: max distance from a stream segment to the CDFW reach line it takes
END_TOL = 0.0006     # ~55 m: tolerance beyond a reach line's end


def split_water(w, cands):
    """Split a stream line by the candidate zones → [(geom, frozenset(zone idx))].
    Polygon zones cut by geometry; name-matched reach lines claim the segments of the
    stream that are nearest to them (handles braids and side channels)."""
    pieces = [(w["geom"], frozenset())]
    line_z = []
    for z in cands:
        if z.get("lakes_only"):
            continue
        if z["name_req"]:
            if w["wid"] in z["allowed"]:
                line_z.append(z)
            continue
        new = []
        for g, mem in pieces:
            if not z["poly"].intersects(g):
                new.append((g, mem))
                continue
            if z["prep"].contains(g):
                new.append((g, mem | {z["idx"]}))
                continue
            try:
                inside = g.intersection(z["poly"])
                outside = g.difference(z["poly"])
            except shapely.errors.GEOSException:
                new.append((g, mem))
                continue
            gi, go = as_lines(inside), as_lines(outside)
            if gi is not None and gi.length > MIN_PIECE:
                new.append((gi, mem | {z["idx"]}))
            elif gi is not None and go is None:
                new.append((gi, mem | {z["idx"]}))
            if go is not None and (go.length > MIN_PIECE or gi is None):
                new.append((go, mem))
        pieces = new
    if not line_z:
        return pieces

    # Name-matched reach lines: nearest-line assignment per ~100 m segment.
    out = []
    for g, mem in pieces:
        if not any(z["poly"].intersects(g) for z in line_z):
            out.append((g, mem))
            continue
        groups = defaultdict(list)
        for part in lines_of(g):
            for seg in _segments(part):
                mid = seg.interpolate(0.5, normalized=True)
                best, bestd, extra = None, NEAR_TOL, []
                for z in line_z:
                    d = z["geom"].distance(mid)
                    if d >= NEAR_TOL:
                        continue
                    np_ = shapely.ops.nearest_points(z["geom"], mid)[0]
                    if d > END_TOL and min(np_.distance(e) for e in z["ends"]) < 1e-7:
                        continue           # beyond the end of this reach
                    if z["role"] == "special_line":
                        if d < bestd:
                            best, bestd = z, d
                    else:
                        extra.append(z["idx"])     # info / area lines: all that apply
                m = set(mem) | set(extra)
                if best is not None:
                    m.add(best["idx"])
                groups[frozenset(m)].append(seg)
        for m, segs in groups.items():
            gm = as_lines(MultiLineString(segs))
            if gm is not None:
                out.append((gm, m))
    return out


def resolve_refs(rows, rules, z):
    """'See Milton Lake 7.50(b)(97)' → that entry's rows; 'Covered under Statewide
    regulation subsection 5.85(a)(2)' → statewide."""
    out, note = [], ""
    for r in rows:
        t = (r.get("season") or "")
        if re.search(r"covered under statewide", t, re.I):
            note = "statewide"
            continue
        m = re.match(r"\s*see\s+(.*?)(?:,?\s*(?:sub)?section\s*)?((?:7\.[45]0)?\s*\(b\)\(\d+\))", t, re.I)
        if m:
            ref = m.group(2).replace(" ", "")
            if not ref.startswith("7."):
                ref = section_of(z["primary_codes"][0]) + ref
            sub = [rr for c, rec in rules.items() if (c == ref or c.startswith(ref + "(")) for rr in rec.get("rows", []) if rr.get("season")]
            if sub:
                out += sub
                note = f"See {m.group(1).strip(' ,')} {ref}."
            continue
        out.append(r)
    return out, note


def choose_primary(mem, az, is_lake, water_name):
    """Return (kind, zone) of the governing rule."""
    zs = [az[i] for i in mem]
    lines = [z for z in zs if z["role"] == "special_line"]
    if lines and not is_lake:
        return "special", min(lines, key=lambda z: z["area"])
    # For lakes, a special_poly is only in mem when its text names the lake itself
    # (see lake_named_in_area); a special-lake polygon is more specific, so it wins.
    polys = [z for z in zs if z["role"] == "special_poly"]
    lakes = [z for z in zs if z["role"] == "special_lake" and is_lake]
    sp = (lakes or polys) if is_lake else polys
    if sp:
        return "special", min(sp, key=lambda z: z["area"])
    if not is_lake:
        d7 = [z for z in zs if z["role"] == "d700" and z["primary_codes"][0] not in ("7.00(b)(1)",)]
        if d7:
            return "district", min(d7, key=lambda z: z["area"])
    return "statewide", None


# ════════════════════════════════════════════════════════════════════════════
# main
# ════════════════════════════════════════════════════════════════════════════

def main():
    os.makedirs(os.path.join(OUT, "tiles"), exist_ok=True)
    qa = []
    rules, zones_raw, state = load_cdfw()
    zones = classify_zones(rules, zones_raw)
    waters, lakes = load_osm(state)
    add_derived_zones(zones, rules, waters, lakes)
    apply_geometry_fixes(zones, waters, qa)
    for i, w in enumerate(waters):
        w["wid"] = i
    az = build_assignment_zones(zones)
    match_line_zones(az, waters)
    tree = STRtree([z["poly"] for z in az])

    # Statewide pseudo-rules
    rules.setdefault("5.85(a)(2)", {})
    sw_stream_days = seasons.statewide_stream_days()

    # ── regimes (primary rules) ──────────────────────────────────────────────
    regimes, regime_ix = [], {}

    def regime_for(kind, z, is_lake):
        if kind == "statewide":
            key = ("statewide", "lake" if is_lake else "stream")
        else:
            key = (kind, z["fid"])
        if key in regime_ix:
            return regime_ix[key]
        if kind == "statewide":
            if is_lake:
                rec = {"kind": "statewide", "codes": ["5.85(a)(1)"], "title": "Statewide trout regulations — lakes, reservoirs and ponds",
                       "days": "o" * 365}
            else:
                rec = {"kind": "statewide", "codes": ["5.85(a)(2)"], "title": "Statewide trout regulations — streams, rivers and canals",
                       "days": sw_stream_days}
        else:
            rows = []
            for c in z["primary_codes"]:
                rows += (rules.get(c) or {}).get("rows", [])
            rows, ref_note = resolve_refs(rows, rules, z)
            if z.get("klamath_basin"):
                days, notes = "c" * 365, []
            elif ref_note == "statewide":
                days, notes = sw_stream_days, []
            else:
                days, notes = seasons.rows_to_days(rows)
            if days is None:
                qa.append(f"UNPARSED SEASON  {z['primary_codes']}  {z['name'][:70]}  {notes[:2]}")
            hdr = None
            for c in z.get("header_codes", []):
                r = rules.get(c)
                if r and r["rows"]:
                    hdr = r["rows"][0]["loc"]
            rec = {"kind": kind, "codes": z["primary_codes"], "header_codes": z.get("header_codes", []),
                   "title": hdr or z["name"], "reach": z["name"], "days": days, "zone": z["fid"],
                   "note": z.get("note", "") or (ref_note if ref_note not in ("", "statewide") else "") or
                           ("Covered under the statewide stream regulation §5.85(a)(2)." if ref_note == "statewide" else "") or
                           ("All anadromous waters of the Klamath River Basin are closed to all fishing all year except the reaches listed "
                            "in §7.40(b)(50)(E). Applies only to waters accessible to salmon and steelhead." if z.get("klamath_basin") else ""),
                   "anadromous_only": bool(z.get("klamath_basin")) or kind == "district" and "anadromous" in (z["name"] + " ".join(r.get("loc", "") for r in rows)).lower()}
        regime_ix[key] = len(regimes)
        regimes.append(rec)
        return regime_ix[key]

    rulesets, ruleset_ix = [], {}

    def ruleset_for(codes, district):
        key = (tuple(sorted(set(codes), key=code_key)), district)
        if key not in ruleset_ix:
            ruleset_ix[key] = len(rulesets)
            rulesets.append({"codes": list(key[0]), "district": district})
        return ruleset_ix[key]

    def area_info(mem, primary_zone):
        codes, district = [], None
        for i in mem:
            z = az[i]
            if z.get("district"):
                district = z["district"]
            if z is primary_zone:
                continue
            if z["role"] in ("area", "area_line", "d700"):
                codes += z["area_codes"]
                if z["role"] == "d700" and z is not primary_zone:
                    codes += z["primary_codes"]          # e.g. 7.00(b)(1) salmon closure
            elif z["role"] in ("special_info", "special_info_line"):
                codes += z.get("info_codes", [])
            elif z["role"].startswith("special"):
                codes += z["primary_codes"]              # overlapping special waters
                codes += z.get("info_codes", [])
        if primary_zone is not None:
            codes += primary_zone.get("area_codes", [])
            codes += primary_zone.get("info_codes", [])
        codes = [c for c in codes if category_of(c) not in ("definition", "other")]
        if primary_zone is not None:
            codes = [c for c in codes if c not in primary_zone["primary_codes"]]
        return codes, district

    # Simplified district outlines (holes filled) for pieces that fall in gaps
    # between CDFW's district polygons (lake holes, shoreline slivers).
    dsimple = []
    for z in az:
        if z.get("district"):
            filled = unary_union([Polygon(p.exterior) for p in polys_of(z["poly"])]).simplify(0.005)
            dsimple.append((z["district"], filled, shapely.prepared.prep(filled)))

    def nearest_district(g):
        pt = g.representative_point()
        for name, poly, pp in dsimple:
            if pp.contains(pt):
                return name
        return min(dsimple, key=lambda d: d[1].distance(pt))[0] if dsimple else None

    # ── streams ──────────────────────────────────────────────────────────────
    log("Splitting streams by regulation zones…")
    feats = []        # (geom, props)
    n_special = Counter()
    for k, w in enumerate(waters):
        cidx = tree.query(w["geom"], predicate="intersects")
        cands = sorted((az[i] for i in cidx), key=lambda z: -z["area"])
        for g, mem in split_water(w, cands):
            kind, pz = choose_primary(mem, az, False, w["name"])
            rid = regime_for(kind, pz, False)
            codes, district = area_info(mem, pz)
            district = district or nearest_district(g)
            rs = ruleset_for(codes, district)
            n_special[kind] += g.length
            feats.append((g, {"n": w["name"], "k": w["kind"][0], "r": rid, "s": rs}))
        if k % 2000 == 0:
            log(f"  {k}/{len(waters)} streams, {len(feats)} pieces")
    log(f"  {len(feats)} stream pieces; km by rule kind: " + ", ".join(f"{a}={km(b):.0f}" for a, b in n_special.items()))

    # ── lakes ────────────────────────────────────────────────────────────────
    log("Assigning lakes…")
    lake_area_hits = Counter()
    for l in lakes:
        g = l["geom"]
        pt = g.representative_point()
        mem = set()
        extra_codes = []
        for i in tree.query(g, predicate="intersects"):
            z = az[i]
            if z["role"] == "special_lake":
                if re.search(r"tributar|mouth", z["name"], re.I):
                    # strips at tributary mouths: the lake isn't governed by them, list as "also applies"
                    if z["poly"].intersects(g):
                        mem.add(i) if False else extra_codes.append(z["primary_codes"])
                    continue
                inter = z["poly"].intersection(g).area if z["poly"].intersects(g) else 0
                if inter > 0.4 * g.area:
                    mem.add(i)
            elif z["role"] in ("special_line", "area_line"):
                continue
            elif z["role"] == "special_poly":
                # "Goose Lake and tributaries" governs the lake too; "Almanor Lake tributaries" does not
                if lake_named_in_area(l["name"], z["name"]) and z["prep"].contains(pt):
                    mem.add(i)
                    lake_area_hits[z["name"][:60]] += 1
                continue
            elif z["prep"].contains(pt):
                mem.add(i)
        kind, pz = choose_primary(mem, az, True, l["name"])
        rid = regime_for(kind, pz, True)
        codes, district = area_info(mem, pz)
        for c in extra_codes:
            codes += c
        district = district or nearest_district(g)
        rs = ruleset_for(codes, district)
        feats.append((g, {"n": l["name"], "k": "l", "r": rid, "s": rs}))

    for nm, n in sorted(lake_area_hits.items()):
        log(f"  lake governed by special area: {nm} ({n} polygon{'s' if n > 1 else ''})")

    # ── CDFW special features drawn as-is (authoritative reach geometry) ─────
    log("Adding CDFW special-water geometry…")
    special_feats = []
    for z in zones:
        if not z["role"].startswith("special") or z["role"].startswith("special_info"):
            continue
        if z.get("derived") and z["role"] == "special_poly":
            continue           # traced networks are already applied to stream pieces
        if z.get("klamath_basin"):
            continue           # basin-wide polygon: applied to streams, not drawn
        kind_g = "line" if "Line" in z["geom"].geom_type else "poly"
        rid = regime_for("special", z, z["role"] == "special_lake")
        rp = z["geom"].representative_point() if kind_g == "poly" else z["geom"].interpolate(0.5, normalized=True) if z["geom"].geom_type == "LineString" else z["geom"].representative_point()
        mem = {i for i in tree.query(rp) if az[i]["role"] in ("area", "d700") and az[i]["prep"].contains(rp)}
        codes, district = area_info(mem, z)
        district = district or nearest_district(z["geom"])
        rs = ruleset_for(codes, district)
        special_feats.append((z["geom"], {"n": head_of(z["name"]) or z["name"], "k": "S" if kind_g == "line" else ("A" if z["role"] == "special_poly" else "L"),
                                          "r": rid, "s": rs, "f": z["fid"]}))

    # ── QA: which special entries ended up on the map ─────────────────────────
    pdf = json.load(open(os.path.join(DATA, "pdf_entries.json"))) if os.path.exists(os.path.join(DATA, "pdf_entries.json")) else {}
    mapped = defaultdict(set)
    for r in regimes:
        for c in r["codes"] + r.get("header_codes", []):
            m = re.match(r"(7\.[45]0)\(b\)\((\d+)\)", c)
            if m:
                mapped[m.group(1)].add(int(m.group(2)))
    for sec, ents in pdf.items():
        miss = [(n, nm) for n, nm in ents if n not in mapped[sec] and not nm.lower().startswith("repealed")
                and not REFERS.get(f"{sec}({n})")]
        qa.append(f"§{sec}: {len(ents)} entries in PDF, {len(mapped[sec])} on map, {len(miss)} missing")
        for n, nm in miss:
            qa.append(f"   missing §{sec}(b)({n}) {nm[:90]}")

    # Special lines that matched no OSM stream by name (still drawn from CDFW geometry)
    matched = set()
    for g, p in feats:
        rg = regimes[p["r"]]
        if rg["kind"] == "special":
            matched.add(rg["zone"])
    for z in az:
        if z["role"] == "special_line" and z.get("matched_by") != "name":
            qa.append(f"LINE MATCHED BY {z.get('matched_by','?').upper():8}  {z['primary_codes']}  {z['name'][:80]}")

    write_outputs(rules, regimes, rulesets, feats, special_feats, qa)


def simplify(g, tol):
    s = g.simplify(tol, preserve_topology=False)
    return s if not s.is_empty else g


def write_outputs(rules, regimes, rulesets, feats, special_feats, qa):
    log("Writing outputs…")
    # Only keep rules referenced somewhere, plus statewide/district context
    used = set()
    for r in regimes:
        used.update(r["codes"]); used.update(r.get("header_codes", []))
    for rs in rulesets:
        used.update(rs["codes"])
    used.update(STATEWIDE_CODES)
    # include parents of used codes (for headers like "Sierra District" or entry names)
    for c in list(used):
        while "(" in c:
            c = c[: c.rfind("(")]
            if c in rules:
                used.add(c)
    used.update(["5.85", "5.85(a)", "5.85(a)(1)", "5.85(a)(2)", "7.00", "7.40(a)", "7.50(a)"])
    rules_out = {c: rules[c] for c in sorted(used, key=code_key) if c in rules and rules[c]}
    meta = {"generated": time.strftime("%Y-%m-%d %H:%M"), "year": seasons.YEAR,
            "sources": ["CDFW RFRService Feature Server (official reaches, areas and regulation text)",
                        "OpenStreetMap named waterways (stream network)",
                        "2026 Freshwater Sport Fishing Regulations booklet (entries missing from CDFW data)"]}
    json.dump({"meta": meta, "rules": rules_out, "regimes": regimes, "rulesets": rulesets, "unmapped": UNMAPPED, "refers": REFERS,
               "statewide": STATEWIDE_CODES},
              open(os.path.join(OUT, "rules.json"), "w", encoding="utf-8"), separators=(",", ":"), ensure_ascii=False)

    # Tiles (full detail, lightly simplified)
    tiles = defaultdict(list)
    search = {}
    for g, p in feats + special_feats:
        gs = simplify(g, 0.00004)
        minx, miny, maxx, maxy = gs.bounds
        for tx in range(int(math.floor(minx / TILE)), int(math.floor(maxx / TILE)) + 1):
            for ty in range(int(math.floor(miny / TILE)), int(math.floor(maxy / TILE)) + 1):
                cell = box(tx * TILE, ty * TILE, (tx + 1) * TILE, (ty + 1) * TILE)
                if not gs.intersects(cell):
                    continue
                part = gs if cell.contains(gs) else gs.intersection(cell)
                if part.is_empty:
                    continue
                try:
                    tiles[(tx, ty)].append({"type": "Feature", "properties": p, "geometry": geom_json(part)})
                except ValueError:
                    pass
        # search index: one entry per name+regime, positioned at a representative point
        rp = g.representative_point() if g.geom_type.endswith("Polygon") else g.interpolate(0.5, normalized=True) if g.geom_type == "LineString" else g.representative_point()
        key = (p["n"], p["r"])
        ln = g.length if "Line" in g.geom_type else 0
        if key not in search or ln > search[key][4]:
            search[key] = [p["n"], round(rp.y, 4), round(rp.x, 4), p["r"], ln, p["k"], [round(v, 3) for v in g.bounds]]
    tdir = os.path.join(OUT, "tiles")
    for f in os.listdir(tdir):
        os.remove(os.path.join(tdir, f))
    for (tx, ty), fs in tiles.items():
        json.dump({"type": "FeatureCollection", "features": fs}, open(os.path.join(tdir, f"{tx}_{ty}.json"), "w", encoding="utf-8"),
                  separators=(",", ":"), ensure_ascii=False)
    # Manifest: the app checks it so a partial copy of tiles/ shows a warning instead of a blank map
    json.dump({"tile": TILE, "generated": time.strftime("%Y-%m-%d %H:%M"),
               "tiles": {f"{tx}_{ty}": len(fs) for (tx, ty), fs in sorted(tiles.items())}},
              open(os.path.join(tdir, "index.json"), "w", encoding="utf-8"), separators=(",", ":"))
    log(f"  {len(tiles)} tiles")

    # Overview: rivers + special waters + big lakes, heavily simplified
    ov = []
    for g, p in feats + special_feats:
        big = p["k"] in ("r", "S", "A", "L") or (p["k"] == "l" and g.area > 2e-5) or (p["k"] == "s" and g.length > 0.08)
        if not big:
            continue
        gs = simplify(g, 0.0012)
        if gs.is_empty:
            continue
        try:
            ov.append({"type": "Feature", "properties": p, "geometry": geom_json(gs, 4)})
        except ValueError:
            pass
    json.dump({"type": "FeatureCollection", "features": ov}, open(os.path.join(OUT, "overview.json"), "w", encoding="utf-8"),
              separators=(",", ":"), ensure_ascii=False)
    log(f"  overview: {len(ov)} features")

    s = sorted(([v[0], v[1], v[2], v[3], v[5], v[6]] for v in search.values()), key=lambda r: r[0].lower())
    json.dump(s, open(os.path.join(OUT, "search.json"), "w", encoding="utf-8"), separators=(",", ":"), ensure_ascii=False)
    log(f"  search index: {len(s)} entries")

    with open(os.path.join(OUT, "qa_report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(qa) + "\n")
    log(f"  QA lines: {len(qa)}")


if __name__ == "__main__":
    main()
