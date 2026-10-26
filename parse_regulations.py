"""
parse_regulations.py
Parses the CA 2026 freshwater fishing regulations text file into structured JSON.
Handles multi-segment entries like "(A) From Nimbus Dam to... (B) From... to..."

Output: data/regulations.json
"""

import re
import json
import os

TEXT_PATH = '2026_Freshwater_Sport_Fish_V3_011526.pdf'

# ── Section boundaries (line numbers, 1-indexed) ──────────────────────────────
# §7.40 starts at the first real entry after the header
SEC_740_START = 2863   # "(1) Alameda Creek..."
SEC_740_END   = 5590

SEC_750_START = 5595   # "(1) Alambique Creek..."
SEC_750_END   = 7400


# ── Helpers ───────────────────────────────────────────────────────────────────

def clean(text):
    """Normalize whitespace."""
    return re.sub(r'\s+', ' ', text or '').strip()


def parse_name_county(raw):
    """
    From '(4) American River (Sacramento Co.)' extract:
      name   = 'American River'
      county = 'Sacramento'
    Returns (name, county_str)
    """
    # strip leading entry number
    text = re.sub(r'^\(\d+\)\s*', '', raw).strip()

    # strip footnote-style notes like "Also see Section 8.00..."
    text = re.sub(r'\.\s*Also see Section.*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\.\s*Note:.*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\.\s*See.*', '', text, flags=re.IGNORECASE)

    # find last (... co...) group
    county = ''
    name = text
    for m in re.finditer(r'\(([^)]+)\)', text):
        g = m.group(1)
        if re.search(r'\bcos?\.?\b', g, re.IGNORECASE):
            county_raw = re.sub(r'\s*cos?\.?\s*$', '', g, flags=re.IGNORECASE).strip()
            county = county_raw
            name = text[:m.start()].strip().rstrip('.,; ')
    return name, county


def determine_status(segments):
    """Derive overall status from list of segment dicts."""
    if not segments:
        return 'unknown'

    restriction_keywords = [
        'artificial lures', 'barbless', 'fly fishing', 'no bait',
        'single hook', 'catch and release', 'artificial flies',
        'minimum size', 'size limit'
    ]

    closed = open_ = restricted = 0
    for seg in segments:
        combined = (clean(seg.get('open_season', '')) + ' ' + clean(seg.get('bag_limit', ''))).lower()
        if 'closed to all fishing all year' in combined or ('closed' in combined and 'all year' in combined):
            closed += 1
        elif any(k in combined for k in restriction_keywords):
            restricted += 1
        elif clean(seg.get('open_season', '')):
            open_ += 1

    total = closed + open_ + restricted
    if total == 0:
        return 'unknown'
    if closed == total:
        return 'closed'
    if open_ == total:
        return 'open'
    if restricted > 0 and closed == 0:
        return 'restricted'
    return 'mixed'


def extract_segment_bounds(description):
    """
    Try to extract upstream/downstream landmark text from segment descriptions.
    e.g. 'From Nimbus Dam to the USGS gauging station cable crossing...'
    Returns dict with 'from_landmark' and 'to_landmark' if found.
    """
    desc = clean(description)
    result = {}

    # Pattern: "From X to Y" or "from X downstream to Y"
    m = re.search(
        r'[Ff]rom\s+(.+?)\s+(?:downstream |upstream )?to\s+(.+?)(?:\.\s+(?:Only|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|All|Closed|Last|Fourth|Open)|\.?\s*$)',
        desc
    )
    if m:
        result['from_landmark'] = clean(m.group(1))
        result['to_landmark'] = clean(m.group(2))
        return result

    # Pattern: "above X"
    m = re.search(r'\babove\b\s+(.+?)(?:\.|,|$)', desc, re.IGNORECASE)
    if m:
        result['qualifier'] = 'above'
        result['landmark'] = clean(m.group(1))

    # Pattern: "below X"
    m2 = re.search(r'\bbelow\b\s+(.+?)(?:\.|,|$)', desc, re.IGNORECASE)
    if m2:
        result['qualifier'] = 'below'
        result['landmark'] = clean(m2.group(1))

    # Pattern: "upstream of X"
    m3 = re.search(r'\bupstream of\b\s+(.+?)(?:\.|,|$)', desc, re.IGNORECASE)
    if m3:
        result['qualifier'] = 'above'
        result['landmark'] = clean(m3.group(1))

    # Pattern: "downstream of X"
    m4 = re.search(r'\bdownstream of\b\s+(.+?)(?:\.|,|$)', desc, re.IGNORECASE)
    if m4:
        result['qualifier'] = 'below'
        result['landmark'] = clean(m4.group(1))

    return result


# ── Main parser ───────────────────────────────────────────────────────────────

# Patterns
MAIN_ENTRY  = re.compile(r'^\((\d+)\)\s+(.+)')            # (4) American River...
SUB_LETTER  = re.compile(r'^\(([A-Za-z])\)\s+(.+)')       # (A) From Nimbus...
SUB_NUMBER  = re.compile(r'^(\d+)\.\s+(.+)')              # 1. Arroyo Del Valle...
SKIP_LINES  = re.compile(
    r'^(2026 FRESHWATER|Updated 01/01|ALPHABETICAL LIST|Body of Water|'
    r'Open Season|Daily Bag|Possession Limit|\* Hatchery|Hatchery trout or|'
    r'\(T14 CCR|\(a\) General|\(b\) Alphabetical)',
    re.IGNORECASE
)
NOTE_LINE = re.compile(r'^Note:', re.IGNORECASE)


def parse_section(lines, section_id):
    """
    Parse a slice of lines into a list of stream regulation entries.
    Each entry has: id, section, entry_num, name, county, segments, status
    Each segment has: label, description, open_season, bag_limit, bounds
    """
    streams = []
    current = None
    current_seg = None
    in_open_season = False   # reading open_season continuation
    in_bag = False            # reading bag continuation

    def flush_seg():
        nonlocal current_seg
        if current_seg and current:
            # Extract bounds from description
            current_seg['bounds'] = extract_segment_bounds(current_seg.get('description', ''))
            current['segments'].append(current_seg)
        current_seg = None

    i = 0
    while i < len(lines):
        raw = lines[i].rstrip('\r\n')
        line = clean(raw)
        i += 1

        if not line or SKIP_LINES.match(line) or NOTE_LINE.match(line):
            continue

        # ── Main numbered entry ───────────────────────────────────────────────
        m = MAIN_ENTRY.match(line)
        if m:
            flush_seg()
            if current:
                current['status'] = determine_status(current['segments'])
            entry_num = int(m.group(1))
            rest = clean(m.group(2))

            if 'repealed' in rest.lower():
                current = None
                continue

            name, county = parse_name_county(f'({entry_num}) ' + rest)

            current = {
                'id': f'{section_id}_{entry_num}',
                'section': section_id,
                'entry_num': entry_num,
                'name': name,
                'county': county,
                'segments': [],
                'status': 'unknown',
            }
            streams.append(current)

            # If no sub-entries follow, the rest is description for a root segment
            current_seg = {
                'label': '',
                'description': rest,  # full header text
                'open_season': '',
                'bag_limit': '',
            }
            in_open_season = False
            in_bag = False
            continue

        # ── Lettered sub-entry (A), (B)... ────────────────────────────────────
        m = SUB_LETTER.match(line)
        if m and current:
            flush_seg()
            label = m.group(1)
            desc  = clean(m.group(2))
            current_seg = {
                'label': label,
                'description': desc,
                'open_season': '',
                'bag_limit': '',
            }
            in_open_season = False
            in_bag = False
            continue

        # ── Numbered sub-sub-entry 1., 2. ─────────────────────────────────────
        m = SUB_NUMBER.match(line)
        if m and current:
            flush_seg()
            label = m.group(1)
            desc  = clean(m.group(2))
            current_seg = {
                'label': label,
                'description': desc,
                'open_season': '',
                'bag_limit': '',
            }
            in_open_season = False
            in_bag = False
            continue

        # ── Regulation content lines ──────────────────────────────────────────
        # These follow a segment header. Heuristic: if line looks like a season
        # or bag limit, attribute it to the current segment.
        if current_seg is None and current:
            # Root-level segment with no sub-label
            current_seg = {
                'label': '',
                'description': '',
                'open_season': '',
                'bag_limit': '',
            }

        if current_seg is not None:
            # Classify line as open_season or bag_limit content
            season_keywords = [
                'jan.', 'feb.', 'mar.', 'apr.', 'may', 'jun.',
                'jul.', 'aug.', 'sep.', 'oct.', 'nov.', 'dec.',
                'all year', 'closed to all', 'sat. preceding', 'last sat.',
                'fourth sat.', 'only artificial', 'only barbless',
                'only flies', 'only fly', 'open all year',
            ]
            bag_keywords = [
                'trout.', 'steelhead.', 'steelhead*', 'salmon.', 'bass.',
                'possession.', 'bag limit', 'catch and release',
                'hatchery trout', 'hatchery steelhead',
                ' trout\n', 'fish daily', 'daily bag',
                '0 trout', '2 trout', '5 trout', '1 trout',
            ]

            ll = line.lower()
            looks_season = any(k in ll for k in season_keywords)
            looks_bag    = any(k in ll for k in bag_keywords) and not looks_season

            # If we haven't started season/bag yet and this doesn't look like reg content,
            # treat as description continuation (handles multi-line "From X to Y" text)
            if not current_seg['open_season'] and not current_seg['bag_limit'] and not looks_season and not looks_bag:
                current_seg['description'] = (current_seg['description'] + ' ' + line).strip()
            elif looks_season:
                if current_seg['open_season']:
                    current_seg['open_season'] += ' | ' + line
                else:
                    current_seg['open_season'] = line
            elif looks_bag:
                if current_seg['bag_limit']:
                    current_seg['bag_limit'] += ' | ' + line
                else:
                    current_seg['bag_limit'] = line
            else:
                # Continuation of bag limit or season
                if current_seg['bag_limit']:
                    current_seg['bag_limit'] += ' ' + line
                elif current_seg['open_season']:
                    current_seg['open_season'] += ' ' + line

    # Flush last
    flush_seg()
    if current:
        current['status'] = determine_status(current['segments'])

    return streams


def load_lines(path, start, end):
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        all_lines = f.readlines()
    return all_lines[start-1:end-1]  # 1-indexed to 0-indexed


def main():
    os.makedirs('data', exist_ok=True)

    lines_740 = load_lines(TEXT_PATH, SEC_740_START, SEC_740_END)
    lines_750 = load_lines(TEXT_PATH, SEC_750_START, SEC_750_END)

    print('Parsing §7.40...')
    s740 = parse_section(lines_740, '7.40')
    print(f'  {len(s740)} entries')

    print('Parsing §7.50...')
    s750 = parse_section(lines_750, '7.50')
    print(f'  {len(s750)} entries')

    all_streams = s740 + s750

    # Stats
    from collections import Counter
    statuses = Counter(s['status'] for s in all_streams)
    seg_counts = Counter(len(s['segments']) for s in all_streams)

    print(f'\nTotal: {len(all_streams)} entries')
    print('Status:', dict(statuses))
    print('Segment counts:', dict(seg_counts))

    output = {
        'source': '2026 California Freshwater Sport Fishing Regulations',
        'updated': 'January 1, 2026',
        'sections': {
            '7.40': 'Hatchery Trout, Hatchery Steelhead, and Salmon Waters',
            '7.50': 'Trout Waters with Special Regulations',
        },
        'streams': all_streams,
    }

    out_path = 'data/regulations.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f'\nSaved to {out_path}')

    # Sample output
    print('\nSample entries:')
    for s in all_streams[:5]:
        print(f"  [{s['section']}] {s['name']} ({s['county']}) → {s['status']}, {len(s['segments'])} segments")
        for seg in s['segments'][:2]:
            print(f"    ({seg['label']}) {seg['description'][:60]}")
            if seg.get('bounds'):
                print(f"         bounds: {seg['bounds']}")


if __name__ == '__main__':
    main()
