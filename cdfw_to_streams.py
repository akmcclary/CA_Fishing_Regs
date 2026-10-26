"""
cdfw_to_streams.py

Convert the CDFW data downloaded by fetch_cdfw.py into the schema that
index.html and the existing pipeline already understand.

Inputs:
  data/cdfw_features.geojson        from fetch_cdfw.py
  data/cdfw_regulations.json        from fetch_cdfw.py

Outputs (overwrite the prior parser's outputs):
  data/regulations.json   { "streams": [ ... ] }   one stream per CDFW feature
  data/streams.geojson    FeatureCollection         one feature per CDFW feature
                                                     with the official pre-clipped
                                                     geometry already in place.

Why one-stream-per-feature?
  The CDFW dataset has each REACH as its own pre-clipped feature with its own
  regulation.  No need to group by parent waterbody — every feature already
  points at exactly the right segment, and the resulting sidebar is fully
  searchable / filterable.

Filter:
  Only features whose linked regulations have a Fin-Fish tag and that fall
  under §7.40 / §7.50 / §5.* / §2.* / §3.* (i.e. anything fishable).  Hold-
  over reporting-card sections (§8.*) are excluded by default; pass
  --include-all to keep everything.
"""

import calendar
import json
import os
import re
import sys
import time
from datetime import date, timedelta

CDFW_FEATS_PATH = os.path.join("data", "cdfw_features.geojson")
CDFW_REGS_PATH  = os.path.join("data", "cdfw_regulations.json")
REGS_OUT        = os.path.join("data", "regulations.json")
GEO_OUT         = os.path.join("data", "streams.geojson")


# ── Date phrase parser ───────────────────────────────────────────────────────
# Parses CDFW open_season text into year-agnostic MM-DD ranges so the JS map
# can colour streams by what's open on any given day of the year.

_MONTHS = {
    "jan":1, "january":1,
    "feb":2, "february":2,
    "mar":3, "march":3,
    "apr":4, "april":4,
    "may":5,
    "jun":6, "june":6,
    "jul":7, "july":7,
    "aug":8, "august":8,
    "sep":9, "sept":9, "september":9,
    "oct":10, "october":10,
    "nov":11, "november":11,
    "dec":12, "december":12,
}
_WEEKDAYS = {  # 0 = Monday … 6 = Sunday
    "mon":0, "monday":0,
    "tue":1, "tuesday":1, "tues":1,
    "wed":2, "wednesday":2,
    "thu":3, "thursday":3, "thur":3, "thurs":3,
    "fri":4, "friday":4,
    "sat":5, "saturday":5,
    "sun":6, "sunday":6,
}
_ORDINALS = {
    "first":1, "second":2, "third":3, "fourth":4, "fifth":5, "last":-1,
}

# Use a non-leap year as the reference (Feb 29 won't appear in regs)
_REF_YEAR = 2025


def _month_num(s):
    s = s.strip().lower().rstrip(".")
    if s in _MONTHS:
        return _MONTHS[s]
    return _MONTHS.get(s[:3])


def _weekday_num(s):
    s = s.strip().lower().rstrip(".")
    if s in _WEEKDAYS:
        return _WEEKDAYS[s]
    return _WEEKDAYS.get(s[:3])


def _nth_weekday_in_month(year, month, weekday, n):
    """n=1..5 for first/second/.../fifth, -1 for last."""
    if n > 0:
        first = date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        day = 1 + offset + (n - 1) * 7
        try:
            return date(year, month, day)
        except ValueError:
            return None
    last_day = calendar.monthrange(year, month)[1]
    last = date(year, month, last_day)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


def _last_day_of_month(year, month):
    return date(year, month, calendar.monthrange(year, month)[1])


def _memorial_day(year):
    return _nth_weekday_in_month(year, 5, 0, -1)  # last Monday of May


def parse_date_phrase(phrase, year=_REF_YEAR):
    """
    Convert a phrase like 'Jan. 1', 'Last Sat. in Apr.', 'Sat. preceding
    Memorial Day', 'the last day in Feb.' into a `date` object.  Returns None
    if the phrase doesn't match any known pattern.
    """
    if not phrase:
        return None
    p = re.sub(r"\s+", " ", phrase).strip().rstrip(".,").strip()
    if not p:
        return None

    pl = p.lower()

    # "the last day [in|of] <month>" / "last day in feb"
    m = re.match(r"(?:the\s+)?last\s+day\s+(?:in|of)\s+([a-z]+)\.?$", pl)
    if m:
        mn = _month_num(m.group(1))
        if mn:
            return _last_day_of_month(year, mn)

    # Holidays
    if pl == "memorial day":
        return _memorial_day(year)
    if pl == "labor day":
        return _nth_weekday_in_month(year, 9, 0, 1)  # first Mon Sep

    # "<weekday>. preceding <ordinal> <weekday>. in <month>"
    m = re.match(
        r"(?:the\s+)?([a-z]+)\.?\s+preceding\s+(?:the\s+)?"
        r"(first|second|third|fourth|fifth|last)\s+([a-z]+)\.?\s+in\s+([a-z]+)\.?$",
        pl,
    )
    if m:
        wd  = _weekday_num(m.group(1))
        ord_ = _ORDINALS.get(m.group(2))
        twd = _weekday_num(m.group(3))
        mn  = _month_num(m.group(4))
        if wd is not None and ord_ is not None and twd is not None and mn:
            target = _nth_weekday_in_month(year, mn, twd, ord_)
            if target is None:
                return None
            offset = (target.weekday() - wd) % 7
            if offset == 0:
                offset = 7
            return target - timedelta(days=offset)

    # "<weekday>. preceding memorial day"
    m = re.match(r"(?:the\s+)?([a-z]+)\.?\s+preceding\s+memorial\s+day$", pl)
    if m:
        wd = _weekday_num(m.group(1))
        if wd is not None:
            md = _memorial_day(year)
            offset = (md.weekday() - wd) % 7
            if offset == 0:
                offset = 7
            return md - timedelta(days=offset)

    # "<ordinal> <weekday>. in <month>"
    m = re.match(
        r"(?:the\s+)?(first|second|third|fourth|fifth|last)\s+"
        r"([a-z]+)\.?\s+in\s+([a-z]+)\.?$",
        pl,
    )
    if m:
        ord_ = _ORDINALS.get(m.group(1))
        wd   = _weekday_num(m.group(2))
        mn   = _month_num(m.group(3))
        if ord_ is not None and wd is not None and mn:
            return _nth_weekday_in_month(year, mn, wd, ord_)

    # "<month>. <day>"  e.g. "Jan. 1", "Mar 31", "Sept. 15"
    m = re.match(r"([a-z]+)\.?\s*(\d{1,2})$", pl)
    if m:
        mn = _month_num(m.group(1))
        if mn:
            try:
                return date(year, mn, int(m.group(2)))
            except ValueError:
                return None

    # "<day> <month>" rarely used; fall through.
    return None


# Stop tokens for the date-range portion of an open_season string.
# Anything beyond these is a method/equipment/note restriction, not a date.
_DATE_STOP_RE = re.compile(
    r"\s+(?:Only\s|Bait\s|Cutthroat\s|Fishing\s+hours|Minimum\s+size|"
    r"However,|At\s+all\s+times|for\s+hatchery|Closed\s+to\s+bass|"
    r"Closed\s+to\s+the\s+take|Note:|NOTE:|but\s+only|This\s+is)",
    re.I,
)
# Word-boundary "and" used to OR-connect two date ranges
_AND_SPLIT_RE = re.compile(r"\s+and\s+", re.I)
# Range connectors
_RANGE_RE = re.compile(r"^(.*?)\s+(?:through|thru|to)\s+(.*)$", re.I | re.DOTALL)


def _extract_date_portion(text):
    """Return the leading date-range portion of an open_season string."""
    if not text:
        return ""
    t = re.sub(r"\s+", " ", text).strip()
    m = _DATE_STOP_RE.search(t)
    if m:
        t = t[: m.start()]
    # Strip a trailing period.
    return t.strip().rstrip(".").strip()


def _parse_one_range(text, year):
    """Parse 'X through Y' into {'start':MM-DD,'end':MM-DD,...} or None."""
    text = text.strip().rstrip(",.").strip()
    if not text:
        return None
    m = _RANGE_RE.match(text)
    if not m:
        return None
    start = parse_date_phrase(m.group(1), year)
    end   = parse_date_phrase(m.group(2), year)
    if start is None or end is None:
        return None
    return {
        "start":     start.strftime("%m-%d"),
        "end":       end.strftime("%m-%d"),
        "all_year":  False,
        "closed":    False,
    }


def parse_open_season(text, year=_REF_YEAR):
    """
    Parse an open_season string into a structured form:
      {
        'all_year':         bool,   # rule applies every day of the year
        'closed_all_year':  bool,   # rule is "closed to all fishing all year"
        'date_ranges':      [ {start, end, closed}, ... ],
        'parseable':        bool,   # we recognised SOMETHING
      }
    All dates are MM-DD strings, year-agnostic.
    """
    out = {
        "all_year":        False,
        "closed_all_year": False,
        "date_ranges":     [],
        "parseable":       False,
    }
    if not text or len(text.strip()) < 2:
        return out

    t = re.sub(r"\s+", " ", text).strip()
    tl = t.lower()

    # Skip pure references / cross-section pointers
    if (tl.startswith("see ") or tl.startswith("covered under")
            or tl.startswith("section ")):
        return out

    # "Closed to all fishing [all] year" — fully closed
    if re.match(
        r"^closed\s+to\s+(?:all\s+fishing|the\s+take\s+of\s+\S+)"
        r"(?:\s+all\s+year|\s+year)?\s*\.?\s*$",
        tl,
    ):
        out["closed_all_year"] = True
        out["all_year"]        = True
        out["parseable"]       = True
        return out

    # "Closed to all fishing all year for safety purposes." etc.
    if re.match(r"^closed\s+to\s+all\s+fishing(\s|$)", tl):
        # If it has a "from X through Y" inside, treat as seasonal closure
        m = re.search(
            r"closed\s+to\s+all\s+fishing\s+from\s+(.+?)\s+through\s+(.+?)\s*\.?$",
            tl,
        )
        if m:
            s = parse_date_phrase(m.group(1), year)
            e = parse_date_phrase(m.group(2), year)
            if s and e:
                out["date_ranges"].append({
                    "start":  s.strftime("%m-%d"),
                    "end":    e.strftime("%m-%d"),
                    "closed": True,
                })
                out["parseable"] = True
                return out
        # Otherwise treat the whole regulation as closed all year
        out["closed_all_year"] = True
        out["all_year"]        = True
        out["parseable"]       = True
        return out

    # "All year" prefix — rule applies every day; a non-fishing rule means open
    if re.match(r"^all\s*year\b", tl):
        out["all_year"]  = True
        out["parseable"] = True
        return out

    # Otherwise look for one or more "X through Y" ranges
    date_text = _extract_date_portion(t)
    if not date_text:
        return out

    # Split on top-level " and " — many CDFW rules use:
    #   "Jan. 1 through Mar. 31 and fourth Sat. in May through Sep. 30"
    parts = _AND_SPLIT_RE.split(date_text)
    for part in parts:
        r = _parse_one_range(part, year)
        if r:
            out["date_ranges"].append(r)
            out["parseable"] = True

    return out


# Sections we consider "fishable freshwater" — keep their features.
DEFAULT_KEEP_SECTION_PREFIXES = (
    "2.",   # statewide methods, district seasons
    "3.",   # sport fish (general)
    "4.",   # ocean / anadromous (some inland-relevant)
    "5.",   # district-specific bag/limit
    "7.",   # special trout & salmon waters (7.40 and 7.50)
)


# ── Verbatim-text cleaner ────────────────────────────────────────────────────

# CDFW's `Verbatim` field is wrapped in template markers like
#   [start_row][row]LOCATION|OPEN_SEASON|BAG_LIMIT[row][end_row]
# with literal "_" placeholders for empty fields.  This strips the wrappers and
# splits the pipe-separated fields back out into structured data.

_VERBATIM_BLOCK_RE = re.compile(
    r"\[start_row\]\[row\](?P<body>.*?)\[row\]\[end_row\]",
    re.DOTALL,
)
_HEADER_RE = re.compile(r"\[start_(?:header|subHeader)\](.*?)\[end_(?:header|subHeader)\]",
                        re.DOTALL)


def _clean_field(s):
    """Strip stray markup tokens from a field."""
    if s is None:
        return ""
    s = s.replace("[start_row]", "").replace("[end_row]", "").replace("[row]", "")
    return s.strip()


def parse_verbatim(text):
    """
    Return a list of dicts: [{location, open_season, bag_limit, raw}, ...]

    CDFW format examples:
      [start_row][row]LOC|SEAS|BAG[row][end_row]
      [start_row][row]LOC|SEAS1|BAG1[row][row]_|SEAS2|BAG2[row][end_row]
      [start_header]Title[end_header][start_subHeader]Body[end_subHeader]

    Rules:
      • Rows are separated by [start_row]...[end_row] OR by inner [row][row]
      • Within a row, fields are pipe-separated
      • '_' means "empty/inherited" — for location, inherit from the prior row
      • Header/subHeader blocks are flattened into a single description row
    """
    if not text:
        return []
    rows = []

    # ── Header/subHeader blocks (used by §2.x rules) ──────────────────────────
    if "[start_header]" in text or "[start_subHeader]" in text:
        chunks = _HEADER_RE.findall(text)
        if chunks:
            rows.append({
                "location":    " — ".join(c.strip() for c in chunks),
                "open_season": "",
                "bag_limit":   "",
                "raw":         text.strip(),
            })

    # ── Standard [start_row]…[end_row] blocks ─────────────────────────────────
    last_loc = ""
    for m in _VERBATIM_BLOCK_RE.finditer(text):
        body = m.group("body")
        # A single block can hold MULTIPLE rows separated by [row][row]
        sub_rows = re.split(r"\[row\]\[row\]", body)
        for sub in sub_rows:
            fields = [_clean_field(f) for f in sub.split("|")]
            while len(fields) < 3:
                fields.append("")
            loc, season, bag = fields[0], fields[1], fields[2]
            if loc    == "_": loc    = ""
            if season == "_": season = ""
            if bag    == "_": bag    = ""
            # Empty location → inherit from previous row in same block
            if not loc:
                loc = last_loc
            else:
                last_loc = loc
            if not (loc or season or bag):
                continue
            rows.append({
                "location":    loc,
                "open_season": season,
                "bag_limit":   bag,
                "raw":         _clean_field(sub),
            })

    # ── Fallback: no markers but text exists ──────────────────────────────────
    if not rows and text.strip():
        cleaned = re.sub(r"\[/?(?:start_|end_)?(?:row|header|subHeader)\]", "", text).strip()
        if cleaned:
            fields = [f.strip() for f in cleaned.split("|")]
            while len(fields) < 3:
                fields.append("")
            loc, season, bag = fields[0], fields[1], fields[2]
            if loc    == "_": loc    = ""
            if season == "_": season = ""
            if bag    == "_": bag    = ""
            if loc or season or bag:
                rows.append({
                    "location": loc, "open_season": season,
                    "bag_limit": bag, "raw": cleaned,
                })
    return rows


# ── Segment construction ────────────────────────────────────────────────────

def _segment_status(seg):
    """Match the heuristic used in index.html's segStatus()."""
    text = (seg.get("open_season", "") + seg.get("bag_limit", "")).lower()
    if "closed to all fishing all year" in text:
        return "closed"
    if any(k in text for k in ("artificial", "barbless", "catch and release",
                                "size limit", "minimum size", "bag and possession")):
        return "restricted"
    if seg.get("open_season"):
        return "open"
    return "unknown"


def _stream_status(segments):
    if not segments:
        return "unknown"
    statuses = {_segment_status(s) for s in segments}
    statuses.discard("unknown")
    if not statuses:
        return "unknown"
    if len(statuses) == 1:
        return statuses.pop()
    return "mixed"


def _build_segments(reg_rows):
    """
    Convert a list of CDFW regulation rows into the existing 'segments' schema:
      { label, description, open_season, bag_limit, ... }
    Each parsed verbatim row becomes one segment.
    """
    segs = []
    for r in reg_rows:
        code  = r.get("Code", "") or ""
        title = r.get("Title", "") or ""
        tags  = r.get("Tags", "") or ""
        verb  = r.get("Verbatim", "") or ""

        rows = parse_verbatim(verb)
        if not rows:
            # Skip rows with no extractable verbatim content
            continue

        for row in rows:
            parsed = parse_open_season(row["open_season"])
            seg = {
                "label":           _label_from_code(code),
                "description":     row["location"] or _description_from_title(title),
                "open_season":     row["open_season"],
                "bag_limit":       row["bag_limit"],
                "code":            code,
                "tags":            tags,
                "title":           title,
                "all_year":        parsed["all_year"],
                "closed_all_year": parsed["closed_all_year"],
                "date_ranges":     parsed["date_ranges"],
                "date_parseable":  parsed["parseable"],
            }
            segs.append(seg)
    return segs


def _label_from_code(code):
    """Pull the trailing label like 'A' / '(b)(4)(A)' out of a CCR code."""
    if not code:
        return ""
    m = re.search(r"\(([A-Za-z0-9]+)\)\s*$", code)
    return m.group(1) if m else ""


def _description_from_title(title):
    """If a regulation has no location row, fall back to a snippet of its Title."""
    if not title:
        return ""
    return title[:120]


# ── Centroid helper ─────────────────────────────────────────────────────────

def _centroid(geom):
    if not geom:
        return None, None
    t = geom.get("type")
    pts = []
    if t == "Point":
        c = geom["coordinates"]
        return c[1], c[0]
    if t == "LineString":
        pts = geom["coordinates"]
    elif t == "MultiLineString":
        for line in geom["coordinates"]:
            pts.extend(line)
    elif t == "Polygon":
        if geom["coordinates"]:
            pts = geom["coordinates"][0]
    elif t == "MultiPolygon":
        if geom["coordinates"] and geom["coordinates"][0]:
            pts = geom["coordinates"][0][0]
    if not pts:
        return None, None
    mid = pts[len(pts) // 2]
    return mid[1], mid[0]


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    include_all = "--include-all" in sys.argv

    if not os.path.exists(CDFW_FEATS_PATH):
        sys.exit(f"Missing {CDFW_FEATS_PATH} — run `python fetch_cdfw.py` first.")

    print(f"Loading {CDFW_FEATS_PATH}…", flush=True)
    with open(CDFW_FEATS_PATH, encoding="utf-8") as f:
        cdfw = json.load(f)
    raw_feats = cdfw.get("features", [])
    print(f"  {len(raw_feats)} CDFW features", flush=True)

    streams      = []
    out_features = []
    skipped      = {"no_regs": 0, "no_geometry": 0, "wrong_section": 0,
                    "no_segments": 0}

    for raw in raw_feats:
        props = raw["properties"]
        geom  = raw.get("geometry")

        gid     = props.get("id") or ""
        name    = props.get("name") or "(unnamed)"
        section = props.get("section") or ""
        regs    = props.get("regulations") or []
        category = props.get("category") or ""
        counties = props.get("counties") or []

        # Filter: drop sections we don't care about (e.g. §8.* hold-over cards)
        if not include_all:
            if not section or not any(
                section.startswith(p) for p in DEFAULT_KEEP_SECTION_PREFIXES
            ):
                skipped["wrong_section"] += 1
                continue

        if not regs:
            skipped["no_regs"] += 1
            continue
        if geom is None:
            skipped["no_geometry"] += 1
            continue

        segments = _build_segments(regs)
        if not segments:
            skipped["no_segments"] += 1
            continue

        status = _stream_status(segments)
        county_str = ", ".join(counties)
        lat, lng = _centroid(geom)

        # Stable short id from first 8 chars of GUID
        sid = f"cdfw_{gid[:8]}" if gid else f"cdfw_{len(streams):04d}"

        # Carry the cleaned-up regulation/feature info into the stream record
        stream = {
            "id":         sid,
            "section":    section,
            "entry_num":  len(streams) + 1,
            "name":       name,
            "county":     county_str,
            "category":   category,
            "geom_kind":  props.get("geom_kind"),
            "segments":   segments,
            "lat":        lat,
            "lng":        lng,
            "status":     status,
            "global_id":  gid,
        }
        streams.append(stream)

        out_features.append({
            "type":     "Feature",
            "geometry": geom,
            "properties": {
                "id":           sid,
                "name":         name,
                "county":       county_str,
                "section":      section,
                "status":       status,
                "category":     category,
                "geom_kind":    props.get("geom_kind"),
                "segments":     segments,
                "lat":          lat,
                "lng":          lng,
                "source":       "cdfw",
                "global_id":    gid,
                "qualifier_text": None,   # not needed — geometry is already clipped
                "needs_review":   False,
                "clipped":        True,    # CDFW pre-clipped
            },
        })

    print(f"\nProduced {len(streams)} streams")
    print(f"Skipped:")
    print(f"  no regulations:    {skipped['no_regs']}")
    print(f"  no geometry:       {skipped['no_geometry']}")
    print(f"  off-section:       {skipped['wrong_section']}")
    print(f"  no parsed segs:    {skipped['no_segments']}")

    # Section breakdown
    by_section = {}
    for s in streams:
        by_section[s["section"]] = by_section.get(s["section"], 0) + 1
    print(f"\nBy section:")
    for sec in sorted(by_section):
        print(f"  §{sec:<6s} {by_section[sec]}")

    # Status breakdown
    by_status = {}
    for s in streams:
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1
    print(f"\nBy status:")
    for st in sorted(by_status):
        print(f"  {st:<10s} {by_status[st]}")

    # Geometry-type breakdown
    by_type = {}
    for f in out_features:
        t = f["geometry"]["type"]
        by_type[t] = by_type.get(t, 0) + 1
    print(f"\nGeometry types:")
    for t in sorted(by_type):
        print(f"  {t:<18s} {by_type[t]}")

    # ── Save outputs ──────────────────────────────────────────────────────────
    regs_out = {
        "source":   "California Department of Fish and Wildlife — RFRService Feature Server",
        "url":      "https://services2.arcgis.com/Uq9r85Potqm3MfRV/arcgis/rest/services/RFRService/FeatureServer",
        "updated":  time.strftime("%Y-%m-%d", time.gmtime()),
        "sections": sorted(by_section.keys()),
        "streams":  streams,
    }
    with open(REGS_OUT, "w", encoding="utf-8") as f:
        json.dump(regs_out, f, ensure_ascii=False, indent=2)
    print(f"\nSaved {REGS_OUT}  ({len(streams)} streams)")

    geo_out = {
        "type":     "FeatureCollection",
        "metadata": {
            "source":  "CDFW RFRService Feature Server",
            "updated": time.strftime("%Y-%m-%d", time.gmtime()),
        },
        "features": out_features,
    }
    with open(GEO_OUT, "w", encoding="utf-8") as f:
        json.dump(geo_out, f, ensure_ascii=False)
    print(f"Saved {GEO_OUT}      ({len(out_features)} features)")


if __name__ == "__main__":
    main()
