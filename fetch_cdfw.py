"""
fetch_cdfw.py

Downloads the official CDFW Recreational Fishing Regulations dataset from the
ArcGIS Feature Server that backs https://apps.wildlife.ca.gov/sportfishingregs/
and produces self-contained data files.

Source service:
  https://services2.arcgis.com/Uq9r85Potqm3MfRV/arcgis/rest/services/RFRService/FeatureServer

Layers used:
  0  RFRInlandLines      polylines  (rivers/creeks — pre-clipped to reaches)
  1  RFRInlandPolygons   polygons   (lakes/reservoirs/lagoons)
  2  regulations         table      (CCR Title 14 verbatim text)

Each line/polygon feature has a GlobalID; the regulations table has one or
more rows whose GlobalID equals the *feature's* GlobalID.  We join them so
each feature ships with its full set of regulation rows.

Outputs:
  data/cdfw_features.geojson   FeatureCollection — one feature per pre-clipped
                                reach, with all matching regulation rows in
                                feature.properties.regulations[].
  data/cdfw_regulations.json   raw dump of the regulations table (1,195 rows).

Usage:
  python fetch_cdfw.py            # download everything, overwrite outputs
  python fetch_cdfw.py --no-cache # bypass any cached HTTP responses
"""

import json
import os
import re
import sys
import time
import requests

BASE = (
    "https://services2.arcgis.com/Uq9r85Potqm3MfRV/"
    "arcgis/rest/services/RFRService/FeatureServer"
)

OUT_DIR     = "data"
OUT_GEO     = os.path.join(OUT_DIR, "cdfw_features.geojson")
OUT_REGS    = os.path.join(OUT_DIR, "cdfw_regulations.json")
CACHE_DIR   = os.path.join(OUT_DIR, "cache_cdfw")

HEADERS = {
    "User-Agent": "CA-Fish-Reg-Map/1.0 (+https://github.com/akmcclary; contact: akmcclary@gmail.com)",
    "Accept":     "application/json",
}

PAGE_SIZE = 2000  # the service's maxRecordCount


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _cache_path(tag):
    import hashlib
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, hashlib.md5(tag.encode()).hexdigest() + ".json")


def _http_get_json(url, params, cache_tag, use_cache=True):
    """GET → JSON with optional on-disk caching."""
    cp = _cache_path(cache_tag)
    if use_cache and os.path.exists(cp):
        with open(cp, encoding="utf-8") as f:
            return json.load(f)
    r = requests.get(url, params=params, headers=HEADERS, timeout=90)
    r.raise_for_status()
    data = r.json()
    if use_cache:
        with open(cp, "w", encoding="utf-8") as f:
            json.dump(data, f)
    return data


# ── Layer downloaders ─────────────────────────────────────────────────────────

def fetch_layer_geojson(layer_id, layer_name, use_cache=True):
    """
    Pull every feature from a polyline/polygon layer as GeoJSON.  Pages by
    OBJECTID using resultOffset until the layer is exhausted.
    """
    url    = f"{BASE}/{layer_id}/query"
    feats  = []
    offset = 0

    while True:
        params = {
            "where":             "1=1",
            "outFields":         "feat_desc,category,reg_definition,GlobalID",
            "returnGeometry":    "true",
            "outSR":             "4326",
            "f":                 "geojson",
            "resultRecordCount": str(PAGE_SIZE),
            "resultOffset":      str(offset),
            # Sort so paging is deterministic
            "orderByFields":     "OBJECTID",
        }
        cache_tag = f"layer{layer_id}:offset{offset}"
        print(f"  GET {layer_name} (offset={offset})…", flush=True)
        data = _http_get_json(url, params, cache_tag, use_cache=use_cache)

        page  = data.get("features", []) or []
        feats.extend(page)
        exceeded = data.get("exceededTransferLimit") or len(page) >= PAGE_SIZE
        if not page or not exceeded:
            break
        offset += len(page)
        time.sleep(0.3)  # be polite

    print(f"    → {len(feats)} features", flush=True)
    return feats


def fetch_regulations(use_cache=True):
    """Pull every row of the regulations table (layer 2)."""
    url    = f"{BASE}/2/query"
    rows   = []
    offset = 0

    while True:
        params = {
            "where":             "1=1",
            "outFields":         "Source,Code,ParentCode,Verbatim,Title,Tags,"
                                 "RuleType,FeatureCategory,GlobalID",
            "returnGeometry":    "false",
            "f":                 "json",
            "resultRecordCount": str(PAGE_SIZE),
            "resultOffset":      str(offset),
            "orderByFields":     "id",
        }
        cache_tag = f"layer2:offset{offset}"
        print(f"  GET regulations (offset={offset})…", flush=True)
        data = _http_get_json(url, params, cache_tag, use_cache=use_cache)

        page = data.get("features", []) or []
        for feat in page:
            rows.append(feat.get("attributes", {}))
        exceeded = data.get("exceededTransferLimit") or len(page) >= PAGE_SIZE
        if not page or not exceeded:
            break
        offset += len(page)
        time.sleep(0.3)

    print(f"    → {len(rows)} regulation rows", flush=True)
    return rows


# ── Section / status derivation ───────────────────────────────────────────────

_RE_SECTION = re.compile(r"(\d+\.\d+)")


def section_of(reg_definition):
    """Pull the leading 'major.minor' code (e.g. '7.40') from a reg_definition."""
    if not reg_definition:
        return ""
    first = reg_definition.split(",")[0].strip()
    m = _RE_SECTION.match(first)
    return m.group(1) if m else ""


def status_from_regulations(matching_regs):
    """
    Crude open/restricted/closed/mixed roll-up across all regulations linked to
    a feature.  Mirrors the heuristic used by index.html so the existing UI
    can colour these features the same way.
    """
    if not matching_regs:
        return "unknown"

    statuses = set()
    for r in matching_regs:
        text = " ".join(
            (r.get(k) or "") for k in ("Verbatim", "Title", "Tags")
        ).lower()
        if "closed to all fishing all year" in text:
            statuses.add("closed")
        elif any(k in text for k in (
            "artificial", "barbless", "catch and release", "size limit",
            "minimum size", "bag and possession",
        )):
            statuses.add("restricted")
        elif r.get("Verbatim"):
            statuses.add("open")

    if not statuses:
        return "unknown"
    if len(statuses) == 1:
        return statuses.pop()
    return "mixed"


# ── County extraction (best-effort, from feat_desc) ───────────────────────────

CA_COUNTIES = [
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
]


def counties_from_text(text):
    """Pull every CA county name that appears in the text (e.g. '(Sacramento Co.)')."""
    if not text:
        return []
    found = []
    # Sort longer names first so 'San Luis Obispo' matches before 'San'
    for cnty in sorted(CA_COUNTIES, key=len, reverse=True):
        pat = r"(?<![A-Za-z])" + re.escape(cnty) + r"(?![A-Za-z])"
        if re.search(pat, text):
            found.append(cnty)
    found.sort(key=lambda c: text.find(c))
    return found


# ── Feature assembly ──────────────────────────────────────────────────────────

def build_feature(raw_feat, geom_kind, regs_by_gid):
    """
    Convert one raw GeoJSON feature from the CDFW service into our enriched
    output format.
    """
    props = raw_feat.get("properties", {}) or {}
    gid   = props.get("GlobalID")
    name  = props.get("feat_desc") or "(unnamed)"

    matching = regs_by_gid.get(gid, [])
    reg_def  = props.get("reg_definition") or ""

    return {
        "type":     "Feature",
        "geometry": raw_feat.get("geometry"),
        "properties": {
            "id":              gid,
            "name":            name,
            "category":        props.get("category") or "",
            "reg_definition":  reg_def,
            "section":         section_of(reg_def),
            "geom_kind":       geom_kind,            # "line" / "polygon"
            "counties":        counties_from_text(name),
            "status":          status_from_regulations(matching),
            "regulations":     matching,             # full CCR rows
            "n_regulations":   len(matching),
        },
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    os.makedirs(OUT_DIR, exist_ok=True)

    use_cache = "--no-cache" not in sys.argv
    if not use_cache:
        print("(--no-cache: bypassing local HTTP cache)\n")

    print(f"Fetching from {BASE}\n")

    # 1) Polylines
    print("Layer 0: RFRInlandLines")
    lines = fetch_layer_geojson(0, "RFRInlandLines", use_cache)
    print()

    # 2) Polygons
    print("Layer 1: RFRInlandPolygons")
    polys = fetch_layer_geojson(1, "RFRInlandPolygons", use_cache)
    print()

    # 3) Regulations table
    print("Layer 2: regulations")
    regs = fetch_regulations(use_cache)
    print()

    # ── Save raw regulations ──────────────────────────────────────────────────
    with open(OUT_REGS, "w", encoding="utf-8") as f:
        json.dump({
            "source": BASE + "/2",
            "count":  len(regs),
            "rows":   regs,
        }, f, ensure_ascii=False)
    print(f"Saved {len(regs):>5} regulation rows → {OUT_REGS}")

    # ── Index regulations by their referenced feature GlobalID ────────────────
    regs_by_gid = {}
    for r in regs:
        gid = r.get("GlobalID")
        if gid:
            regs_by_gid.setdefault(gid, []).append(r)
    print(f"          {len(regs_by_gid):>5} distinct features referenced by ≥1 reg row")

    # ── Build the joined FeatureCollection ────────────────────────────────────
    out_features = []
    for raw in lines:
        out_features.append(build_feature(raw, "line",    regs_by_gid))
    for raw in polys:
        out_features.append(build_feature(raw, "polygon", regs_by_gid))

    # Tally
    n_lines   = sum(1 for f in out_features if f["properties"]["geom_kind"] == "line")
    n_polys   = sum(1 for f in out_features if f["properties"]["geom_kind"] == "polygon")
    n_with    = sum(1 for f in out_features if f["properties"]["regulations"])
    sections  = {}
    for f in out_features:
        s = f["properties"]["section"] or "(none)"
        sections[s] = sections.get(s, 0) + 1

    fc = {
        "type": "FeatureCollection",
        "metadata": {
            "source":         BASE,
            "fetched":        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "lines":          n_lines,
            "polygons":       n_polys,
            "regulations":    len(regs),
            "linked_features": n_with,
        },
        "features": out_features,
    }
    with open(OUT_GEO, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False)

    print(f"\nWrote {len(out_features):>5} features → {OUT_GEO}")
    print(f"   {n_lines:>5} polylines")
    print(f"   {n_polys:>5} polygons")
    print(f"   {n_with:>5} with ≥1 linked regulation row")
    print(f"\nFeature counts by leading section code:")
    for s in sorted(sections):
        print(f"   {s:>10s}: {sections[s]}")


if __name__ == "__main__":
    main()
