# X-Plane Runway Extractor

Extracts runway data from X-Plane's scenery files (`apt.dat`) and generates SQL to load it into the [MAM](https://github.com/pianista215/mam) (Modern Airlines Manager) database. This enables airport views in MAM to display runway ends with headings and draw runway polygons on the map.

## Related Projects

- [MAM](https://github.com/pianista215/mam) - Virtual airline management web application
- [MAM ACARS](https://github.com/pianista215/mam-acars) - Flight recorder that captures black box data
- [MAM Analyzer](https://github.com/pianista215/mam-analyzer) - Analyzes flight data and generates reports

## What it does

- Reads X-Plane's `apt.dat` file (~35,000 airports with runway data)
- Queries the MAM database to find airports that don't have runway data yet
- Matches airports by ICAO code, or by coordinates (within 5 km) when ICAO doesn't match
- Generates idempotent SQL `INSERT` statements for `runway` and `runway_end` tables
- Optionally, can extract a single airport with an interactive HTML map for inspection

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) package manager
- Access to a MAM database (MariaDB/MySQL)
- X-Plane's `apt.dat` file

## Installation

### Clone and install dependencies

```bash
git clone https://github.com/pianista215/xplane-runway-extractor.git
cd xplane-runway-extractor
uv sync
```

### Copy the X-Plane data file

Copy the `apt.dat` file from your X-Plane installation into the project:

```bash
mkdir -p xplane_data
cp "/path/to/X-Plane 12/Global Scenery/Global Airports/Earth nav data/apt.dat" xplane_data/
```

This file is not included in the repository due to its size (~1 GB).

## Usage

### Generate SQL for all airports (main workflow)

> **Important:** Do not run this tool directly against a production database. Instead, export your production database to a local instance, generate the SQL locally, review it, and then apply it to production.

```bash
uv run python src/generate_runway_sql.py -H <host> -u <user> -p <password> -d <database> > runways.sql
```

**Options:**

| Flag | Description | Default |
|------|-------------|---------|
| `-H`, `--host` | Database host | `localhost` |
| `-u`, `--user` | Database user | `root` |
| `-p`, `--password` | Database password | (empty) |
| `-d`, `--database` | Database name | `mam` |
| `-P`, `--port` | Database port | `3306` |

**Example:**

```bash
# Generate SQL and save to file
uv run python src/generate_runway_sql.py -H localhost -u root -p secret -d mam > runways.sql

# Review the output
less runways.sql

# Apply to your local database
mysql -u root -p mam < runways.sql
```

The script outputs progress and a summary to stderr:

```
Found 1542 airports without runway data.
Building X-Plane airport index...
Indexed 35421 airports from X-Plane.

--- Summary ---
Matched by ICAO: 1480
Matched by coordinates: 31
  OAKB -> OAIX
  ...
Found but no land runways: 12 (HELI, ...)
Not found in X-Plane: 19 (ZZZZ, ...)
```

### Recommended workflow for production

1. **Export** the production database to a local instance:

```bash
mysqldump -h production-host -u user -p mam > mam_backup.sql
mysql -u root -p mam_local < mam_backup.sql
```

2. **Generate** the SQL against the local copy:

```bash
uv run python src/generate_runway_sql.py -H localhost -u root -p secret -d mam_local > runways.sql
```

3. **Review** the generated SQL and check the coordinate-based matches are correct.

4. **Apply** the SQL to production:

```bash
mysql -h production-host -u user -p mam < runways.sql
```

The SQL is idempotent (uses `INSERT ... WHERE NOT EXISTS`), so it's safe to run multiple times.

### Inspect a single airport

To extract and visualize runway data for a specific airport:

```bash
uv run python src/extract_airport.py LEMD
```

This prints runway details to the terminal and generates an interactive HTML map at `/tmp/LEMD_runways.html` showing:

- Runway surfaces (dark gray)
- Displaced thresholds (orange)
- Stopways/overruns (red)
- Threshold markers (green dots)

## How matching works

For each airport in the MAM database that doesn't have runway data:

1. **ICAO match**: Looks up the ICAO code directly in the X-Plane index
2. **Coordinate match**: If ICAO is not found, searches for the nearest X-Plane airport within 5 km of the database coordinates

Coordinate matches are logged in the summary so you can verify them.

## Generated SQL format

The tool generates SQL using ICAO codes and designators as identifiers (never numeric IDs), making the SQL portable across environments:

```sql
-- LEMD: Madrid Barajas
INSERT INTO runway (airport_icao, designators, width_m, length_m)
SELECT 'LEMD', '14L/32R', 60.0, 4179.0
WHERE NOT EXISTS (
    SELECT 1 FROM runway WHERE airport_icao = 'LEMD' AND designators = '14L/32R'
);

INSERT INTO runway_end (runway_id, designator, latitude, longitude, true_heading_deg, displaced_threshold_m, stopway_m)
SELECT r.id, '14L', 40.4936000, -3.5789000, 144.12, 0.0, 0.0
FROM runway r
WHERE r.airport_icao = 'LEMD' AND r.designators = '14L/32R'
AND NOT EXISTS (
    SELECT 1 FROM runway_end re WHERE re.runway_id = r.id AND re.designator = '14L'
);
```

## License

This project is licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)**.

This means:
- You can use, modify, and distribute this software
- Any derivative work must also be licensed under AGPL-3.0
- If you run a modified version as a network service, you must make the source code available to users
- See [LICENSE](LICENSE) for the full text

Copyright (c) 2026 Unai Sarasola Alvarez
