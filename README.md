# Fuel Route Planner

A Django REST Framework API for planning fuel stops on US road trips. Give it a start and finish location, and it returns a driving route, suggested fuel stops, and the cost of fuel bought during the trip.

## Run locally

Use Python 3.12 or newer:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver
```

Open http://127.0.0.1:8000/map/ to use the map in your browser.

## Try the API

Send a JSON `POST` request to `http://127.0.0.1:8000/api/route/`:

```json
{"start": "Chicago, IL", "finish": "Dallas, TX"}
```

Set `Content-Type: application/json` in Postman. Each location can be a `"City, ST"` string, a five-digit ZIP code, `{"city":"Chicago","state":"IL"}`, or `{"latitude":41.88,"longitude":-87.63}`. Street addresses are not supported.

The response includes the route as GeoJSON, ordered fuel stops with prices and amounts to buy, trip distance, and total fuel cost. The `/map/` page draws the same response.

## How the plan works

The vehicle starts with a full 50-gallon tank, has a 500-mile range, and gets 10 miles per gallon. The reported cost covers fuel bought at stops; it does not include fuel already in the tank. The planner compares prices along the route and uses a $3 preference per stop to avoid extra stops for small savings. That $3 is never added to the fuel total.

Fuel prices come from the supplied CSV at `planner/data/fuel-prices-for-be-assessment.csv`. It has no station coordinates, so the map uses approximate city positions. The planner considers cities within 15 straight-line miles of the route. Check a station's address and current price before driving there; detours are not included in the fuel calculation.

A new route needs one OSRM routing request. Routes and completed plans are cached for 24 hours, so repeating a request needs no routing call. City and ZIP lookups use the bundled data files and make no geocoding requests.

- Map data: [OpenStreetMap](https://www.openstreetmap.org/copyright).
- City coordinates: [GeoNames US postal data](https://www.geonames.org/export/zip/) ([CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)).
- US boundary: [Natural Earth](https://www.naturalearthdata.com/downloads/10m-cultural-vectors/) (public domain).
- Routing service: [FOSSGIS](https://routing.openstreetmap.de/about.html).

## Tests

```bash
.venv/bin/python manage.py test
```

The tests run without calling the routing service.
