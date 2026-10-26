"""
seasons.py — turn regulation season / bag text into a day-by-day status for a year.

Each regulation row has an "open season" phrase (e.g. "Last Sat. in Apr. through
Nov. 15. Only artificial lures with barbless hooks may be used.") and a bag-limit
phrase (e.g. "0 trout").  We convert a list of rows into a string with one
character per day of the year:

    o  open, ordinary rules (bag > 0, no gear restriction)
    r  open, but restricted (gear restriction and/or zero bag / catch-and-release)
    c  closed to fishing
    u  could not be determined from the text

Waters listed in §7.40(b)/§7.50(b) and §7.00 are closed except during the
open seasons shown, so any day not covered by a parsed season is "c".
"""

import calendar
import re
from datetime import date, timedelta

YEAR = 2026

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})
_MONTHS["sept"] = 9
_WD = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3,
       "fri": 4, "sat": 5, "sun": 6, "monday": 0, "tuesday": 1, "wednesday": 2,
       "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
_ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "last": -1}


def _mon(s):
    s = s.lower().rstrip(".")
    return _MONTHS.get(s) or _MONTHS.get(s[:3])


def _wd(s):
    s = s.lower().rstrip(".")
    return _WD.get(s, _WD.get(s[:3]))


def nth_weekday(y, m, wd, n):
    if n > 0:
        d = date(y, m, 1)
        d += timedelta(days=(wd - d.weekday()) % 7 + 7 * (n - 1))
        return d if d.month == m else None
    d = date(y, m, calendar.monthrange(y, m)[1])
    return d - timedelta(days=(d.weekday() - wd) % 7)


def memorial_day(y):
    return nth_weekday(y, 5, 0, -1)


def parse_day(p, y=YEAR):
    """'Jan. 1', 'last Sat. in Apr.', 'Sat. preceding Memorial Day',
    'Fri. preceding the last Sat. in Apr.', 'the last day of Feb.' → date"""
    p = re.sub(r"\s+", " ", p or "").strip().strip(".,;: ").lower()
    p = re.sub(r"^(?:from|the|on)\s+", "", p)
    p = p.replace("saturday", "sat").replace("friday", "fri").replace("sunday", "sun")
    if not p:
        return None
    m = re.match(r"(?:the )?last day (?:in|of) ([a-z]+)\.?$", p)
    if m and _mon(m.group(1)):
        mn = _mon(m.group(1))
        return date(y, mn, calendar.monthrange(y, mn)[1])
    if p in ("memorial day",):
        return memorial_day(y)
    if p in ("labor day",):
        return nth_weekday(y, 9, 0, 1)
    m = re.match(r"([a-z]+)\.? (?:preceding|before) (?:the )?(first|second|third|fourth|fifth|last) ([a-z]+)\.? (?:in|of) ([a-z]+)\.?$", p)
    if m:
        wd, n, twd, mn = _wd(m.group(1)), _ORD[m.group(2)], _wd(m.group(3)), _mon(m.group(4))
        if None not in (wd, twd, mn):
            t = nth_weekday(y, mn, twd, n)
            off = (t.weekday() - wd) % 7 or 7
            return t - timedelta(days=off)
    m = re.match(r"([a-z]+)\.? (?:preceding|before) memorial day$", p)
    if m and _wd(m.group(1)) is not None:
        md = memorial_day(y)
        off = (md.weekday() - _wd(m.group(1))) % 7 or 7
        return md - timedelta(days=off)
    m = re.match(r"(first|second|third|fourth|fifth|last) ([a-z]+)\.? (?:in|of) ([a-z]+)\.?$", p)
    if m and _wd(m.group(2)) is not None and _mon(m.group(3)):
        return nth_weekday(y, _mon(m.group(3)), _wd(m.group(2)), _ORD[m.group(1)])
    m = re.match(r"([a-z]+)\.? ?(\d{1,2})(?:st|nd|rd|th)?$", p)
    if m and _mon(m.group(1)):
        try:
            return date(y, _mon(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


_RANGE = re.compile(
    r"((?:the\s+)?(?:last\s+day\s+(?:in|of)\s+[A-Za-z]+\.?|"
    r"(?:(?:Sat|Fri|Sun|Saturday|Friday|Sunday)\.?\s+(?:preceding|before)\s+(?:the\s+)?(?:(?:first|second|third|fourth|fifth|last)\s+[A-Za-z]+\.?\s+(?:in|of)\s+[A-Za-z]+\.?|Memorial\s+Day))|"
    r"(?:first|second|third|fourth|fifth|last)\s+[A-Za-z]+\.?\s+(?:in|of)\s+[A-Za-z]+\.?|"
    r"[A-Z][a-z]{2,8}\.?\s*\d{1,2}))"
    r"\s*(?:,\s*)?(?:through|thru|to|-|–)\s*"
    r"((?:the\s+)?(?:last\s+day\s+(?:in|of)\s+[A-Za-z]+\.?|"
    r"(?:(?:Sat|Fri|Sun|Saturday|Friday|Sunday)\.?\s+(?:preceding|before)\s+(?:the\s+)?(?:(?:first|second|third|fourth|fifth|last)\s+[A-Za-z]+\.?\s+(?:in|of)\s+[A-Za-z]+\.?|Memorial\s+Day))|"
    r"(?:first|second|third|fourth|fifth|last)\s+[A-Za-z]+\.?\s+(?:in|of)\s+[A-Za-z]+\.?|"
    r"[A-Z][a-z]{2,8}\.?\s*\d{1,2}))",
    re.I,
)

GEAR_RE = re.compile(r"artificial lures?|barbless|fly fishing|flies only|no bait|catch[- ]and[- ]release|"
                     r"only artificial|bait may not|no fishing from|only barbless", re.I)
CLOSED_ALL_RE = re.compile(r"^\s*closed\s+to\s+(?:all\s+)?fishing(?:\s+all\s+year)?\b", re.I)
SPECIES_ONLY_RE = re.compile(r"\bfor\s+(?:chinook|salmon|steelhead|hatchery steelhead|kokanee|"
                             r"striped bass|sturgeon|bass)\b", re.I)


def bag_is_zero(bag):
    b = (bag or "").lower()
    if not b.strip():
        return False
    if re.search(r"\b0\s+(?:trout|fish|hatchery|salmon|bass|chinook)|zero[- ]trout|\bno\s+trout\b|catch and release", b):
        # "0 trout" but also maybe "2 hatchery steelhead" → treat as restricted
        return True
    return False


def _days_in_range(s, e):
    """List of day indexes (0-based) from s to e inclusive, wrapping the year."""
    y0 = date(YEAR, 1, 1)
    n = 366 if calendar.isleap(YEAR) else 365
    a, b = (s - y0).days, (e - y0).days
    if a <= b:
        return list(range(a, b + 1))
    return list(range(a, n)) + list(range(0, b + 1))


def row_days(season, bag):
    """→ (dict day->state, parsed_ok, note)"""
    n = 366 if calendar.isleap(YEAR) else 365
    s = re.sub(r"\s+", " ", season or "").strip()
    if not s or s == "_":
        return {}, False, "no season text"
    sl = s.lower()
    restricted = bool(GEAR_RE.search(s)) or bag_is_zero(bag)
    st = "r" if restricted else "o"

    # Fully closed ("Closed to all fishing all year", "All anadromous portions … Closed to all fishing all year")
    if (CLOSED_ALL_RE.match(s) or re.search(r"closed to (?:all )?fishing all year", sl)) \
            and not re.search(r"\bfrom\b.*\bthrough\b", sl) and "except" not in sl:
        return {d: "c" for d in range(n)}, True, ""
    m = re.match(r"closed to (?:all )?fishing from (.+?) through (.+?)(?:\.|$)", sl)
    if m:
        a, b = parse_day(m.group(1)), parse_day(m.group(2))
        if a and b:
            out = {d: st for d in range(n)}
            for d in _days_in_range(a, b):
                out[d] = "c"
            return out, True, ""
    if re.match(r"^all\s*year", sl):
        return {d: st for d in range(n)}, True, ""
    out = {}
    found = False
    for m in _RANGE.finditer(s):
        a, b = parse_day(m.group(1)), parse_day(m.group(2))
        if a and b:
            found = True
            # Text right after the range: "... for Chinook Salmon" → not a general season
            tail = s[m.end(): m.end() + 40]
            if re.match(r"\s*for\s+(chinook|salmon)", tail, re.I):
                continue
            days = _days_in_range(a, b)
            # "but only on Sat., Sun., Wed., legal holidays" → open on those weekdays only
            only = re.search(r"but only on ((?:(?:Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri|Sat|Sun)[a-z]*\.?,?\s*(?:and\s+)?)+)", s[m.end(): m.end() + 120])
            allowed = None
            if only:
                allowed = {_wd(w) for w in re.findall(r"[A-Za-z]+", only.group(1)) if _wd(w) is not None}
            for d in days:
                if allowed is not None and (date(YEAR, 1, 1) + timedelta(days=d)).weekday() not in allowed:
                    out.setdefault(d, "c")
                    continue
                out[d] = st
    if found:
        return out, True, ""
    return {}, False, "unparsed season: " + s[:80]


def rows_to_days(rows, default="c"):
    """Combine regulation rows into a per-day status string.
    Days not covered by any open season use `default` ('c' for listed waters)."""
    n = 366 if calendar.isleap(YEAR) else 365
    days = [None] * n
    ok_any, notes = False, []
    rank = {"o": 3, "r": 2, "c": 1}
    for r in rows:
        dd, ok, note = row_days(r.get("season"), r.get("bag"))
        if note:
            notes.append(note)
        if ok:
            ok_any = True
            for d, v in dd.items():
                cur = days[d]
                if cur is None or rank[v] > rank[cur]:
                    days[d] = v
    if not ok_any:
        return None, notes
    return "".join(v or default for v in days), notes


def statewide_stream_days():
    """§5.85(a)(2): last Sat. in Apr. – Nov. 15 open (5 trout, no gear rules);
    rest of year open with 0 trout and artificial barbless lures only."""
    rows = [{"season": "Last Sat. in Apr. through Nov. 15.", "bag": "5 trout"},
            {"season": "Nov. 16 through the Fri. preceding the last Sat. in Apr. Only artificial lures with barbless hooks may be used.",
             "bag": "0 trout"}]
    return rows_to_days(rows)[0]


if __name__ == "__main__":
    for t in ["Last Sat. in Apr. through Nov. 15.", "Sat. preceding Memorial Day through Sep. 30. Only artificial lures with barbless hooks may be used.",
              "Nov. 16 through the Fri. preceding the last Sat. in Apr.", "Fourth Sat. in May through Mar. 31.",
              "Dec. 1 through Mar. 7, but only on Sat., Sun., Wed., legal holidays and opening and closing days. Only barbless hooks may be used.",
              "Closed to all fishing all year.", "All year.", "Jan. 1 through Jul. 15. Only barbless hooks may be used."]:
        d, ok, note = row_days(t, "2 trout")
        s = "".join(d.get(i, ".") for i in range(365))
        print(ok, t[:50], "|", s[::7])
    print(parse_day("Fri. preceding the last Sat. in Apr."), parse_day("last Sat. in Apr."), parse_day("Sat. preceding Memorial Day"))
