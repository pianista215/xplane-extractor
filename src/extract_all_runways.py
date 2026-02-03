#!/usr/bin/env python3
"""
Extract runway data from X-Plane for all airports in the database.

Connects to MySQL, retrieves all airports, matches them with X-Plane data,
and writes runway information to a file.

Usage:
    uv run python src/extract_all_runways.py [--dry]
"""

import argparse
import sys
from math import atan2, cos, radians, sin, sqrt
from pathlib import Path

import mysql.connector

# Database configuration
DB_CONFIG = {
    "host": "172.19.0.2",
    "user": "airbilbao",
    "password": "complex-password",
    "database": "mam",
}

# Surface type codes
SURFACE_TYPES = {
    1: "Asphalt",
    2: "Concrete",
    3: "Grass",
    4: "Dirt",
    5: "Gravel",
    12: "Dry lakebed",
    13: "Water",
    14: "Snow/Ice",
    15: "Transparent",
}


def get_surface_name(code: int) -> str:
    """Get surface type name from code."""
    return SURFACE_TYPES.get(code, f"Unknown ({code})")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two points in kilometers using Haversine formula."""
    R = 6371  # Earth radius in km
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * R * atan2(sqrt(a), sqrt(1 - a))


def parse_land_runway(parts: list[str]) -> dict:
    """Parse a land runway (row code 100)."""
    return {
        "width": float(parts[1]),
        "surface": int(parts[2]),
        "surface_name": get_surface_name(int(parts[2])),
        "end1": {
            "id": parts[8],
            "lat": float(parts[9]),
            "lon": float(parts[10]),
            "displaced_threshold": float(parts[11]),
            "overrun": float(parts[12]),
        },
        "end2": {
            "id": parts[17],
            "lat": float(parts[18]),
            "lon": float(parts[19]),
            "displaced_threshold": float(parts[20]),
            "overrun": float(parts[21]),
        },
    }


def is_heliport(name: str) -> bool:
    """Check if airport name indicates a heliport/helipad."""
    name_lower = name.lower()
    return "heliport" in name_lower or "helipad" in name_lower


def get_airports_from_db() -> tuple[list[dict], int]:
    """Fetch all airports from the database, filtering out heliports."""
    print("Connecting to database...", file=sys.stderr)
    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor(dictionary=True)

    cursor.execute(
        "SELECT icao_code, name, latitude, longitude FROM airport ORDER BY icao_code"
    )
    all_airports = cursor.fetchall()

    cursor.close()
    conn.close()

    airports = [a for a in all_airports if not is_heliport(a["name"] or "")]
    heliports_filtered = len(all_airports) - len(airports)

    print(f"Loaded {len(airports)} airports from database (filtered {heliports_filtered} heliports)", file=sys.stderr)
    return airports, heliports_filtered


def load_xplane_airports(apt_dat_path: Path) -> tuple[dict, list[tuple], int]:
    """
    Load all airports from apt.dat into memory, filtering out heliports.

    Returns:
        - Dict mapping ICAO -> airport data (with runways)
        - List of (icao, lat, lon) for coordinate-based search
        - Count of filtered heliports
    """
    print("Loading X-Plane apt.dat file...", file=sys.stderr)

    xplane_airports = {}
    coord_index = []
    heliports_filtered = 0

    current_icao = None
    current_name = None
    current_lat = None
    current_lon = None
    current_runways = []
    first_runway_coords = None
    current_is_heliport = False

    with open(apt_dat_path, "r", encoding="latin-1") as f:
        for line in f:
            line = line.rstrip("\n\r")
            parts = line.split()

            if not parts:
                continue

            row_code = parts[0]

            # Start of a new airport
            if row_code in ("1", "16", "17"):
                # Save previous airport if it had runways and is not a heliport
                if current_icao and current_runways:
                    if current_is_heliport:
                        heliports_filtered += 1
                    else:
                        # Use first runway coordinates as airport reference
                        if first_runway_coords:
                            current_lat, current_lon = first_runway_coords

                        xplane_airports[current_icao] = {
                            "name": current_name,
                            "lat": current_lat,
                            "lon": current_lon,
                            "runways": current_runways,
                        }
                        if current_lat is not None and current_lon is not None:
                            coord_index.append((current_icao, current_lat, current_lon))

                # Start new airport
                if len(parts) >= 5:
                    current_icao = parts[4].upper()
                    current_name = " ".join(parts[5:]) if len(parts) > 5 else ""
                    current_runways = []
                    first_runway_coords = None
                    current_lat = None
                    current_lon = None
                    # Row code 17 = Heliport in X-Plane
                    current_is_heliport = row_code == "17"
                else:
                    current_icao = None

            # Land runway (row 100)
            elif row_code == "100" and len(parts) >= 22 and current_icao:
                runway = parse_land_runway(parts)
                current_runways.append(runway)
                # Use first runway's first threshold as airport reference coordinates
                if first_runway_coords is None:
                    first_runway_coords = (runway["end1"]["lat"], runway["end1"]["lon"])

        # Don't forget the last airport
        if current_icao and current_runways:
            if current_is_heliport:
                heliports_filtered += 1
            else:
                if first_runway_coords:
                    current_lat, current_lon = first_runway_coords
                xplane_airports[current_icao] = {
                    "name": current_name,
                    "lat": current_lat,
                    "lon": current_lon,
                    "runways": current_runways,
                }
                if current_lat is not None and current_lon is not None:
                    coord_index.append((current_icao, current_lat, current_lon))

    print(f"Loaded {len(xplane_airports)} X-Plane airports with runways (filtered {heliports_filtered} heliports)", file=sys.stderr)
    return xplane_airports, coord_index, heliports_filtered


def find_airport_by_coords(
    coord_index: list[tuple], lat: float, lon: float, max_distance_km: float = 2.0
) -> str | None:
    """Find nearest X-Plane airport by coordinates within max distance."""
    best_match = None
    best_distance = max_distance_km

    for icao, apt_lat, apt_lon in coord_index:
        distance = haversine_km(lat, lon, apt_lat, apt_lon)
        if distance < best_distance:
            best_distance = distance
            best_match = icao

    return best_match


def format_runway_data(icao: str, name: str, runways: list[dict]) -> str:
    """Format runway data for output file."""
    lines = [f"# Airport: {icao} - {name}"]

    for rwy in runways:
        lines.append(
            f"RWY {rwy['end1']['id']}/{rwy['end2']['id']} "
            f"width={rwy['width']} surface={rwy['surface_name']}"
        )
        lines.append(
            f"  {rwy['end1']['id']}: "
            f"lat={rwy['end1']['lat']:.6f} lon={rwy['end1']['lon']:.6f} "
            f"displaced={rwy['end1']['displaced_threshold']} "
            f"stopway={rwy['end1']['overrun']}"
        )
        lines.append(
            f"  {rwy['end2']['id']}: "
            f"lat={rwy['end2']['lat']:.6f} lon={rwy['end2']['lon']:.6f} "
            f"displaced={rwy['end2']['displaced_threshold']} "
            f"stopway={rwy['end2']['overrun']}"
        )

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Extract runway data from X-Plane")
    parser.add_argument("--dry", action="store_true", help="Dry run, don't write output file")
    args = parser.parse_args()

    # Find apt.dat file
    script_dir = Path(__file__).parent.parent
    apt_dat_path = script_dir / "xplane_data" / "apt.dat"

    if not apt_dat_path.exists():
        print(f"Error: apt.dat not found at {apt_dat_path}", file=sys.stderr)
        print("\nPlease copy the apt.dat file from X-Plane to:", file=sys.stderr)
        print(f"  {apt_dat_path}", file=sys.stderr)
        sys.exit(1)

    # Load data
    db_airports, db_heliports_filtered = get_airports_from_db()
    xplane_airports, coord_index, xplane_heliports_filtered = load_xplane_airports(apt_dat_path)

    # Track X-Plane airports that were matched
    matched_xplane_icaos = set()

    # Process airports
    output_path = Path("/tmp/runways_extracted.txt")
    if not args.dry:
        print(f"Writing results to {output_path}...", file=sys.stderr)

    found_count = 0
    not_found_in_xplane = []  # DB airports not found in X-Plane
    out_file = None if args.dry else open(output_path, "w", encoding="utf-8")

    try:
        for i, db_apt in enumerate(db_airports):
            icao = db_apt["icao_code"]
            name = db_apt["name"]
            lat = float(db_apt["latitude"]) if db_apt["latitude"] else None
            lon = float(db_apt["longitude"]) if db_apt["longitude"] else None

            # Progress indicator
            if (i + 1) % 1000 == 0:
                print(f"Processed {i + 1}/{len(db_airports)} airports...", file=sys.stderr)

            # Try to find in X-Plane by ICAO
            xplane_icao = None
            if icao in xplane_airports:
                xplane_icao = icao
            elif lat is not None and lon is not None:
                # Try coordinate-based search
                xplane_icao = find_airport_by_coords(coord_index, lat, lon)

            if xplane_icao:
                xplane_apt = xplane_airports[xplane_icao]
                matched_xplane_icaos.add(xplane_icao)
                found_count += 1

                # Write runway data
                if out_file:
                    out_file.write(format_runway_data(icao, name, xplane_apt["runways"]))
                    out_file.write("\n\n")
            else:
                not_found_in_xplane.append((icao, name))
    finally:
        if out_file:
            out_file.close()

    # Report X-Plane airports not found in database
    unmatched_xplane = set(xplane_airports.keys()) - matched_xplane_icaos
    for icao in sorted(unmatched_xplane):
        apt = xplane_airports[icao]
        # Sanitize name to avoid encoding issues
        safe_name = apt['name'].encode('ascii', 'replace').decode('ascii')
        print(f"WARNING: X-Plane airport {icao} ({safe_name}) not found in database")

    # Report DB airports not found in X-Plane
    for icao, name in sorted(not_found_in_xplane):
        print(f"WARNING: DB airport {icao} ({name}) not found in X-Plane")

    # Summary
    print(f"\nSummary:")
    print(f"  Database airports: {len(db_airports)} (filtered {db_heliports_filtered} heliports)")
    print(f"  X-Plane airports: {len(xplane_airports)} (filtered {xplane_heliports_filtered} heliports)")
    print(f"  Matched with X-Plane: {found_count}")
    print(f"  Not found in X-Plane: {len(not_found_in_xplane)}")
    print(f"  X-Plane airports not in DB: {len(unmatched_xplane)}")
    if not args.dry:
        print(f"\nResults written to: {output_path}")


if __name__ == "__main__":
    main()
