# CA Freshwater Fishing Regulations Map — Project Context

## Project Goal
Interactive single-page web app showing California freshwater fishing regulations on a Leaflet.js map. Data sourced from CA DFW regulation text, geocoded to actual waterway geometry.

## File Structure
```
C:\Users\amcclary\Documents\CA_Fish_Reg_Map\
├── index.html                  # Full front-end app (Leaflet.js, ~26KB)
├── fetch_streams.py            # Geocoder script (Overpass API + NLDI fallback)
├── data/
│   ├── streams.geojson         # 230 geocoded waterway features
│   └── regulations.json        # 292 regulation entries (all streams)
└── .venv/                      # Python virtual environment
```

## Data Summary

### `data/streams.geojson` — 230 features
Properties per feature:
```json
{
  "id": "stream_001",
  "name": "Alameda Creek",
  "county": "Alameda",
  "section": "7.50(b)(1)",
  "status": "restricted",
  "segments": "...",
  "lat": 37.5,
  "lng": -121.9,
  "source": "osm",
  "qualifier_text": "above Niles Dam",
  "needs_review": false
}
```

**Source breakdown:**
- `osm`: 129 features (Overpass API line/MultiLineString geometry)
- `point`: 54 features (approximate centroid only)
- `nldi`: 47 features (USGS NHD network reaches)

**Status breakdown:**
- `restricted`: 108
- `open`: 48
- `closed`: 43
- `mixed`: 31

**Special flags:**
- `needs_review: true`: 12 features — streams with geographic qualifiers like "above Folsom Lake" or "below Fish Barrier Dam" that couldn't be auto-resolved to a boundary
- 62 stream IDs in `regulations.json` have **no corresponding geometry** (no entry in `streams.geojson`)

### `data/regulations.json` — 292 entries
Keys per entry: `{id, section, entry_num, name, county, segments, lat, lng, status}`

## Tech Stack
- **Front-end**: Vanilla JS, Leaflet.js 1.9.4 (CDN), single `index.html`
- **Map tiles**: OpenStreetMap
- **Geocoder**: Python 3, Overpass API (primary), NLDI/USGS (fallback), Nominatim (point fallback)
- **Dev server**: `python -m http.server 8765` → http://localhost:8765

## `index.html` — Key Features
1. **Sidebar** with searchable/filterable stream list
2. **Filter buttons**: All / Open / Closed / Restricted / Mixed / ⚠ Needs Review
3. **Stream list items**: Show ⚠ badge if `needs_review`, dimmed "no location" label if no geometry
4. **Map popups**: Show source tag (OSM / NLDI / ~Approx), qualifier boundary text, review warning
5. **Detail panel**: Full regulation info, qualifier text box, review alert, no-map alert for unmapped streams
6. **Legend**: Color-coded by status + Needs Review + No Location entries
7. **Data merge in `loadData()`**: GeoJSON properties (`source`, `qualifier_text`, `needs_review`, `noGeometry`) merged into regulation objects from `regulations.json`

### Key JS Functions
- `loadData()` — fetches both JSON files, merges geo props, calls `renderList()` and `renderMap()`
- `getVisibleStreams()` — applies active status filter + search query; handles `needs_review` as special filter
- `renderList()` — builds sidebar DOM from filtered streams
- `renderMap()` — creates Leaflet GeoJSON layers + circle markers for point sources
- `buildPopupHtml(stream)` — constructs map popup HTML with source/qualifier/review info
- `showDetail(stream)` — populates right-side detail panel

## `fetch_streams.py` — Geocoder Logic
1. Load `data/regulations.json`
2. For each stream, query **Overpass API** for `way["name"=...]["waterway"]` within California bounds
3. Strip segments outside CA (lat 32–42, lng -125 to -114)
4. Fallback to **NLDI** (USGS upstream-main network trace from lat/lng seed point) for lakes/reservoirs
5. Fallback to **Nominatim** point if all else fails
6. Save checkpoint every 20 features to `data/streams_checkpoint.json`
7. Output: `data/streams.geojson`

## Known Issues / Pending Work
- **62 unmapped streams**: Present in `regulations.json` but have no geometry. Candidates for manual geocoding or additional Overpass queries.
- **12 needs_review streams**: These have "above X / below Y" geographic qualifiers that require a human to verify the correct boundary segment. Examples:
  - Calaveras River (above New Hogan Reservoir)
  - Carmel River (below Los Padres Dam)
  - American River North/Middle/South Fork (various dams)
  - Feather River, Guadalupe River, Trinity River, etc.
- **Pole Creek**: Was previously returning wrong-state geometry via Nominatim. Fixed by switching to Overpass API — now correctly shows 3-part MultiLineString in Placer County (lon -120.26 to -120.21, lat 39.23–39.24).

## Potential Next Steps
1. Manually geocode the 62 missing streams (add to `streams.geojson`)
2. Add precise boundary markers for the 12 `needs_review` streams
3. Export a printable review list of needs_review streams for field verification
4. Add regulation detail text (bag limits, gear restrictions) to the detail panel
5. Style map lines by status color (currently popups are colored but lines may be uniform)
6. Add county boundary overlay layer

## Running the App
```powershell
cd C:\Users\amcclary\Documents\CA_Fish_Reg_Map
.venv\Scripts\python -m http.server 8765
# Open http://localhost:8765
```

## Re-running the Geocoder
```powershell
cd C:\Users\amcclary\Documents\CA_Fish_Reg_Map
.venv\Scripts\python fetch_streams.py
# Resumes from checkpoint if data/streams_checkpoint.json exists
# Output: data/streams.geojson
```
