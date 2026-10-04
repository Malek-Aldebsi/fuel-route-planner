"""Build offline US city and ZIP indexes from GeoNames."""

import csv
import io
import json
import re
import statistics
import sys
import unicodedata
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "planner" / "data"
GEO_URL = "https://download.geonames.org/export/zip/US.zip"
STATES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split())


def normalize_city_name(value):
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    value = re.sub(r"\bsaint\b", "st", value)
    value = re.sub(r"\bfort\b", "ft", value)
    return re.sub(r"[^a-z0-9]", "", value)


def main(zip_path):
    places = defaultdict(list)
    zip_codes = {}
    with zipfile.ZipFile(zip_path) as archive:
        with archive.open("US.txt") as raw:
            for row in csv.reader(io.TextIOWrapper(raw, encoding="utf-8"), delimiter="\t"):
                if len(row) < 12 or row[0] != "US" or row[4] not in STATES:
                    continue
                try:
                    latitude, longitude = float(row[9]), float(row[10])
                except ValueError:
                    continue
                if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                    continue
                record = [latitude, longitude]
                places[f"{row[4]}|{normalize_city_name(row[2])}"].append(record)
                zip_codes[row[1]] = [row[2], row[4], latitude, longitude]

    # The median of all ZIP centroids is a stable approximate city position.
    cities = {
        place: [round(statistics.median(p[0] for p in points), 5),
                round(statistics.median(p[1] for p in points), 5)]
        for place, points in places.items()
    }
    OUTPUT.mkdir(exist_ok=True)
    (OUTPUT / "cities.json").write_text(json.dumps(cities, separators=(",", ":")), encoding="utf-8")
    (OUTPUT / "zip_codes.json").write_text(json.dumps(zip_codes, separators=(",", ":")), encoding="utf-8")
    print(f"Indexed {len(cities):,} US places and {len(zip_codes):,} ZIP codes")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(Path(sys.argv[1]))
    else:
        request = urllib.request.Request(GEO_URL, headers={"User-Agent": "spotter-assessment/1.0 (offline-index-build)"})
        with urllib.request.urlopen(request, timeout=30) as response:
            archive_path = ROOT / "US.zip"
            archive_path.write_bytes(response.read())
        try:
            main(archive_path)
        finally:
            archive_path.unlink()
