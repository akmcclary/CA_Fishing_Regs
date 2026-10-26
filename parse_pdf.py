"""
parse_pdf.py
Extracts stream-specific fishing regulations from the CA Freshwater Sport Fish PDF.
Outputs data/regulations.json
"""

import pdfplumber
import json
import re
import os

PDF_PATH = '2026 Freshwater Sport Fish_V3_011526.pdf'

# Page ranges (0-indexed). Confirmed from PDF inspection.
SEC_740_PAGES = range(39, 73)   # §7.40: Hatchery Trout/Steelhead/Salmon Waters
SEC_750_PAGES = range(73, 88)   # §7.50: Trout Waters


def clean_text(text):
    if not text:
        return ''
    return re.sub(r'\s+', ' ', str(text)).strip()


def parse_name_county(raw_text):
    """
    Extract stream name and county from an entry header like:
      '(4) American River (Sacramento Co.)'
      '(1) Alameda Creek and tributaries (Alameda and Santa Clara cos.)'
    Returns (name, county_string).
    """
    # Remove leading number with parens, e.g. "(4) "
    text = re.sub(r'^\(\d+\)\s*', '', raw_text).strip()

    # Find the last parenthesized phrase that contains "co" or "cos"
    county_match = None
    for m in re.finditer(r'\(([^)]+)\)', text):
        if re.search(r'\bcos?\.?\b', m.group(1), re.IGNORECASE):
            county_match = m

    if county_match:
        county_raw = county_match.group(1)
        # Remove trailing "co." / "cos." suffix
        county = re.sub(r'\s+cos?\.?\s*$', '', county_raw, flags=re.IGNORECASE).strip()
        name = text[:county_match.start()].strip().rstrip('.,')
        return name, county

    # No county found – return whole text as name
    return text.rstrip('.,'), ''


def determine_status(segments):
    """Derive a simple overall status string from the segment list."""
    if not segments:
        return 'unknown'

    closed = 0
    restricted = 0
    open_count = 0

    restriction_keywords = [
        'artificial lures', 'barbless', 'fly fishing', 'no bait',
        'single hook', 'catch and release'
    ]

    for seg in segments:
        season = clean_text(seg.get('open_season', '')).lower()
        bag    = clean_text(seg.get('bag_limit', '')).lower()
        combined = season + ' ' + bag

        if 'closed to all fishing all year' in combined:
            closed += 1
        elif 'closed' in combined and 'all year' in combined:
            closed += 1
        else:
            if any(k in combined for k in restriction_keywords):
                restricted += 1
            elif season:
                open_count += 1

    total = closed + restricted + open_count
    if total == 0:
        return 'unknown'
    if closed == total:
        return 'closed'
    if open_count == total:
        return 'open'
    if restricted > 0 and closed == 0:
        return 'restricted'
    return 'mixed'


def parse_section(pdf, page_indices, section_name):
    """
    Extract stream regulation entries from a set of PDF pages.
    Returns a list of stream dicts.
    """
    streams = []
    current_stream = None

    HEADER_PATTERNS = [
        'Body of Water', 'Open Season', 'Daily Bag', 'Possession Limit',
        'ALPHABETICAL LIST', 'HATCHERY TROUT', 'TROUT WATERS', '§7.40', '§7.50',
        'FRESHWATER SPORT FISHING', 'Updated 01/01/2026',
        '* Hatchery trout or steelhead',
        'Hatchery trout or steelhead in anadromous',
        '(T14 CCR'
    ]

    for page_idx in page_indices:
        page = pdf.pages[page_idx]
        tables = page.extract_tables()

        for table in tables:
            for row in table:
                if not row:
                    continue

                col0 = clean_text(row[0]) if row[0] else ''
                col1 = clean_text(row[1]) if len(row) > 1 and row[1] else ''
                col2 = clean_text(row[2]) if len(row) > 2 and row[2] else ''

                # Skip header / boilerplate rows
                if any(col0.startswith(h) or col1.startswith(h) for h in HEADER_PATTERNS):
                    continue
                if col0.startswith('*') or col1.startswith('*'):
                    continue
                if not col0 and not col1 and not col2:
                    continue

                # ── Main numbered water body entry, e.g. "(4) American River (Sacramento Co.)"
                main_match = re.match(r'^\((\d+)\)\s+(.+)', col0, re.DOTALL)

                # ── Sub-section: lettered, e.g. "(A) From Nimbus Dam..."
                sub_match = re.match(r'^\([A-Za-z]\)\s+', col0)

                # ── Sub-sub-section: numbered, e.g. "1. Arroyo Del Valle..."
                subsub_match = re.match(r'^\d+\.\s+', col0)

                if main_match:
                    entry_num = int(main_match.group(1))
                    name, county = parse_name_county(col0)

                    # Skip "Repealed" entries
                    if 'repealed' in name.lower():
                        current_stream = None
                        continue

                    current_stream = {
                        'id': f'{section_name}_{entry_num}',
                        'section': section_name,
                        'entry_num': entry_num,
                        'name': name,
                        'county': county,
                        'segments': [],
                        'lat': None,
                        'lng': None,
                    }
                    streams.append(current_stream)

                    # If the row also has regulation content it's a single-segment entry
                    if col1 or col2:
                        current_stream['segments'].append({
                            'description': '',
                            'open_season': col1,
                            'bag_limit': col2,
                        })

                elif (sub_match or subsub_match) and current_stream is not None:
                    current_stream['segments'].append({
                        'description': col0,
                        'open_season': col1,
                        'bag_limit': col2,
                    })

                elif not col0 and (col1 or col2) and current_stream is not None:
                    # Continuation row: additional season periods for the last segment
                    if current_stream['segments']:
                        last = current_stream['segments'][-1]
                        if col1:
                            last['open_season'] = (last['open_season'] + ' | ' + col1).strip(' |')
                        if col2:
                            last['bag_limit'] = (last['bag_limit'] + ' | ' + col2).strip(' |')

                elif col0 and current_stream is not None and (col1 or col2):
                    # Unlabeled sub-section with regulation content
                    current_stream['segments'].append({
                        'description': col0,
                        'open_season': col1,
                        'bag_limit': col2,
                    })

    # Derive status
    for stream in streams:
        stream['status'] = determine_status(stream['segments'])

    return streams


def main():
    os.makedirs('data', exist_ok=True)

    all_streams = []

    with pdfplumber.open(PDF_PATH) as pdf:
        print('Parsing §7.40 (Hatchery Trout / Steelhead / Salmon waters)...')
        s740 = parse_section(pdf, SEC_740_PAGES, '7.40')
        print(f'  Found {len(s740)} entries')
        all_streams.extend(s740)

        print('Parsing §7.50 (Trout waters)...')
        s750 = parse_section(pdf, SEC_750_PAGES, '7.50')
        print(f'  Found {len(s750)} entries')
        all_streams.extend(s750)

    output = {
        'source': '2026 California Freshwater Sport Fishing Regulations',
        'updated': 'January 1, 2026',
        'sections': {
            '7.40': 'Hatchery Trout, Hatchery Steelhead, and Salmon Waters',
            '7.50': 'Trout Waters with Special Regulations',
        },
        'streams': all_streams,
    }

    out_path = os.path.join('data', 'regulations.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f'\nTotal entries: {len(all_streams)}')
    print(f'Saved to {out_path}')

    from collections import Counter
    statuses = Counter(s['status'] for s in all_streams)
    print('\nStatus summary:')
    for status, count in statuses.most_common():
        print(f'  {status}: {count}')

    # Print a few sample stream names for sanity check
    print('\nSample entries:')
    for s in all_streams[:5]:
        print(f"  [{s['section']}] {s['name']} ({s['county']}) → {s['status']}")


if __name__ == '__main__':
    main()
