# California Freshwater Fishing Regulations Map (2026)

An interactive map of the 2026 California freshwater sport fishing regulations. Click any named stream, river, or lake to see the rule that governs it, its season for the selected date, and every other rule that applies at that spot.

**Live map:** https://akmcclary.github.io/CA_Fishing_Regs/

Always confirm with the official CDFW Freshwater Sport Fishing Regulations and any emergency closures before fishing. This map is not an official CDFW product.

## How each water gets its rules

Every named stream is split wherever a regulation area begins or ends. Each piece takes its governing rule in this order:

1. A special regulation reach or area for that water (CCR Title 14, §7.40(b) and §7.50(b))
2. The district general regulation (§7.00)
3. The statewide trout regulation (§5.85)

Fishing hours, bait, gear, and species rules that cover the location are listed with every water.

## Stocking layer

The **Stocking** button adds CDFW hatchery plants as an optional layer. It loads live from the public layer behind the CDFW Fishing Guide (`FishingGuide/FeatureServer/2` on services2.arcgis.com), so it needs no rebuild. Each point is one water. Its color shows whether the water is scheduled, was planted this week, was planted in the last 3 weeks, or was planted earlier in the past 12 months. Selecting a lake or stream lists the stocking sites that belong to it.

## Repository layout

| Path | Purpose |
|---|---|
| `index.html` | The web app (Leaflet 1.9.4, no build step) |
| `data/map/` | Data the app loads: `rules.json`, `overview.json`, `search.json`, and `tiles/` |
| `data/map/tiles/index.json` | Tile manifest. The app warns if a listed tile is missing. |
| `build_map_data.py` | Builds everything in `data/map/` from the inputs below |
| `seasons.py` | Converts season text into an open, restricted, or closed status for each day of the year |
| `data/cdfw_features.geojson.gz` | CDFW RFRService reaches, areas, and regulation text (from `fetch_cdfw.py`) |
| `data/default_waters.geojson.gz` | OpenStreetMap named streams, rivers, and lakes (from `fetch_default_waters.py`) |
| `data/special_additions.json` | Hand-built special waters that CDFW has no geometry for |
| `data/pdf_entries.json` | Entry list from the regulation booklet, used for the QA report |
| `serve.py`, `Start Map.bat` | Local web server for testing |

The large inputs are stored gzipped because GitHub rejects files over 100 MB. `build_map_data.py` reads the `.gz` files directly. The older scripts (`cdfw_to_streams.py`, `fetch_default_waters.py`, `fetch_streams.py`, `fetch_nhd.py`) expect the uncompressed files, so decompress them first if you run those.

## Rebuild the map data

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python build_map_data.py
```

The build takes about 3 minutes. Check `data/map/qa_report.txt` for special-regulation entries that did not reach the map.

## Run locally

Double-click `Start Map.bat`, or run `.venv\Scripts\python serve.py`. Opening `index.html` directly from disk does not work because the browser blocks the data requests.

## Publish with GitHub Pages

1. Commit and push to `main`.
2. In the repository on GitHub, go to **Settings** > **Pages**.
3. Under **Build and deployment**, set **Source** to **Deploy from a branch**, then select `main` and `/ (root)`.

The `.nojekyll` file tells Pages to serve the files as they are.

## Data sources

* CDFW RFRService Feature Server (official reaches, areas, and regulation text)
* OpenStreetMap named waterways, © OpenStreetMap contributors
* 2026 Freshwater Sport Fishing Regulations booklet (CDFW)
* CDFW Fish Planting Schedule (hatchery plants, queried live)
* Basemaps: USGS The National Map and OpenStreetMap
