#!/usr/bin/env python3
"""
X-Plane airport and runway data extractor.

Usage:
    uv run python src/extract_airport.py ICAO_CODE

Example:
    uv run python src/extract_airport.py LEMD
"""

import json
import sys
from math import atan2, cos, degrees, radians, sin
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


def offset_point(lat: float, lon: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    """
    Calculate a new point given a starting point, bearing, and distance.
    Returns (lat, lon) in degrees.
    """
    R = 6371000  # Earth radius in meters
    bearing = radians(bearing_deg)
    lat1 = radians(lat)
    lon1 = radians(lon)

    lat2 = lat1 + (distance_m / R) * cos(bearing)
    lon2 = lon1 + (distance_m / R) * sin(bearing) / cos(lat1)

    return degrees(lat2), degrees(lon2)


def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate bearing in degrees from point 1 to point 2."""
    lat1_r, lon1_r = radians(lat1), radians(lon1)
    lat2_r, lon2_r = radians(lat2), radians(lon2)

    dlon = lon2_r - lon1_r
    x = sin(dlon) * cos(lat2_r)
    y = cos(lat1_r) * sin(lat2_r) - sin(lat1_r) * cos(lat2_r) * cos(dlon)

    return (degrees(atan2(x, y)) + 360) % 360


def generate_runway_polygon(rwy: dict) -> dict:
    """Generate GeoJSON polygon for a runway with all zones."""
    end1 = rwy["end1"]
    end2 = rwy["end2"]
    width = rwy["width"]
    half_width = width / 2

    # Calculate bearing from end1 to end2
    bearing = calculate_bearing(end1["lat"], end1["lon"], end2["lat"], end2["lon"])
    reverse_bearing = (bearing + 180) % 360
    perp_left = (bearing - 90) % 360
    perp_right = (bearing + 90) % 360

    features = []

    # Stopway/overrun end1 (before threshold 1)
    if end1["overrun"] > 0:
        p1 = offset_point(end1["lat"], end1["lon"], reverse_bearing, end1["overrun"])
        p1_left = offset_point(p1[0], p1[1], perp_left, half_width)
        p1_right = offset_point(p1[0], p1[1], perp_right, half_width)
        t1_left = offset_point(end1["lat"], end1["lon"], perp_left, half_width)
        t1_right = offset_point(end1["lat"], end1["lon"], perp_right, half_width)

        features.append({
            "type": "Feature",
            "properties": {"zone": "stopway", "label": f"Stopway {end1['id']} ({end1['overrun']}m)"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [p1_left[1], p1_left[0]],
                    [p1_right[1], p1_right[0]],
                    [t1_right[1], t1_right[0]],
                    [t1_left[1], t1_left[0]],
                    [p1_left[1], p1_left[0]],
                ]]
            }
        })

    # Stopway/overrun end2 (after threshold 2)
    if end2["overrun"] > 0:
        p2 = offset_point(end2["lat"], end2["lon"], bearing, end2["overrun"])
        p2_left = offset_point(p2[0], p2[1], perp_left, half_width)
        p2_right = offset_point(p2[0], p2[1], perp_right, half_width)
        t2_left = offset_point(end2["lat"], end2["lon"], perp_left, half_width)
        t2_right = offset_point(end2["lat"], end2["lon"], perp_right, half_width)

        features.append({
            "type": "Feature",
            "properties": {"zone": "stopway", "label": f"Stopway {end2['id']} ({end2['overrun']}m)"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [t2_left[1], t2_left[0]],
                    [t2_right[1], t2_right[0]],
                    [p2_right[1], p2_right[0]],
                    [p2_left[1], p2_left[0]],
                    [t2_left[1], t2_left[0]],
                ]]
            }
        })

    # Displaced threshold end1
    if end1["displaced_threshold"] > 0:
        dt1 = offset_point(end1["lat"], end1["lon"], bearing, end1["displaced_threshold"])
        t1_left = offset_point(end1["lat"], end1["lon"], perp_left, half_width)
        t1_right = offset_point(end1["lat"], end1["lon"], perp_right, half_width)
        dt1_left = offset_point(dt1[0], dt1[1], perp_left, half_width)
        dt1_right = offset_point(dt1[0], dt1[1], perp_right, half_width)

        features.append({
            "type": "Feature",
            "properties": {"zone": "displaced", "label": f"Displaced {end1['id']} ({end1['displaced_threshold']}m)"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [t1_left[1], t1_left[0]],
                    [t1_right[1], t1_right[0]],
                    [dt1_right[1], dt1_right[0]],
                    [dt1_left[1], dt1_left[0]],
                    [t1_left[1], t1_left[0]],
                ]]
            }
        })

    # Displaced threshold end2
    if end2["displaced_threshold"] > 0:
        dt2 = offset_point(end2["lat"], end2["lon"], reverse_bearing, end2["displaced_threshold"])
        t2_left = offset_point(end2["lat"], end2["lon"], perp_left, half_width)
        t2_right = offset_point(end2["lat"], end2["lon"], perp_right, half_width)
        dt2_left = offset_point(dt2[0], dt2[1], perp_left, half_width)
        dt2_right = offset_point(dt2[0], dt2[1], perp_right, half_width)

        features.append({
            "type": "Feature",
            "properties": {"zone": "displaced", "label": f"Displaced {end2['id']} ({end2['displaced_threshold']}m)"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [dt2_left[1], dt2_left[0]],
                    [dt2_right[1], dt2_right[0]],
                    [t2_right[1], t2_right[0]],
                    [t2_left[1], t2_left[0]],
                    [dt2_left[1], dt2_left[0]],
                ]]
            }
        })

    # Main runway surface (between thresholds, excluding displaced areas)
    start1_lat, start1_lon = end1["lat"], end1["lon"]
    start2_lat, start2_lon = end2["lat"], end2["lon"]

    if end1["displaced_threshold"] > 0:
        start1_lat, start1_lon = offset_point(end1["lat"], end1["lon"], bearing, end1["displaced_threshold"])
    if end2["displaced_threshold"] > 0:
        start2_lat, start2_lon = offset_point(end2["lat"], end2["lon"], reverse_bearing, end2["displaced_threshold"])

    s1_left = offset_point(start1_lat, start1_lon, perp_left, half_width)
    s1_right = offset_point(start1_lat, start1_lon, perp_right, half_width)
    s2_left = offset_point(start2_lat, start2_lon, perp_left, half_width)
    s2_right = offset_point(start2_lat, start2_lon, perp_right, half_width)

    features.append({
        "type": "Feature",
        "properties": {"zone": "runway", "label": f"Runway {end1['id']}/{end2['id']} ({rwy['width']}m wide)"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [s1_left[1], s1_left[0]],
                [s1_right[1], s1_right[0]],
                [s2_right[1], s2_right[0]],
                [s2_left[1], s2_left[0]],
                [s1_left[1], s1_left[0]],
            ]]
        }
    })

    # Threshold markers (points)
    features.append({
        "type": "Feature",
        "properties": {"zone": "threshold", "label": f"THR {end1['id']}"},
        "geometry": {"type": "Point", "coordinates": [end1["lon"], end1["lat"]]}
    })
    features.append({
        "type": "Feature",
        "properties": {"zone": "threshold", "label": f"THR {end2['id']}"},
        "geometry": {"type": "Point", "coordinates": [end2["lon"], end2["lat"]]}
    })

    return features


def generate_map_html(airport: dict, output_path: Path) -> None:
    """Generate an HTML file with OpenLayers map showing runways."""
    features = []

    for rwy in airport["land_runways"]:
        features.extend(generate_runway_polygon(rwy))

    # Calculate center point
    all_lats = []
    all_lons = []
    for rwy in airport["land_runways"]:
        all_lats.extend([rwy["end1"]["lat"], rwy["end2"]["lat"]])
        all_lons.extend([rwy["end1"]["lon"], rwy["end2"]["lon"]])

    center_lat = sum(all_lats) / len(all_lats) if all_lats else 0
    center_lon = sum(all_lons) / len(all_lons) if all_lons else 0

    geojson = {"type": "FeatureCollection", "features": features}

    html = f'''<!DOCTYPE html>
<html>
<head>
    <title>{airport["icao"]} - {airport["name"]}</title>
    <meta charset="utf-8">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/ol@v8.2.0/ol.css">
    <style>
        html, body, #map {{ height: 100%; margin: 0; padding: 0; }}
        .legend {{
            position: absolute;
            top: 10px;
            right: 10px;
            background: white;
            padding: 10px;
            border-radius: 5px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.3);
            font-family: sans-serif;
            font-size: 12px;
        }}
        .legend-item {{ display: flex; align-items: center; margin: 5px 0; }}
        .legend-color {{ width: 20px; height: 20px; margin-right: 8px; border: 1px solid #333; }}
        .info {{
            position: absolute;
            bottom: 10px;
            left: 10px;
            background: white;
            padding: 10px;
            border-radius: 5px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.3);
            font-family: sans-serif;
            font-size: 12px;
            max-width: 300px;
        }}
        #popup {{
            position: absolute;
            background: white;
            padding: 8px 12px;
            border-radius: 4px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.3);
            font-family: sans-serif;
            font-size: 12px;
            pointer-events: none;
        }}
    </style>
</head>
<body>
    <div id="map"></div>
    <div class="legend">
        <strong>{airport["icao"]} - {airport["name"]}</strong>
        <hr>
        <div class="legend-item"><div class="legend-color" style="background: #333;"></div>Runway</div>
        <div class="legend-item"><div class="legend-color" style="background: #ff9800;"></div>Displaced Threshold</div>
        <div class="legend-item"><div class="legend-color" style="background: #f44336;"></div>Stopway/Overrun</div>
        <div class="legend-item"><div class="legend-color" style="background: #4caf50; border-radius: 50%;"></div>Threshold</div>
    </div>
    <div class="info">
        <strong>Runways:</strong><br>
        {"<br>".join(f"{rwy['end1']['id']}/{rwy['end2']['id']}: {rwy['width']}m wide, {rwy['surface_name']}" for rwy in airport["land_runways"])}
    </div>
    <div id="popup" style="display: none;"></div>

    <script src="https://cdn.jsdelivr.net/npm/ol@v8.2.0/dist/ol.js"></script>
    <script>
        const geojsonData = {json.dumps(geojson)};

        const styles = {{
            'runway': new ol.style.Style({{
                fill: new ol.style.Fill({{ color: 'rgba(51, 51, 51, 0.8)' }}),
                stroke: new ol.style.Stroke({{ color: '#000', width: 1 }})
            }}),
            'displaced': new ol.style.Style({{
                fill: new ol.style.Fill({{ color: 'rgba(255, 152, 0, 0.8)' }}),
                stroke: new ol.style.Stroke({{ color: '#e65100', width: 1 }})
            }}),
            'stopway': new ol.style.Style({{
                fill: new ol.style.Fill({{ color: 'rgba(244, 67, 54, 0.8)' }}),
                stroke: new ol.style.Stroke({{ color: '#b71c1c', width: 1 }})
            }}),
            'threshold': new ol.style.Style({{
                image: new ol.style.Circle({{
                    radius: 8,
                    fill: new ol.style.Fill({{ color: '#4caf50' }}),
                    stroke: new ol.style.Stroke({{ color: '#1b5e20', width: 2 }})
                }})
            }})
        }};

        const vectorSource = new ol.source.Vector({{
            features: new ol.format.GeoJSON().readFeatures(geojsonData, {{
                featureProjection: 'EPSG:3857'
            }})
        }});

        const vectorLayer = new ol.layer.Vector({{
            source: vectorSource,
            style: function(feature) {{
                return styles[feature.get('zone')];
            }}
        }});

        const map = new ol.Map({{
            target: 'map',
            layers: [
                new ol.layer.Tile({{
                    source: new ol.source.XYZ({{
                        url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}',
                        attributions: 'Tiles &copy; Esri'
                    }})
                }}),
                vectorLayer
            ],
            view: new ol.View({{
                center: ol.proj.fromLonLat([{center_lon}, {center_lat}]),
                zoom: 15
            }})
        }});

        // Fit view to features
        map.getView().fit(vectorSource.getExtent(), {{ padding: [50, 50, 50, 50] }});

        // Popup on hover
        const popup = document.getElementById('popup');
        map.on('pointermove', function(e) {{
            const feature = map.forEachFeatureAtPixel(e.pixel, f => f);
            if (feature) {{
                popup.style.display = 'block';
                popup.style.left = (e.pixel[0] + 10) + 'px';
                popup.style.top = (e.pixel[1] + 10) + 'px';
                popup.textContent = feature.get('label');
            }} else {{
                popup.style.display = 'none';
            }}
        }});
    </script>
</body>
</html>'''

    output_path.write_text(html)
    print(f"\nMap generated: {output_path}")


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

    # Generate map HTML
    if airport["land_runways"]:
        output_path = Path(f"/tmp/{icao_code}_runways.html")
        generate_map_html(airport, output_path)


if __name__ == "__main__":
    main()
