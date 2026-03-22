#!/usr/bin/env python3
"""
Generate SQL UPDATE statements for max_glideslope_deg in runway_end table.

Sources (takes the maximum value across both):
- apt.dat: row code 21 (PAPI/VASI) - glide slope angle directly
- CIFP/*.dat: approach procedures - vertical angle field (ARINC 424)

Only generates UPDATEs for runway ends where the extracted value differs
from the DB default (3.00°). Uses airport_icao + designator in WHERE clause
so it works across any database instance.

Usage:
    uv run python src/generate_glideslope_sql.py > glideslope.sql
"""

import re
import sys
from math import atan2, cos, degrees, radians, sin
from pathlib import Path


DEFAULT_GS = 3.00
HEADING_TOLERANCE = 10  # degrees for PAPI heading matching
RAIL_TYPE = "6"         # Row 21 type 6 = RAIL, no vertical guidance


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1r, lon1r = radians(lat1), radians(lon1)
    lat2r, lon2r = radians(lat2), radians(lon2)
    dlon = lon2r - lon1r
    x = sin(dlon) * cos(lat2r)
    y = cos(lat1r) * sin(lat2r) - sin(lat1r) * cos(lat2r) * cos(dlon)
    return (degrees(atan2(x, y)) + 360) % 360


def heading_diff(h1: float, h2: float) -> float:
    diff = abs(h1 - h2) % 360
    return min(diff, 360 - diff)


def extract_runway_from_procedure(proc_name: str) -> str | None:
    """Extract runway designator from CIFP procedure name.

    Examples:
        R21   -> '21'
        I03-Z -> '03'
        D03-Y -> '03'
        VORA  -> None  (circling, no specific runway)
        RNVA  -> None
    """
    m = re.match(r"^[A-Z](\d{2}[LRC]?)(?:-[A-Z])?$", proc_name.strip())
    return m.group(1) if m else None


def extract_papi_glideslopes(apt_dat_path: Path) -> dict[tuple[str, str], float]:
    """Extract glide slope angles from PAPI/VASI rows (code 21) in apt.dat.

    Associates each PAPI to a runway end by matching orientation heading
    within HEADING_TOLERANCE degrees. Returns max GS per (icao, designator).
    """
    result: dict[tuple[str, str], float] = {}
    current_icao: str | None = None
    current_runways: list[tuple] = []
    current_papis: list[tuple[float, float]] = []

    def flush() -> None:
        if not current_icao or not current_runways or not current_papis:
            return
        for des1, lat1, lon1, des2, lat2, lon2 in current_runways:
            bear12 = bearing(lat1, lon1, lat2, lon2)
            bear21 = (bear12 + 180) % 360
            for des, hdg in [(des1, bear12), (des2, bear21)]:
                best_gs, best_diff = None, HEADING_TOLERANCE + 1
                for ori, gs in current_papis:
                    diff = heading_diff(hdg, ori)
                    if diff < best_diff:
                        best_diff = diff
                        best_gs = gs
                if best_gs is not None and best_gs > 0:
                    key = (current_icao, des)
                    result[key] = max(result.get(key, 0.0), best_gs)

    with open(apt_dat_path, "r", encoding="latin-1") as f:
        for line in f:
            parts = line.split()
            if not parts:
                continue
            if parts[0] in ("1", "16", "17") and len(parts) >= 5:
                flush()
                current_icao = parts[4].upper()
                current_runways = []
                current_papis = []
            elif parts[0] == "100" and len(parts) >= 22:
                current_runways.append((
                    parts[8], float(parts[9]), float(parts[10]),
                    parts[17], float(parts[18]), float(parts[19]),
                ))
            elif parts[0] == "21" and len(parts) >= 6 and parts[3] != RAIL_TYPE:
                gs = float(parts[5])
                if gs > 0:
                    current_papis.append((float(parts[4]), gs))
    flush()

    return result


def extract_cifp_glideslopes(cifp_dir: Path) -> dict[tuple[str, str], float]:
    """Extract glide slope angles from CIFP approach procedure files.

    Parses APPCH lines following ARINC 424 format. The vertical angle field
    (index 28, comma-separated) is encoded as a signed integer where
    -304 = -3.04° (descending). Only negative (descending) values are used.

    Returns max GS per (icao, designator).
    """
    result: dict[tuple[str, str], float] = {}

    if not cifp_dir.exists():
        return result

    for cifp_file in sorted(cifp_dir.glob("*.dat")):
        icao = cifp_file.stem.upper()

        with open(cifp_file, "r", encoding="latin-1") as f:
            for line in f:
                if not line.startswith("APPCH:"):
                    continue
                parts = line.rstrip(";\r\n").split(",")
                if len(parts) < 29:
                    continue

                proc_name = parts[2].strip()
                designator = extract_runway_from_procedure(proc_name)
                if not designator:
                    continue

                vangle_raw = parts[28].strip()
                if not vangle_raw:
                    continue
                try:
                    vangle = int(vangle_raw)
                except ValueError:
                    continue

                # Only descending angles (negative) represent glide slopes
                if vangle >= 0:
                    continue

                gs = abs(vangle) / 100.0
                if gs > 10.0:  # Sanity check (e.g. -90 from corrupt PAPI-like data)
                    continue

                key = (icao, designator)
                result[key] = max(result.get(key, 0.0), gs)

    return result


def main() -> None:
    script_dir = Path(__file__).parent.parent
    apt_dat_path = script_dir / "xplane_data" / "apt.dat"
    cifp_dir = script_dir / "xplane_data" / "CIFP"

    if not apt_dat_path.exists():
        print(f"Error: apt.dat not found at {apt_dat_path}", file=sys.stderr)
        sys.exit(1)

    print("Extracting PAPI/VASI glide slopes from apt.dat...", file=sys.stderr)
    papi_gs = extract_papi_glideslopes(apt_dat_path)
    print(f"  {len(papi_gs)} runway ends with PAPI/VASI data", file=sys.stderr)

    print("Extracting glide slopes from CIFP procedures...", file=sys.stderr)
    cifp_gs = extract_cifp_glideslopes(cifp_dir)
    if cifp_dir.exists():
        cifp_count = len(list(cifp_dir.glob("*.dat")))
        print(f"  {cifp_count} CIFP files processed, {len(cifp_gs)} runway ends found", file=sys.stderr)
    else:
        print("  CIFP directory not found, skipping", file=sys.stderr)

    # Merge: take max from both sources per runway end
    all_keys = set(papi_gs.keys()) | set(cifp_gs.keys())
    merged: dict[tuple[str, str], float] = {}
    for key in all_keys:
        gs = max(papi_gs.get(key, 0.0), cifp_gs.get(key, 0.0))
        if gs > 0:
            merged[key] = gs

    print(f"Total runway ends with glide slope data: {len(merged)}", file=sys.stderr)

    # Only generate UPDATEs for values that differ from the default by >= 0.10°
    # (avoids spurious updates for near-standard values like 3.04°)
    updates = [
        (key, gs)
        for key, gs in merged.items()
        if abs(round(gs, 2) - DEFAULT_GS) >= 0.10
    ]
    updates.sort()

    print(f"Runway ends needing UPDATE (≠ {DEFAULT_GS:.2f}°): {len(updates)}", file=sys.stderr)

    if not updates:
        print("-- No updates needed.", file=sys.stderr)
        return

    print("-- Generated by generate_glideslope_sql.py")
    print(f"-- Updates max_glideslope_deg in runway_end where value differs from default ({DEFAULT_GS:.2f}°)")
    print("-- Sources: apt.dat (PAPI/VASI row 21) and CIFP approach procedures")
    print(f"-- Total updates: {len(updates)}\n")

    for (icao, designator), gs in updates:
        sources = []
        if (icao, designator) in papi_gs:
            sources.append(f"PAPI={papi_gs[(icao, designator)]:.2f}°")
        if (icao, designator) in cifp_gs:
            sources.append(f"CIFP={cifp_gs[(icao, designator)]:.2f}°")

        print(f"-- {icao} RWY {designator} ({', '.join(sources)})")
        print(f"UPDATE runway_end re")
        print(f"JOIN runway r ON re.runway_id = r.id")
        print(f"SET re.max_glideslope_deg = {gs:.2f}")
        print(f"WHERE r.airport_icao = '{icao}' AND re.designator = '{designator}';")
        print()


if __name__ == "__main__":
    main()
