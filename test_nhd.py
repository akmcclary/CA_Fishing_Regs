"""
Test Nominatim polygon_geojson=1 for waterway geometry + quality check.
"""
import requests, json, time

NOM_URL = "https://nominatim.openstreetmap.org/search"
HEADERS = {"User-Agent": "CA-Fish-Reg-Map/1.0 (educational)"}

CA_XMIN, CA_YMIN, CA_XMAX, CA_YMAX = -124.48, 32.53, -114.13, 42.01

def in_ca(lon, lat):
    return CA_XMIN <= lon <= CA_XMAX and CA_YMIN <= lat <= CA_YMAX

def filter_to_ca(geom):
    """Filter MultiLineString/LineString coordinates to CA only."""
    gtype = geom.get("type")
    if gtype == "LineString":
        coords = [c for c in geom["coordinates"] if in_ca(c[0], c[1])]
        return {"type": "LineString", "coordinates": coords} if coords else None
    elif gtype == "MultiLineString":
        lines = []
        for line in geom["coordinates"]:
            ca_pts = [c for c in line if in_ca(c[0], c[1])]
            if len(ca_pts) >= 2:
                lines.append(ca_pts)
        if lines:
            if len(lines) == 1:
                return {"type": "LineString", "coordinates": lines[0]}
            return {"type": "MultiLineString", "coordinates": lines}
    return None

def nom_waterway(name, county=""):
    q = f"{name}, {county + ' County, ' if county else ''}California, USA"
    params = {
        "q": q,
        "format": "json",
        "limit": 5,
        "countrycodes": "us",
        "polygon_geojson": "1",
    }
    time.sleep(1.1)
    r = requests.get(NOM_URL, params=params, headers=HEADERS, timeout=15)
    results = r.json()
    # Prefer waterway class results
    for res in results:
        if res.get("class") == "waterway" or res.get("type") in ["river", "stream", "lake", "reservoir"]:
            geom = res.get("geojson")
            if geom and geom.get("type") in ("LineString", "MultiLineString"):
                ca_geom = filter_to_ca(geom)
                if ca_geom:
                    return ca_geom, res
    return None, None

tests = [
    ("Truckee River", "Nevada"),
    ("Pole Creek", "Placer"),
    ("East Fork Carson River", "Alpine"),
    ("American River", "Sacramento"),
    ("Salinas River", "Monterey"),
    ("San Joaquin River", "Fresno"),
    ("McCloud River", "Shasta"),
]

for name, county in tests:
    geom, meta = nom_waterway(name, county)
    if geom:
        if geom["type"] == "MultiLineString":
            tot_pts = sum(len(s) for s in geom["coordinates"])
            segs = len(geom["coordinates"])
        else:
            tot_pts = len(geom["coordinates"])
            segs = 1
        print(f"  OK  {name}: {segs} segments, {tot_pts} pts  [{meta.get('class')}/{meta.get('type')}]")
    else:
        print(f"  --- {name}: no CA waterway geometry")
