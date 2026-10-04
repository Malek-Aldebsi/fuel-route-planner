"""Extract the US border from Natural Earth's public-domain 10m data."""

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_admin_0_countries.geojson"
OUTPUT = ROOT / "planner" / "data" / "us_boundary.json"


def main():
    if len(sys.argv) > 1:
        source = Path(sys.argv[1]).read_bytes()
    else:
        request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "SpotterFuelPlannerAssessment/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response:
            source = response.read()
    countries = json.loads(source)
    usa = next(feature for feature in countries["features"] if feature["properties"].get("ISO_A3") == "USA")
    OUTPUT.write_text(json.dumps(usa["geometry"], separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {OUTPUT} ({OUTPUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
