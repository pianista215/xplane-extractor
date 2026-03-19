#!/usr/bin/env python3
"""
Generate SQL INSERT statements for navigation data from X-Plane nav data files.

Parses:
  - earth_nav.dat : VOR, NDB, ILS-LOC (with glideslope merged), LOC, DME
  - earth_fix.dat : enroute waypoints/fixes (ENRT usage only)
  - earth_awy.dat : airway segments

Generates idempotent SQL for nav_point, navaid, and airway_segment tables.
No database connection required.

Usage:
    uv run python src/generate_navdata_sql.py > navdata.sql
    uv run python src/generate_navdata_sql.py --navaids-only > navaids.sql
    uv run python src/generate_navdata_sql.py --fixes-only   > fixes.sql
    uv run python src/generate_navdata_sql.py --airways-only > airways.sql
"""

import argparse
import sys
from pathlib import Path


# Row codes parsed from earth_nav.dat
# Row 6 (GS) is parsed but not emitted as a nav_point — its glideslope angle
# is merged into the matching ILS-LOC navaid record.
# Rows 7/8/9 (OM/MM/IM markers) are skipped entirely.
NAV_ROW_TYPES = {
    2:  'NDB',
    3:  'VOR',
    4:  'ILS-LOC',
    5:  'LOC',
    6:  'GS',    # parsed for merging only, not emitted
    13: 'DME',
}

# Terminal aid row codes: field layout has airport_icao + runway_designator
TERMINAL_ROW_CODES = {4, 5, 6}

# Airway endpoint type codes -> nav_point point_type
AWY_TYPE_TO_POINT_TYPE = {
    2:  'NDB',
    3:  'VOR',
    11: 'FIX',
}


def esc(s: str) -> str:
    """Escape single quotes for SQL string literals."""
    return s.replace("'", "''")


def format_frequency(row_code: int, raw_freq: int) -> str:
    """
    Format frequency as a human-readable string.

    NDB (row 2): raw value is already kHz  -> e.g. "362"
    VOR/ILS/DME (rows 3,4,5,13): raw value is MHz*100 -> e.g. 11680 -> "116.80"
    GS (row 6): same encoding as ILS
    """
    if row_code == 2:
        return str(raw_freq)
    return f"{raw_freq / 100:.2f}"


def decode_nav_bearing(row_code: int, field6: float) -> tuple[float | None, float | None]:
    """
    Decode field 6 from earth_nav.dat into (true_bearing_deg, glideslope_deg).

    XP-NAV1200 encoding:
      Row 4/5 (LOC): field6 = floor(bearing) * 360 + bearing  ->  bearing = field6 % 360
      Row 6   (GS):  field6 = glideslope_hundredths * 1000 + bearing
    """
    if row_code in (4, 5):
        return round(field6 % 360, 3), None
    if row_code == 6:
        bearing = round(field6 % 1000, 3)
        glideslope = round(int(field6 // 1000) / 100, 2)
        return bearing, glideslope
    return None, None


def parse_nav_record(parts: list[str]) -> dict | None:
    """Parse one data line from earth_nav.dat. Returns None if not a wanted type."""
    if len(parts) < 11:
        return None
    try:
        row_code = int(parts[0])
        if row_code not in NAV_ROW_TYPES:
            return None

        lat        = float(parts[1])
        lon        = float(parts[2])
        raw_freq   = int(float(parts[4]))
        range_nm   = int(float(parts[5]))
        field6     = float(parts[6])
        identifier = parts[7]
        usage      = parts[8]   # 'ENRT' or airport ICAO
        # parts[9] = icao_region (not stored)

        is_terminal = row_code in TERMINAL_ROW_CODES
        if is_terminal:
            airport_icao      = usage
            runway_designator = parts[10] if len(parts) > 10 else ''
            name              = ' '.join(parts[11:]) if len(parts) > 11 else ''
        else:
            airport_icao      = None if usage == 'ENRT' else usage
            runway_designator = None
            name              = ' '.join(parts[10:]) if len(parts) > 10 else ''

        true_bearing, glideslope_deg = decode_nav_bearing(row_code, field6)
        frequency = format_frequency(row_code, raw_freq)

        return {
            'row_code':          row_code,
            'point_type':        NAV_ROW_TYPES[row_code],
            'lat':               lat,
            'lon':               lon,
            'frequency':         frequency,
            'range_nm':          range_nm,
            'identifier':        identifier,
            'name':              name,
            'airport_icao':      airport_icao,
            'runway_designator': runway_designator,
            'true_bearing':      true_bearing,
            'glideslope_deg':    glideslope_deg,
        }
    except (ValueError, IndexError):
        return None


def parse_earth_nav(nav_dat_path: Path) -> tuple[list[dict], list[dict]]:
    """
    Parse earth_nav.dat.

    Returns:
        nav_records : records to emit as nav_point + navaid (excludes GS)
        gs_records  : GS records for merging glideslope into ILS-LOC navaids
    """
    all_records = []
    with open(nav_dat_path, encoding='latin-1') as f:
        for line in f:
            parts = line.split()
            if not parts:
                continue
            try:
                int(parts[0])
            except ValueError:
                continue
            record = parse_nav_record(parts)
            if record:
                all_records.append(record)

    gs_records  = [r for r in all_records if r['row_code'] == 6]
    nav_records = [r for r in all_records if r['row_code'] != 6]

    # Build GS lookup: (identifier, airport_icao, runway_designator) -> glideslope_deg
    gs_lookup: dict[tuple, float] = {}
    for gs in gs_records:
        key = (gs['identifier'], gs['airport_icao'], gs['runway_designator'])
        if gs['glideslope_deg'] is not None:
            gs_lookup[key] = gs['glideslope_deg']

    # Merge glideslope_deg into matching ILS-LOC records
    merged = 0
    for rec in nav_records:
        if rec['row_code'] == 4:
            key = (rec['identifier'], rec['airport_icao'], rec['runway_designator'])
            if key in gs_lookup:
                rec['glideslope_deg'] = gs_lookup[key]
                merged += 1

    by_type = {}
    for r in nav_records:
        by_type[r['point_type']] = by_type.get(r['point_type'], 0) + 1

    print(f"Parsed {len(nav_records)} nav records from {nav_dat_path.name} "
          f"(+ {len(gs_records)} GS merged, {merged} ILS-LOC enriched):", file=sys.stderr)
    for pt, count in sorted(by_type.items()):
        print(f"  {pt}: {count}", file=sys.stderr)

    return nav_records, gs_records


def parse_earth_fix(fix_dat_path: Path) -> list[dict]:
    """Parse earth_fix.dat, returning only ENRT (enroute) fixes."""
    fixes = []
    with open(fix_dat_path, encoding='latin-1') as f:
        for line in f:
            parts = line.split()
            if len(parts) < 4:
                continue
            try:
                lat  = float(parts[0])
                lon  = float(parts[1])
                name = parts[2]
                if parts[3] == 'ENRT':
                    fixes.append({'lat': lat, 'lon': lon, 'name': name})
            except (ValueError, IndexError):
                continue

    print(f"Parsed {len(fixes)} ENRT fixes from {fix_dat_path.name}", file=sys.stderr)
    return fixes


def parse_earth_awy(awy_dat_path: Path) -> list[dict]:
    """Parse earth_awy.dat, returning list of airway segment dicts."""
    segments = []
    unknown_types = set()

    with open(awy_dat_path, encoding='latin-1') as f:
        for line in f:
            parts = line.split()
            if len(parts) < 10:
                continue
            try:
                from_name      = parts[0]
                from_type_code = int(parts[2])
                to_name        = parts[3]
                to_type_code   = int(parts[5])
                direction      = parts[6]   # N=bidirectional, F=forward only
                awy_type       = int(parts[7])  # 1=low, 2=high
                base_alt_ft    = int(parts[8]) * 100
                top_alt_ft     = int(parts[9]) * 100
                airway_names   = parts[10]

                from_pt = AWY_TYPE_TO_POINT_TYPE.get(from_type_code)
                to_pt   = AWY_TYPE_TO_POINT_TYPE.get(to_type_code)

                if from_pt is None:
                    unknown_types.add(from_type_code)
                    continue
                if to_pt is None:
                    unknown_types.add(to_type_code)
                    continue

                segments.append({
                    'from_name':       from_name,
                    'from_point_type': from_pt,
                    'to_name':         to_name,
                    'to_point_type':   to_pt,
                    'direction':       'BOTH' if direction == 'N' else 'FORWARD',
                    'airway_type':     'LOW' if awy_type == 1 else 'HIGH',
                    'base_alt_ft':     base_alt_ft,
                    'top_alt_ft':      top_alt_ft,
                    'airway_names':    airway_names,
                })
            except (ValueError, IndexError):
                continue

    print(f"Parsed {len(segments)} airway segments from {awy_dat_path.name}", file=sys.stderr)
    if unknown_types:
        print(f"  WARNING: skipped segments with unknown endpoint types: {unknown_types}", file=sys.stderr)
    return segments


# ---------------------------------------------------------------------------
# SQL generators
# ---------------------------------------------------------------------------

def sql_nav_point(lat: float, lon: float, identifier: str, name: str, point_type: str) -> str:
    ident = esc(identifier)
    nm    = esc(name)
    return (
        f"INSERT INTO `nav_point` (`latitude`, `longitude`, `identifier`, `name`, `point_type`)\n"
        f"SELECT {lat:.7f}, {lon:.7f}, '{ident}', '{nm}', '{point_type}'\n"
        f"WHERE NOT EXISTS (\n"
        f"  SELECT 1 FROM `nav_point`\n"
        f"  WHERE `identifier` = '{ident}' AND `point_type` = '{point_type}'\n"
        f"  AND `latitude` = {lat:.7f} AND `longitude` = {lon:.7f}\n"
        f");"
    )


def sql_navaid(rec: dict) -> str:
    lat   = rec['lat']
    lon   = rec['lon']
    ident = esc(rec['identifier'])
    pt    = rec['point_type']

    np_sq = (
        f"(SELECT `id` FROM `nav_point`"
        f" WHERE `identifier` = '{ident}' AND `point_type` = '{pt}'"
        f" AND `latitude` = {lat:.7f} AND `longitude` = {lon:.7f}"
        f" LIMIT 1)"
    )

    freq    = esc(rec['frequency'])
    rng     = str(rec['range_nm']) if rec['range_nm'] else 'NULL'
    bearing = f"{rec['true_bearing']:.3f}" if rec['true_bearing'] is not None else 'NULL'
    gs      = f"{rec['glideslope_deg']:.2f}" if rec['glideslope_deg'] is not None else 'NULL'

    if rec['airport_icao']:
        airport_val = f"(SELECT `icao_code` FROM `airport` WHERE `icao_code` = '{esc(rec['airport_icao'])}' LIMIT 1)"
    else:
        airport_val = 'NULL'

    runway_val = f"'{esc(rec['runway_designator'])}'" if rec['runway_designator'] else 'NULL'

    return (
        f"INSERT INTO `navaid` (`nav_point_id`, `frequency`, `range_nm`, `true_bearing_deg`, `glideslope_deg`, `airport_icao`, `runway_designator`)\n"
        f"SELECT {np_sq}, '{freq}', {rng}, {bearing}, {gs}, {airport_val}, {runway_val}\n"
        f"WHERE NOT EXISTS (\n"
        f"  SELECT 1 FROM `navaid` WHERE `nav_point_id` = {np_sq}\n"
        f");"
    )


def sql_fix_nav_point(fix: dict) -> str:
    return sql_nav_point(fix['lat'], fix['lon'], fix['name'], fix['name'], 'FIX')


def _nav_point_lookup(name: str, point_type: str) -> str:
    """
    SQL expression to resolve a nav_point id by identifier and point_type.
    Fallbacks handle data inconsistencies between earth_awy.dat and earth_nav.dat:
    - FIX (type 11): falls back to VOR/NDB/DME (~6% of awy endpoints are navaids)
    - VOR (type 3): falls back to NDB/DME (some stations listed as VOR are DME in nav.dat)
    - NDB (type 2): falls back to VOR/DME (same reason)
    """
    nm   = esc(name)
    base = f"(SELECT `id` FROM `nav_point` WHERE `identifier` = '{nm}' AND `point_type` = '{point_type}' LIMIT 1)"
    if point_type == 'FIX':
        fallback = f"(SELECT `id` FROM `nav_point` WHERE `identifier` = '{nm}' AND `point_type` IN ('VOR','NDB','DME') LIMIT 1)"
    elif point_type == 'VOR':
        fallback = f"(SELECT `id` FROM `nav_point` WHERE `identifier` = '{nm}' AND `point_type` IN ('NDB','DME') LIMIT 1)"
    elif point_type == 'NDB':
        fallback = f"(SELECT `id` FROM `nav_point` WHERE `identifier` = '{nm}' AND `point_type` IN ('VOR','DME') LIMIT 1)"
    else:
        return base
    return f"COALESCE({base}, {fallback})"


def sql_airway_segment(seg: dict) -> str:
    from_lookup = _nav_point_lookup(seg['from_name'], seg['from_point_type'])
    to_lookup   = _nav_point_lookup(seg['to_name'],   seg['to_point_type'])
    direction   = seg['direction']
    awy_type    = seg['airway_type']
    base        = seg['base_alt_ft']
    top         = seg['top_alt_ft']
    names       = esc(seg['airway_names'])

    return (
        f"INSERT INTO `airway_segment` (`from_nav_point_id`, `to_nav_point_id`, `direction`, `airway_type`, `base_alt_ft`, `top_alt_ft`, `airway_names`)\n"
        f"SELECT from_id, to_id, '{direction}', '{awy_type}', {base}, {top}, '{names}'\n"
        f"FROM (\n"
        f"  SELECT {from_lookup} AS from_id, {to_lookup} AS to_id\n"
        f") AS resolved\n"
        f"WHERE from_id IS NOT NULL AND to_id IS NOT NULL\n"
        f"AND NOT EXISTS (\n"
        f"  SELECT 1 FROM `airway_segment`\n"
        f"  WHERE `from_nav_point_id` = from_id AND `to_nav_point_id` = to_id\n"
        f"  AND `airway_names` = '{names}' AND `airway_type` = '{awy_type}'\n"
        f");"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate SQL for X-Plane navigation data (nav_point, navaid, airway_segment)"
    )
    parser.add_argument('--navaids-only', action='store_true', help='Only output navaid SQL')
    parser.add_argument('--fixes-only',   action='store_true', help='Only output fix SQL')
    parser.add_argument('--airways-only', action='store_true', help='Only output airway segment SQL')
    args = parser.parse_args()

    do_navaids = not args.fixes_only   and not args.airways_only
    do_fixes   = not args.navaids_only and not args.airways_only
    do_airways = not args.navaids_only and not args.fixes_only

    xplane_data = Path(__file__).parent.parent / 'xplane_data'

    required = []
    if do_navaids:
        required.append(xplane_data / 'earth_nav.dat')
    if do_fixes or do_airways:
        required.append(xplane_data / 'earth_fix.dat')
    if do_airways:
        required.append(xplane_data / 'earth_awy.dat')

    for path in required:
        if not path.exists():
            print(f"Error: {path} not found.", file=sys.stderr)
            print(f"Copy it from: X-Plane 12/Resources/default data/", file=sys.stderr)
            sys.exit(1)

    print("-- Generated by generate_navdata_sql.py")
    print("-- Source: X-Plane earth_nav.dat, earth_fix.dat, earth_awy.dat")
    print("-- Tables: nav_point, navaid, airway_segment")
    print()

    if do_navaids:
        nav_records, _ = parse_earth_nav(xplane_data / 'earth_nav.dat')
        print(f"-- ============================================================")
        print(f"-- NAV POINTS from earth_nav.dat ({len(nav_records)} records)")
        print(f"-- ============================================================")
        print()
        for rec in nav_records:
            print(sql_nav_point(rec['lat'], rec['lon'], rec['identifier'], rec['name'], rec['point_type']))
        print()

        print(f"-- ============================================================")
        print(f"-- NAVAIDS — frequency, bearing, ILS glideslope, airport")
        print(f"-- ============================================================")
        print()
        for rec in nav_records:
            print(sql_navaid(rec))
        print()

    if do_fixes:
        fixes = parse_earth_fix(xplane_data / 'earth_fix.dat')
        print(f"-- ============================================================")
        print(f"-- NAV POINTS from earth_fix.dat ({len(fixes)} ENRT fixes)")
        print(f"-- ============================================================")
        print()
        for fix in fixes:
            print(sql_fix_nav_point(fix))
        print()

    if do_airways:
        segments = parse_earth_awy(xplane_data / 'earth_awy.dat')
        print(f"-- ============================================================")
        print(f"-- AIRWAY SEGMENTS from earth_awy.dat ({len(segments)} segments)")
        print(f"-- ============================================================")
        print()
        for seg in segments:
            print(sql_airway_segment(seg))
        print()


if __name__ == '__main__':
    main()
