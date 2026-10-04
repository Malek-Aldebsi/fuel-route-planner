import csv
import json
import hashlib
import math
import os
import re
import statistics
import time
import unicodedata
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from django.core.cache import cache

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "planner" / "data"
FUEL_PRICES_CSV = DATA / "fuel-prices-for-be-assessment.csv"
MILES_PER_METER = 1 / 1609.344
MAX_RANGE_MILES = 500
MILES_PER_GALLON = 10
MAX_CITY_DISTANCE_MILES = 15.0
STOP_INCONVENIENCE_USD = 3.0
STATES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split())


class InputError(Exception):
    # bad ZIP, bad city, invalid coordinates
    pass


class RoutingError(Exception):
    # OSRM failed or no route exists
    pass


class NoFuelPlan(Exception):
    # route exists, but no valid fuel stop strategy exists
    pass


def normalize_city_name(value):
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    value = re.sub(r"\bsaint\b", "st", value)
    value = re.sub(r"\bfort\b", "ft", value)
    return re.sub(r"[^a-z0-9]", "", value)


@lru_cache(maxsize=1)
def load_location_index():
    return (
        json.loads((DATA / "cities.json").read_text(encoding="utf-8")),
        json.loads((DATA / "zip_codes.json").read_text(encoding="utf-8")),
    )


@lru_cache(maxsize=1)
def load_stations():
    cities, _ = load_location_index()
    groups = defaultdict(list)
    with FUEL_PRICES_CSV.open(newline="", encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            state = row["State"].strip().upper()
            city = row["City"].strip()
            place = f"{state}|{normalize_city_name(city)}"
            if place in cities:
                groups[row["OPIS Truckstop ID"].strip()].append(row)

    # Stations in one city have the same approximate position, so keep the
    # cheapest station there after merging duplicate IDs by median price.
    choices = {}
    for station_id, rows in groups.items():
        row = rows[0]
        state = row["State"].strip().upper()
        city = row["City"].strip()
        place = f"{state}|{normalize_city_name(city)}"
        latitude, longitude = cities[place]
        price = round(statistics.median(float(item["Retail Price"]) for item in rows), 5)
        station = {
            "id": station_id,
            "name": row["Truckstop Name"].strip(),
            "address": row["Address"].strip(),
            "city": city,
            "state": state,
            "latitude": latitude,
            "longitude": longitude,
            "price_per_gallon": price,
        }
        if place not in choices or price < choices[place]["price_per_gallon"]:
            choices[place] = station
    return tuple(choices.values())


def _inside_us_bbox(lat, lon):
    """Check rough bounds for the lower 48 states, Alaska, and Hawaii.

    These boxes also include ocean and neighboring country locations.
    _inside_us() checks the US polygons before accepting a point.
    """
    return (
        (24 <= lat <= 50 and -125 <= lon <= -66)
        or (51 <= lat <= 72 and -180 <= lon <= -129)
        or (18 <= lat <= 23 and -162 <= lon <= -154)
    )


@lru_cache(maxsize=1)
def _us_polygons():
    geometry = json.loads((DATA / "us_boundary.json").read_text(encoding="utf-8"))
    polygons = []
    for rings in geometry["coordinates"]:
        outer = rings[0]
        bounds = (min(p[0] for p in outer), min(p[1] for p in outer), max(p[0] for p in outer), max(p[1] for p in outer))
        polygons.append((bounds, rings))
    return polygons


def _point_in_ring(lon, lat, ring):
    """Check if a point is inside a polygon by counting edge crossings."""
    inside = False
    previous = ring[-1]
    for current in ring:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
        previous = current
    return inside


def _inside_us(lat, lon):
    if not _inside_us_bbox(lat, lon):
        return False
    for (left, bottom, right, top), rings in _us_polygons():
        if left <= lon <= right and bottom <= lat <= top and _point_in_ring(lon, lat, rings[0]):
            return not any(_point_in_ring(lon, lat, hole) for hole in rings[1:])
    return False


def resolve_location(value, field):
    """Accept city/state, 5 digit ZIP or {latitude, longitude}."""
    cities, zip_codes = load_location_index()
    if isinstance(value, str):
        value = value.strip()
        if re.fullmatch(r"\d{5}", value):
            found = zip_codes.get(value)
            if found is None:
                raise InputError(f"{field}: unknown US ZIP code")
            return {"label": f"{found[0]}, {found[1]} {value}", "latitude": found[2], "longitude": found[3]}
        parts = [part.strip() for part in value.rsplit(",", 1)]
        if len(parts) != 2 or parts[1].upper() not in STATES:
            raise InputError(f"{field}: use 'City, ST', a five digit ZIP, or coordinates")
        value = {"city": parts[0], "state": parts[1]}

    if not isinstance(value, dict):
        raise InputError(f"{field}: invalid location")
    if "latitude" in value or "longitude" in value:
        try:
            lat = float(value["latitude"])
            lon = float(value["longitude"])
        except (KeyError, TypeError, ValueError):
            raise InputError(f"{field}: latitude and longitude must be numbers") from None
        if not math.isfinite(lat) or not math.isfinite(lon) or not _inside_us(lat, lon):
            raise InputError(f"{field}: coordinates must be within the USA")
        return {"label": f"{lat:.5f}, {lon:.5f}", "latitude": lat, "longitude": lon}

    city = value.get("city")
    state = value.get("state")
    if not isinstance(city, str) or not isinstance(state, str) or state.strip().upper() not in STATES:
        raise InputError(f"{field}: use a US city and two letter state")
    place = cities.get(f"{state.strip().upper()}|{normalize_city_name(city.strip())}")
    if place is None:
        raise InputError(f"{field}: city not found in the US location index")
    return {"label": f"{city.strip()}, {state.strip().upper()}", "latitude": place[0], "longitude": place[1]}


def _decode_polyline6(encoded):
    """Decode OSRM's compact polyline6 into GeoJSON [longitude, latitude] pairs."""
    values = []
    index = 0
    latitude = longitude = 0
    while index < len(encoded):
        pair = []
        for _ in range(2):
            result = shift = 0
            while True:
                if index >= len(encoded):
                    raise RoutingError("Routing provider returned invalid geometry")
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            pair.append((result >> 1) ^ -(result & 1))
        latitude += pair[0]
        longitude += pair[1]
        values.append([longitude / 1_000_000, latitude / 1_000_000])
    return values


def _routing_provider():
    base_url = os.environ.get("OSRM_BASE_URL", "https://routing.openstreetmap.de/routed-car").rstrip("/")
    return base_url, hashlib.sha256(base_url.encode()).hexdigest()[:12]


def route_between(start, finish):
    """A cache miss makes exactly one OSRM Route request."""
    base_url, provider_key = _routing_provider()
    cache_key = f"route:{provider_key}:" + ":".join(f"{p['latitude']:.5f},{p['longitude']:.5f}" for p in (start, finish))
    cached = cache.get(cache_key)
    if cached is not None:
        return cached, True

    coordinates = ";".join(f"{p['longitude']:.6f},{p['latitude']:.6f}" for p in (start, finish))
    url = f"{base_url}/route/v1/driving/{coordinates}?overview=full&geometries=polyline6&steps=false"
    request = urllib.request.Request(url, headers={"User-Agent": os.environ.get("ROUTING_USER_AGENT", "SpotterFuelPlannerAssessment/1.0")})
    # The public service allows at most one request per second across the whole
    # app. The file lock coordinates local worker processes and also prevents
    # concurrent misses for the same route from duplicating requests.
    lock_path = ROOT / ".osrm_rate_limit.lock"
    with lock_path.open("a+", encoding="ascii") as lock_file:
        if os.name == "nt":
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
        cached = cache.get(cache_key)
        if cached is not None:
            return cached, True
        lock_file.seek(0)
        try:
            last_request = float(lock_file.read() or "0")
        except ValueError:
            last_request = 0.0
        delay = last_request + 1.0 - time.time()
        if delay > 0:
            time.sleep(delay)
        lock_file.seek(0)
        lock_file.write(str(time.time()))
        lock_file.truncate()
        lock_file.flush()
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.status != 200:
                    raise RoutingError("Routing provider returned an unsuccessful response")
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise RoutingError("Routing provider is unavailable; please retry") from exc
        if payload.get("code") != "Ok" or not payload.get("routes"):
            raise RoutingError("No drivable route was found between these locations")
        route = payload["routes"][0]
        if route.get("distance", 0) <= 0 or not isinstance(route.get("geometry"), str):
            raise RoutingError("Routing provider returned an invalid route")
        result = {"distance_miles": route["distance"] * MILES_PER_METER, "duration_seconds": route["duration"], "geometry": {"type": "LineString", "coordinates": _decode_polyline6(route["geometry"])}}
        cache.set(cache_key, result, 86400)
        return result, False


def _haversine(a, b):
    """Return the great circle distance in miles between two [lon, lat] points."""
    lon1, lat1 = a
    lon2, lat2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = p2 - p1, math.radians(lon2 - lon1)
    x = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 3958.7613 * 2 * math.asin(min(1, math.sqrt(x)))


def _project(point, a, b):
    """Approximate closest point on a short route segment, in miles."""
    cos_lat = math.cos(math.radians((a[1] + b[1] + point[1]) / 3))
    x = (b[0] - a[0]) * 69.172 * cos_lat
    y = (b[1] - a[1]) * 69.0
    px = (point[0] - a[0]) * 69.172 * cos_lat
    py = (point[1] - a[1]) * 69.0
    length_sq = x * x + y * y
    fraction = max(0.0, min(1.0, (px * x + py * y) / length_sq)) if length_sq else 0.0
    return math.hypot(px - fraction * x, py - fraction * y), fraction


def _simplify_geometry(coordinates, tolerance_miles=0.2):
    """Reduce GeoJSON size without changing the full route used for planning."""
    if len(coordinates) <= 2:
        return coordinates
    selected = {0, len(coordinates) - 1}
    pending = [(0, len(coordinates) - 1)]
    while pending:
        left, right = pending.pop()
        largest = 0.0
        index = None
        for i in range(left + 1, right):
            offset, _ = _project(coordinates[i], coordinates[left], coordinates[right])
            if offset > largest:
                largest, index = offset, i
        if largest > tolerance_miles and index is not None:
            selected.add(index)
            pending.extend([(left, index), (index, right)])
    return [coordinates[i] for i in sorted(selected)]


@dataclass
class Candidate:
    mile: float
    offset_miles: float
    station: dict


def candidates_on_route(route, max_offset=MAX_CITY_DISTANCE_MILES):
    coords = route["geometry"]["coordinates"]
    if len(coords) < 2:
        raise RoutingError("Routing provider returned incomplete geometry")
    lengths = [_haversine(a, b) for a, b in zip(coords, coords[1:])]
    total_geometry = sum(lengths)
    if total_geometry <= 0:
        raise RoutingError("Routing provider returned incomplete geometry")
    cumulative = [0.0]
    for length in lengths:
        cumulative.append(cumulative[-1] + length)

    # Group route segments into half degree cells.
    grid = {}
    cell_size = 0.5
    for i, (a, b) in enumerate(zip(coords, coords[1:])):
        for gx in range(math.floor((min(a[0], b[0]) - 0.75) / cell_size), math.floor((max(a[0], b[0]) + 0.75) / cell_size) + 1):
            for gy in range(math.floor((min(a[1], b[1]) - 0.28) / cell_size), math.floor((max(a[1], b[1]) + 0.28) / cell_size) + 1):
                grid.setdefault((gx, gy), []).append(i)
    result = []
    scale = route["distance_miles"] / total_geometry
    for station in load_stations():
        lon, lat = station["longitude"], station["latitude"]
        nearby = grid.get((math.floor(lon / cell_size), math.floor(lat / cell_size)), ())
        if not nearby:
            continue
        point = (lon, lat)
        closest = (float("inf"), 0.0)
        for i in nearby:
            a, b = coords[i], coords[i + 1]
            if lat < min(a[1], b[1]) - 0.28 or lat > max(a[1], b[1]) + 0.28:
                continue
            if lon < min(a[0], b[0]) - 0.75 or lon > max(a[0], b[0]) + 0.75:
                continue
            offset, fraction = _project(point, a, b)
            if offset < closest[0]:
                closest = (offset, (cumulative[i] + fraction * lengths[i]) * scale)
        if closest[0] <= max_offset and 0 < closest[1] < route["distance_miles"]:
            result.append(Candidate(closest[1], closest[0], station))
    return sorted(result, key=lambda item: (item.mile, item.station["price_per_gallon"]))


def _lowest_fuel_cost(candidates, distance_miles):
    """Find the cheapest fuel purchases on a route, starting with a full tank.

    The strategy is if a cheaper station is reachable, buy only enough fuel to reach it.
    Otherwise, fill enough for the maximum useful range.
    """
    points = [Candidate(0.0, 0.0, {"price_per_gallon": 0.0})] + candidates + [Candidate(distance_miles, 0.0, {"price_per_gallon": 0.0})]
    for left, right in zip(points, points[1:]):
        if right.mile - left.mile > MAX_RANGE_MILES + 1e-7:
            raise NoFuelPlan("No listed fuel stop keeps every leg within the 500 mile range")

    remaining = MAX_RANGE_MILES
    previous_mile = 0.0
    stops = []
    for index, candidate in enumerate(candidates):
        remaining -= candidate.mile - previous_mile
        if remaining < -1e-6:
            raise NoFuelPlan("No listed fuel stop is reachable with the remaining fuel")
        remaining = max(0.0, remaining)
        previous_mile = candidate.mile
        cheaper_mile = None
        for future in candidates[index + 1:]:
            if future.mile - candidate.mile > MAX_RANGE_MILES:
                break
            if future.station["price_per_gallon"] < candidate.station["price_per_gallon"]:
                cheaper_mile = future.mile
                break
        target = min(MAX_RANGE_MILES, distance_miles - candidate.mile)
        if cheaper_mile is not None:
            target = min(target, cheaper_mile - candidate.mile)
        gallons = max(0.0, (target - remaining) / MILES_PER_GALLON)
        if gallons > 1e-7:
            remaining += gallons * MILES_PER_GALLON
            price = Decimal(str(candidate.station["price_per_gallon"]))
            cost = (price * Decimal(str(gallons))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            stops.append({
                "station_id": candidate.station["id"],
                "name": candidate.station["name"],
                "address": candidate.station["address"],
                "city": candidate.station["city"],
                "state": candidate.station["state"],
                "latitude": candidate.station["latitude"],
                "longitude": candidate.station["longitude"],
                "location_precision": "city centroid",
                "mile_marker": round(candidate.mile, 1),
                "estimated_distance_from_route_miles": round(candidate.offset_miles, 1),
                "price_per_gallon_usd": float(price),
                "gallons_purchased": round(gallons, 3),
                "cost_usd": float(cost),
            })

    if remaining + 1e-6 < distance_miles - previous_mile:
        raise NoFuelPlan("The destination is beyond the remaining vehicle range")
    return stops


def optimize_fuel(candidates, distance_miles):
    """Optimize the fuel plan by balancing fuel cost against the number of stops.

    Each stop is assigned a $3 inconvenience penalty when comparing
    alternative plans, so a slightly higher fuel cost may be preferred
    if it reduces the number of refueling stops.
    """
    available = list(candidates)
    stops = _lowest_fuel_cost(available, distance_miles)
    while len(stops) > 1:
        baseline = sum(stop["cost_usd"] for stop in stops) + STOP_INCONVENIENCE_USD * len(stops)
        best = None
        for stop in stops:
            trial = [candidate for candidate in available if candidate.station["id"] != stop["station_id"]]
            try:
                trial_stops = _lowest_fuel_cost(trial, distance_miles)
            except NoFuelPlan:
                continue
            score = sum(item["cost_usd"] for item in trial_stops) + STOP_INCONVENIENCE_USD * len(trial_stops)
            if score < baseline - 0.01 and (best is None or score < best[0]):
                best = (score, trial, trial_stops)
        if best is None:
            break
        _, available, stops = best
    return stops


def make_plan(start, finish):
    """Orchestrate route retrieval, fuel stop optimization, map data creation, and plan caching."""
    _, provider_key = _routing_provider()
    plan_key = f"plan:{provider_key}:" + ":".join(f"{p['latitude']:.5f},{p['longitude']:.5f}" for p in (start, finish))
    cached_plan = cache.get(plan_key)
    if cached_plan is not None:
        cached_plan["start"] = start
        cached_plan["finish"] = finish
        cached_plan["routing"] = {"requests_this_call": 0, "cache_hit": True}
        return cached_plan
    route, cached = route_between(start, finish)
    candidates = candidates_on_route(route)
    stops = optimize_fuel(candidates, route["distance_miles"])
    map_geometry = {"type": "LineString", "coordinates": _simplify_geometry(route["geometry"]["coordinates"])}
    features = [{"type": "Feature", "properties": {"kind": "route"}, "geometry": map_geometry}]
    for stop in stops:
        features.append({"type": "Feature", "properties": {"kind": "fuel_stop", "station_id": stop["station_id"], "price_per_gallon_usd": stop["price_per_gallon_usd"]}, "geometry": {"type": "Point", "coordinates": [stop["longitude"], stop["latitude"]]}})
    plan = {
        "start": start,
        "finish": finish,
        "distance_miles": round(route["distance_miles"], 1),
        "duration_hours": round(route["duration_seconds"] / 3600, 1),
        "vehicle": {
            "max_range_miles": MAX_RANGE_MILES,
            "miles_per_gallon": MILES_PER_GALLON,
            "initial_tank": f"full ({MAX_RANGE_MILES / MILES_PER_GALLON:g} gallons)",
        },
        "fuel_used_gallons": round(route["distance_miles"] / MILES_PER_GALLON, 2),
        "fuel_purchased_gallons": round(sum(s["gallons_purchased"] for s in stops), 3),
        "total_fuel_cost_usd": round(sum(s["cost_usd"] for s in stops), 2),
        "fuel_stops": stops,
        "map": {"type": "FeatureCollection", "features": features},
        "routing": {"requests_this_call": 0 if cached else 1, "cache_hit": cached},
    }
    cache.set(plan_key, plan, 86400)
    return plan
