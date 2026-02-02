#!/usr/bin/env python3
"""
X-Plane airport and runway data extractor.

Usage:
    uv run python src/extract_airport.py ICAO_CODE

Example:
    uv run python src/extract_airport.py LEMD
"""

import sys
from pathlib import Path

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


def parse_land_runway(parts: list[str]) -> dict:
    """Parse a land runway (row code 100)."""
    return {
        "type": "land",
        "width": float(parts[1]),
        "surface": int(parts[2]),
        "surface_name": get_surface_name(int(parts[2])),
        "smoothness": float(parts[4]),
        "centerline_lights": parts[5] == "1",
        "edge_lights": int(parts[6]),
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


def parse_water_runway(parts: list[str]) -> dict:
    """Parse a water runway (row code 101)."""
    return {
        "type": "water",
        "width": float(parts[1]),
        "buoys": parts[2] == "1",
        "end1": {
            "id": parts[3],
            "lat": float(parts[4]),
            "lon": float(parts[5]),
        },
        "end2": {
            "id": parts[6],
            "lat": float(parts[7]),
            "lon": float(parts[8]),
        },
    }


def parse_helipad(parts: list[str]) -> dict:
    """Parse a helipad (row code 102)."""
    return {
        "type": "helipad",
        "id": parts[1],
        "lat": float(parts[2]),
        "lon": float(parts[3]),
        "heading": float(parts[4]),
        "length": float(parts[5]),
        "width": float(parts[6]),
        "surface": int(parts[7]),
        "surface_name": get_surface_name(int(parts[7])),
    }


def parse_airport(lines: list[str]) -> dict | None:
    """Parse airport data from apt.dat lines."""
    if not lines:
        return None

    first_line = lines[0].split()
    if len(first_line) < 5:
        return None

    row_code = first_line[0]
    if row_code not in ("1", "16", "17"):  # Airport, Seaport, Heliport
        return None

    airport = {
        "elevation": float(first_line[1]),
        "icao": first_line[4],
        "name": " ".join(first_line[5:]) if len(first_line) > 5 else "",
        "land_runways": [],
        "water_runways": [],
        "helipads": [],
    }

    for line in lines[1:]:
        parts = line.split()
        if not parts:
            continue

        row_code = parts[0]

        if row_code == "100" and len(parts) >= 22:
            airport["land_runways"].append(parse_land_runway(parts))
        elif row_code == "101" and len(parts) >= 9:
            airport["water_runways"].append(parse_water_runway(parts))
        elif row_code == "102" and len(parts) >= 8:
            airport["helipads"].append(parse_helipad(parts))

    return airport


def find_airport(apt_dat_path: Path, icao_code: str) -> dict | None:
    """Find an airport by ICAO code in the apt.dat file."""
    icao_upper = icao_code.upper()
    current_airport_lines: list[str] = []
    found = False

    with open(apt_dat_path, "r", encoding="latin-1") as f:
        for line in f:
            line = line.rstrip("\n\r")
            parts = line.split()

            if not parts:
                continue

            row_code = parts[0]

            # Start of a new airport
            if row_code in ("1", "16", "17"):
                # If we had accumulated an airport and it was the one we're looking for, parse it
                if found and current_airport_lines:
                    return parse_airport(current_airport_lines)

                current_airport_lines = [line]

                # Check if this is the airport we're looking for
                if len(parts) >= 5 and parts[4].upper() == icao_upper:
                    found = True
                else:
                    found = False
            elif found:
                current_airport_lines.append(line)

    # Check the last airport in the file
    if found and current_airport_lines:
        return parse_airport(current_airport_lines)

    return None


def print_airport(airport: dict) -> None:
    """Print airport data in readable format."""
    print("=" * 60)
    print(f"Airport: {airport['name']}")
    print(f"ICAO Code: {airport['icao']}")
    print(f"Elevation: {airport['elevation']} ft")
    print("=" * 60)

    # Land runways
    if airport["land_runways"]:
        print(f"\n RUNWAYS ({len(airport['land_runways'])})")
        print("-" * 40)
        for rwy in airport["land_runways"]:
            print(f"\n  Runway {rwy['end1']['id']}/{rwy['end2']['id']}")
            print(f"    Width: {rwy['width']} m")
            print(f"    Surface: {rwy['surface_name']}")
            print(f"    Smoothness: {rwy['smoothness']}")
            print(f"    Centerline lights: {'Yes' if rwy['centerline_lights'] else 'No'}")

            print(f"\n    Threshold {rwy['end1']['id']}:")
            print(f"      Coordinates: {rwy['end1']['lat']:.6f}, {rwy['end1']['lon']:.6f}")
            if rwy["end1"]["displaced_threshold"] > 0:
                print(f"      Displaced threshold: {rwy['end1']['displaced_threshold']} m")
            if rwy["end1"]["overrun"] > 0:
                print(f"      Stopway: {rwy['end1']['overrun']} m")

            print(f"\n    Threshold {rwy['end2']['id']}:")
            print(f"      Coordinates: {rwy['end2']['lat']:.6f}, {rwy['end2']['lon']:.6f}")
            if rwy["end2"]["displaced_threshold"] > 0:
                print(f"      Displaced threshold: {rwy['end2']['displaced_threshold']} m")
            if rwy["end2"]["overrun"] > 0:
                print(f"      Stopway: {rwy['end2']['overrun']} m")

    # Water runways
    if airport["water_runways"]:
        print(f"\n WATER RUNWAYS ({len(airport['water_runways'])})")
        print("-" * 40)
        for rwy in airport["water_runways"]:
            print(f"\n  Runway {rwy['end1']['id']}/{rwy['end2']['id']}")
            print(f"    Width: {rwy['width']} m")
            print(f"    Buoys: {'Yes' if rwy['buoys'] else 'No'}")
            print(f"    End {rwy['end1']['id']}: {rwy['end1']['lat']:.6f}, {rwy['end1']['lon']:.6f}")
            print(f"    End {rwy['end2']['id']}: {rwy['end2']['lat']:.6f}, {rwy['end2']['lon']:.6f}")

    # Helipads
    if airport["helipads"]:
        print(f"\n HELIPADS ({len(airport['helipads'])})")
        print("-" * 40)
        for hp in airport["helipads"]:
            print(f"\n  Helipad {hp['id']}")
            print(f"    Coordinates: {hp['lat']:.6f}, {hp['lon']:.6f}")
            print(f"    Heading: {hp['heading']}°")
            print(f"    Dimensions: {hp['length']} x {hp['width']} m")
            print(f"    Surface: {hp['surface_name']}")

    # Summary
    print("\n" + "=" * 60)
    total = len(airport["land_runways"]) + len(airport["water_runways"]) + len(airport["helipads"])
    print(f"Total: {total} item(s)")
    print("=" * 60)


def main():
    if len(sys.argv) < 2:
        print("Usage: uv run python src/extract_airport.py ICAO_CODE")
        print("Example: uv run python src/extract_airport.py LEMD")
        sys.exit(1)

    icao_code = sys.argv[1].upper()

    # Find apt.dat file
    script_dir = Path(__file__).parent.parent
    apt_dat_path = script_dir / "xplane_data" / "apt.dat"

    if not apt_dat_path.exists():
        print(f"Error: apt.dat not found at {apt_dat_path}")
        print("\nPlease copy the apt.dat file from X-Plane to:")
        print(f"  {apt_dat_path}")
        print("\nSource:")
        print("  X-Plane 12/Global Scenery/Global Airports/Earth nav data/apt.dat")
        sys.exit(1)

    print(f"Searching for airport {icao_code}...")

    airport = find_airport(apt_dat_path, icao_code)

    if airport is None:
        print(f"Error: Airport with ICAO code '{icao_code}' not found")
        sys.exit(1)

    print_airport(airport)


if __name__ == "__main__":
    main()
