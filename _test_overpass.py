"""Quick smoke test: verify Overpass returns the right streams."""
import fetch_streams

tests = [
    ("Pole Creek and tributaries", "Placer"),
    ("American River", "Placer and El Dorado"),
    ("Truckee River", "Nevada, Placer, and Sierra"),
    ("Sacramento River", "Many"),
    ("Hot Creek", "Mono"),
]

for name, county in tests:
    q = fetch_streams.parse_qualifiers(name)
    print(f"\n{'='*60}")
    print(f"Reg:     {name}")
    print(f"Base:    {q['base_name']}")
    print(f"Qualif:  {q['qualifier_text']}")
    geom, lat, lon = fetch_streams._overpass_stream(name, county)
    if geom:
        t = geom["type"]
        n = len(geom["coordinates"]) if t == "MultiLineString" else 1
        sample = (geom["coordinates"][0][:2] if t == "MultiLineString"
                  else geom["coordinates"][:2])
        print(f"Result:  {t}, {n} part(s), centroid=[{lon:.4f},{lat:.4f}]")
        print(f"Sample:  {sample}")
    else:
        print(f"Result:  NOT FOUND")
