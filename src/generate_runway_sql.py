#!/usr/bin/env python3
"""
Generate SQL INSERT statements for runway data from X-Plane apt.dat.

Queries all airports from the database that don't have runway data,
then tries to find them in X-Plane by ICAO or coordinates.

Usage:
    uv run python src/generate_runway_sql.py --host localhost --user root --password secret --database mam
    uv run python src/generate_runway_sql.py -H localhost -u root -p secret -d mam > runways.sql
"""

import argparse
import sys
from math import atan2, cos, degrees, radians, sin, sqrt
from pathlib import Path

import mysql.connector


# Maximum distance in km to match airport by coordinates
MAX_COORDINATE_DISTANCE_KM = 5.0


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance in meters between two points using Haversine formula."""
    R = 6371000  # Earth radius in meters

    lat1_r, lon1_r = radians(lat1), radians(lon1)
    lat2_r, lon2_r = radians(lat2), radians(lon2)

    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r

    a = sin(dlat / 2) ** 2 + cos(lat1_r) * cos(lat2_r) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))

    return R * c


def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate bearing in degrees from point 1 to point 2."""
    lat1_r, lon1_r = radians(lat1), radians(lon1)
    lat2_r, lon2_r = radians(lat2), radians(lon2)

    dlon = lon2_r - lon1_r
    x = sin(dlon) * cos(lat2_r)
    y = cos(lat1_r) * sin(lat2_r) - sin(lat1_r) * cos(lat2_r) * cos(dlon)

    return (degrees(atan2(x, y)) + 360) % 360


def parse_land_runway(parts: list[str]) -> dict:
    """Parse a land runway (row code 100)."""
    return {
        "width": float(parts[1]),
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


def parse_airport(lines: list[str]) -> dict | None:
    """Parse airport data from apt.dat lines."""
    if not lines:
        return None

    first_line = lines[0].split()
    if len(first_line) < 5:
        return None

    row_code = first_line[0]
    if row_code not in ("1", "16", "17"):
        return None

    airport = {
        "icao": first_line[4],
        "name": " ".join(first_line[5:]) if len(first_line) > 5 else "",
        "land_runways": [],
        "lat": None,
        "lon": None,
    }

    for line in lines[1:]:
        parts = line.split()
        if not parts:
            continue

        row_code = parts[0]
        if row_code == "100" and len(parts) >= 22:
            rwy = parse_land_runway(parts)
            airport["land_runways"].append(rwy)
            # Use first runway center as airport coordinates
            if airport["lat"] is None:
                airport["lat"] = (rwy["end1"]["lat"] + rwy["end2"]["lat"]) / 2
                airport["lon"] = (rwy["end1"]["lon"] + rwy["end2"]["lon"]) / 2

    return airport


def load_xplane_airport_index(apt_dat_path: Path) -> dict[str, dict]:
    """
    Build an index of ICAO -> {offset, lat, lon} for quick lookups.
    Reads the file once, capturing file positions and first runway coordinates.
    """
    index = {}
    current_icao = None

    with open(apt_dat_path, "r", encoding="latin-1") as f:
        while True:
            pos = f.tell()
            line = f.readline()
            if not line:
                break

            parts = line.split()
            if not parts:
                continue

            if parts[0] in ("1", "16", "17") and len(parts) >= 5:
                current_icao = parts[4].upper()
                index[current_icao] = {"offset": pos, "lat": None, "lon": None}
            elif parts[0] == "100" and len(parts) >= 22 and current_icao:
                entry = index[current_icao]
                # Only capture coords from the first runway
                if entry["lat"] is None:
                    entry["lat"] = (float(parts[9]) + float(parts[18])) / 2
                    entry["lon"] = (float(parts[10]) + float(parts[19])) / 2

    return index


def find_airport_at_offset(apt_dat_path: Path, offset: int) -> dict | None:
    """Read and parse an airport at a given file offset."""
    with open(apt_dat_path, "r", encoding="latin-1") as f:
        f.seek(offset)

        current_airport_lines = []
        for line in f:
            line = line.rstrip("\n\r")
            parts = line.split()

            if not parts:
                continue

            row_code = parts[0]

            if row_code in ("1", "16", "17"):
                if current_airport_lines:
                    # We've hit the next airport, return what we have
                    return parse_airport(current_airport_lines)
                current_airport_lines = [line]
            elif current_airport_lines:
                current_airport_lines.append(line)

        if current_airport_lines:
            return parse_airport(current_airport_lines)

    return None


def find_nearest_icao_in_index(
    target_lat: float,
    target_lon: float,
    index: dict[str, dict],
) -> str | None:
    """Find the nearest airport ICAO in the index within MAX_COORDINATE_DISTANCE_KM."""
    best_icao = None
    best_distance = MAX_COORDINATE_DISTANCE_KM * 1000  # Convert to meters

    for icao, entry in index.items():
        if entry["lat"] is None:
            continue
        dist = haversine_distance(target_lat, target_lon, entry["lat"], entry["lon"])
        if dist < best_distance:
            best_distance = dist
            best_icao = icao

    return best_icao


def get_airports_without_runways(connection) -> list[dict]:
    """Get airports that don't have runway data."""
    cursor = connection.cursor(dictionary=True)
    cursor.execute("""
        SELECT a.icao_code, a.name, a.latitude, a.longitude
        FROM airport a
        LEFT JOIN runway r ON r.airport_icao = a.icao_code
        WHERE r.id IS NULL
        ORDER BY a.icao_code
    """)
    result = cursor.fetchall()
    cursor.close()
    return result


def generate_sql_for_airport(db_icao: str, airport: dict) -> str:
    """Generate SQL INSERT statements for an airport's runways."""
    if not airport["land_runways"]:
        return ""

    sql_lines = [f"-- {db_icao}: {airport['name']}"]

    if airport["icao"] != db_icao:
        sql_lines.append(f"-- (matched from X-Plane ICAO: {airport['icao']})")

    for rwy in airport["land_runways"]:
        end1 = rwy["end1"]
        end2 = rwy["end2"]

        # Calculate length and bearings
        length_m = haversine_distance(
            end1["lat"], end1["lon"],
            end2["lat"], end2["lon"]
        )
        bearing_1_to_2 = calculate_bearing(
            end1["lat"], end1["lon"],
            end2["lat"], end2["lon"]
        )
        bearing_2_to_1 = (bearing_1_to_2 + 180) % 360

        designators = f"{end1['id']}/{end2['id']}"

        # INSERT runway
        sql_lines.append(f"""
INSERT INTO runway (airport_icao, designators, width_m, length_m)
SELECT '{db_icao}', '{designators}', {rwy['width']:.1f}, {length_m:.1f}
WHERE NOT EXISTS (
    SELECT 1 FROM runway WHERE airport_icao = '{db_icao}' AND designators = '{designators}'
);""")

        # INSERT runway_end for end1
        sql_lines.append(f"""
INSERT INTO runway_end (runway_id, designator, latitude, longitude, true_heading_deg, displaced_threshold_m, stopway_m)
SELECT r.id, '{end1['id']}', {end1['lat']:.7f}, {end1['lon']:.7f}, {bearing_1_to_2:.2f}, {end1['displaced_threshold']:.1f}, {end1['overrun']:.1f}
FROM runway r
WHERE r.airport_icao = '{db_icao}' AND r.designators = '{designators}'
AND NOT EXISTS (
    SELECT 1 FROM runway_end re WHERE re.runway_id = r.id AND re.designator = '{end1['id']}'
);""")

        # INSERT runway_end for end2
        sql_lines.append(f"""
INSERT INTO runway_end (runway_id, designator, latitude, longitude, true_heading_deg, displaced_threshold_m, stopway_m)
SELECT r.id, '{end2['id']}', {end2['lat']:.7f}, {end2['lon']:.7f}, {bearing_2_to_1:.2f}, {end2['displaced_threshold']:.1f}, {end2['overrun']:.1f}
FROM runway r
WHERE r.airport_icao = '{db_icao}' AND r.designators = '{designators}'
AND NOT EXISTS (
    SELECT 1 FROM runway_end re WHERE re.runway_id = r.id AND re.designator = '{end2['id']}'
);""")

    return "\n".join(sql_lines)


def main():
    parser = argparse.ArgumentParser(
        description="Generate SQL for runway data from X-Plane apt.dat"
    )
    parser.add_argument("-H", "--host", default="localhost", help="Database host (default: localhost)")
    parser.add_argument("-u", "--user", default="root", help="Database user (default: root)")
    parser.add_argument("-p", "--password", default="", help="Database password (default: empty)")
    parser.add_argument("-d", "--database", default="mam", help="Database name (default: mam)")
    parser.add_argument("-P", "--port", type=int, default=3306, help="Database port (default: 3306)")

    args = parser.parse_args()

    # Find apt.dat file
    script_dir = Path(__file__).parent.parent
    apt_dat_path = script_dir / "xplane_data" / "apt.dat"

    if not apt_dat_path.exists():
        print(f"Error: apt.dat not found at {apt_dat_path}", file=sys.stderr)
        sys.exit(1)

    # Connect to database
    try:
        connection = mysql.connector.connect(
            host=args.host,
            user=args.user,
            password=args.password,
            database=args.database,
            port=args.port,
        )
    except mysql.connector.Error as e:
        print(f"Error connecting to database: {e}", file=sys.stderr)
        sys.exit(1)

    # Get airports without runways
    airports = get_airports_without_runways(connection)
    connection.close()

    if not airports:
        print("All airports already have runway data.", file=sys.stderr)
        sys.exit(0)

    print(f"Found {len(airports)} airports without runway data.", file=sys.stderr)
    print("Building X-Plane airport index...", file=sys.stderr)

    # Build index for fast ICAO lookups
    xplane_index = load_xplane_airport_index(apt_dat_path)
    print(f"Indexed {len(xplane_index)} airports from X-Plane.", file=sys.stderr)

    # Process each airport
    sql_output = []
    matched_by_icao = []
    matched_by_coords = []
    not_found = []
    no_runways = []

    for db_airport in airports:
        icao = db_airport["icao_code"]
        xplane_icao = None

        # Try to find by ICAO first
        if icao.upper() in xplane_index:
            xplane_icao = icao.upper()
        elif db_airport["latitude"] and db_airport["longitude"]:
            # Try to find by coordinates (in-memory, fast)
            xplane_icao = find_nearest_icao_in_index(
                db_airport["latitude"],
                db_airport["longitude"],
                xplane_index
            )

        if xplane_icao is None:
            not_found.append(icao)
            continue

        # Read and parse the airport from the file
        airport = find_airport_at_offset(apt_dat_path, xplane_index[xplane_icao]["offset"])

        if airport is None or not airport["land_runways"]:
            no_runways.append(icao)
            continue

        sql = generate_sql_for_airport(icao, airport)
        if sql:
            sql_output.append(sql)
            if xplane_icao == icao.upper():
                matched_by_icao.append(icao)
            else:
                matched_by_coords.append(f"{icao} -> {xplane_icao}")

    # Print summary to stderr
    print("\n--- Summary ---", file=sys.stderr)
    print(f"Matched by ICAO: {len(matched_by_icao)}", file=sys.stderr)
    if matched_by_coords:
        print(f"Matched by coordinates: {len(matched_by_coords)}", file=sys.stderr)
        for match in matched_by_coords:
            print(f"  {match}", file=sys.stderr)
    if no_runways:
        print(f"Found but no land runways: {len(no_runways)} ({', '.join(no_runways[:10])}{'...' if len(no_runways) > 10 else ''})", file=sys.stderr)
    if not_found:
        print(f"Not found in X-Plane: {len(not_found)} ({', '.join(not_found[:10])}{'...' if len(not_found) > 10 else ''})", file=sys.stderr)

    # Print SQL to stdout
    if sql_output:
        print("-- Generated by generate_runway_sql.py")
        print("-- Only airports without existing runway data\n")
        print("\n\n".join(sql_output))


if __name__ == "__main__":
    main()
