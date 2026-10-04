import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase
from django.urls import reverse

from .services import Candidate, InputError, NoFuelPlan, _lowest_fuel_cost, candidates_on_route, load_stations, optimize_fuel, resolve_location


def station(identifier, price):
    return {
        "id": str(identifier), "name": f"Station {identifier}", "address": "Test road",
        "city": "Test", "state": "TX", "latitude": 32.0, "longitude": -97.0,
        "price_per_gallon": price,
    }


class InputTests(SimpleTestCase):
    def test_resolves_city_zip_and_coordinates_without_external_geocoding(self):
        self.assertEqual(resolve_location("Chicago, IL", "start")["label"], "Chicago, IL")
        self.assertIn("Beverly Hills", resolve_location("90210", "finish")["label"])
        self.assertEqual(resolve_location({"latitude": 32.77, "longitude": -96.78}, "finish")["longitude"], -96.78)

    def test_api_rejects_non_us_location(self):
        response = self.client.post(reverse("route"), json.dumps({"start": "Toronto, ON", "finish": "Dallas, TX"}), content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_coordinate_border_check_rejects_canada(self):
        with self.assertRaises(InputError):
            resolve_location({"latitude": 43.65, "longitude": -79.38}, "start")

    def test_drf_serializer_requires_both_distinct_locations(self):
        missing = self.client.post(reverse("route"), json.dumps({"start": "Chicago, IL"}), content_type="application/json")
        self.assertEqual(missing.status_code, 400)
        self.assertIn("finish", missing.json()["details"])
        same = self.client.post(reverse("route"), json.dumps({"start": "Chicago, IL", "finish": "Chicago, IL"}), content_type="application/json")
        self.assertEqual(same.status_code, 400)
        self.assertIn("different", same.json()["error"])

    def test_drf_json_parser_rejects_other_media_types(self):
        response = self.client.post(reverse("route"), "invalid", content_type="text/plain")
        self.assertEqual(response.status_code, 415)
        self.assertIn("Unsupported media type", response.json()["error"])

    def test_map_uses_street_tiles(self):
        page = self.client.get(reverse("map"))
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"tile.openstreetmap.de", page.content)
        self.assertNotIn(b"outlineButton", page.content)


class FuelPlanningTests(SimpleTestCase):
    def test_stations_use_supplied_csv_with_median_prices(self):
        header = "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
        rows = (
            "1,Dallas A,Road,Dallas,TX,1,4.0\n"
            "1,Dallas A,Road,Dallas,TX,1,2.0\n"
            "2,Dallas B,Road,Dallas,TX,1,2.5\n"
            "3,Austin A,Road,Austin,TX,1,4.0\n"
            "3,Austin A,Road,Austin,TX,1,2.0\n"
            "4,Outside US,Road,Toronto,ON,1,1.0\n"
        )
        with TemporaryDirectory() as directory:
            csv_path = Path(directory) / "fuel-prices-for-be-assessment.csv"
            csv_path.write_text(header + rows, encoding="utf-8")
            cities = {"TX|dallas": [32.77, -96.78], "TX|austin": [30.27, -97.74]}
            with patch("planner.services.FUEL_PRICES_CSV", csv_path), patch("planner.services.load_location_index", return_value=(cities, {})):
                load_stations.cache_clear()
                self.addCleanup(load_stations.cache_clear)
                result = {station["city"]: station for station in load_stations()}
        self.assertEqual(set(result), {"Dallas", "Austin"})
        self.assertEqual(result["Dallas"]["id"], "2")
        self.assertEqual(result["Austin"]["price_per_gallon"], 3.0)

    def test_cheaper_station_is_preferred_without_exceeding_range(self):
        points = [Candidate(300, 1, station(1, 4.0)), Candidate(480, 1, station(2, 3.0)), Candidate(700, 1, station(3, 4.5))]
        stops = _lowest_fuel_cost(points, 900)
        self.assertEqual([s["station_id"] for s in stops], ["2"])
        self.assertAlmostEqual(stops[0]["gallons_purchased"], 40, places=2)

    def test_unreachable_gap_reports_no_plan(self):
        with self.assertRaises(NoFuelPlan):
            optimize_fuel([Candidate(300, 0, station(1, 3.0))], 900)

    def test_short_route_uses_starting_tank(self):
        self.assertEqual(optimize_fuel([], 200), [])

    def test_station_just_off_route_is_found(self):
        route = {"distance_miles": 100, "geometry": {"type": "LineString", "coordinates": [[-97.0, 32.0], [-95.5, 32.0]]}}
        with patch("planner.services.load_stations", return_value=(station(1, 3.0),)):
            result = candidates_on_route(route)
        self.assertEqual(len(result), 0)  # At the start, so no refueling point.
        nearby = station(2, 3.0) | {"longitude": -96.2, "latitude": 32.05}
        with patch("planner.services.load_stations", return_value=(nearby,)):
            result = candidates_on_route(route)
        self.assertEqual(len(result), 1)
        self.assertLess(result[0].offset_miles, 5)

    def test_stations_near_start_and_finish_are_candidates(self):
        route = {"distance_miles": 100, "geometry": {"type": "LineString", "coordinates": [[-97.0, 32.0], [-95.5, 32.0]]}}
        near_start = station(3, 3.0) | {"longitude": -96.995}
        near_finish = station(4, 3.0) | {"longitude": -95.53}
        with patch("planner.services.load_stations", return_value=(near_start, near_finish)):
            result = candidates_on_route(route)
        self.assertEqual([candidate.station["id"] for candidate in result], ["3", "4"])
        self.assertLess(result[0].mile, 1)
        self.assertGreater(result[1].mile, 95)

    @patch("planner.views.make_plan")
    def test_api_returns_geojson_and_cost(self, make_plan):
        make_plan.return_value = {"total_fuel_cost_usd": 40.0, "map": {"type": "FeatureCollection", "features": []}}
        response = self.client.post(reverse("route"), json.dumps({"start": "Chicago, IL", "finish": "Dallas, TX"}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["map"]["type"], "FeatureCollection")
        self.assertEqual(response.json()["total_fuel_cost_usd"], 40.0)
