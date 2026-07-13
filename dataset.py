"""
Route geometry + physics baseline extractor — DISTANCE-CORRECTED VERSION

Fix vs. previous version: OSRM was being called with only a start and end
coordinate, which leaves it free to solve for *any* drivable path between
those two points. On the public OSRM demo server, that produced a route
390.9 km long for Manali-Leh, when the real NH3 highway via the Atal Tunnel
is documented at ~427-430 km. The gap was consistent and too clean to be
route-variant noise -- almost certainly OSRM cutting a corner somewhere
because of a routing-graph gap or an alternate path it preferred.

The fix: force the route through the real highway's known waypoints
(Sissu -> Keylong -> Jispa -> Darcha -> Baralacha La -> Sarchu -> Pang ->
Tanglang La -> Upshi) by passing them all to OSRM in one /route call. OSRM
still finds the real road-following path between each consecutive pair --
we're not straight-lining between waypoints, just preventing it from
skipping the actual highway. The script also now prints the per-leg
distance between each waypoint so you can see exactly where things stand
(and immediately spot a leg that's suspiciously short, which would flag a
data gap in that specific stretch instead of a mystery 40km deficit
somewhere in a 400km route).

Only running manali_leh for now, per your call -- confirm this route is
right before spending the API budget on spiti_circuit too.

Requires: requests
    pip install requests

Run this on your own machine (needs internet access to OSRM + Open Topo Data).
"""

import time
import math
import csv
import requests

# ---------------------------------------------------------------------------
# CONFIG — edit this per route
# ---------------------------------------------------------------------------

# Each route is now a WAYPOINT CHAIN, not just start/end. OSRM will be asked
# to route through all of these in order, on the real road network, so the
# geometry can't silently skip the actual highway.
ROUTES = [
    {
        "route_id": "manali_leh",
        "route_name": "Manali to Leh",
        "waypoints": [
            ("manali",       32.2432, 77.1892),
            ("sissu",        32.4802, 77.1244),   # north portal, Atal Tunnel
            ("keylong",      32.5710, 77.0320),
            ("jispa",        32.6390, 77.1852),
            ("darcha",       32.6780, 77.1950),
            ("baralacha_la", 32.7585, 77.4200),
            ("sarchu",       32.9070, 77.5813),
            ("pang",         33.1295, 77.7863),
            ("tanglang_la",  33.5078, 77.7699),
            ("upshi",        33.8304, 77.8142),
            ("leh",          34.1526, 77.5771),
        ],
    },
    # spiti_circuit deliberately left out — confirm manali_leh distance is
    # right first, then add its own waypoint chain the same way before
    # re-enabling it. Re-verify against a real source before trusting it;
    # 377.2 km hasn't been checked against a reference yet.
    # {
    #     "route_id": "spiti_circuit",
    #     "route_name": "Shimla to Kaza (Spiti)",
    #     "waypoints": [
    #         ("shimla", 31.1049, 77.1734),
    #         ("kaza",   32.2277, 78.0720),
    #     ],
    # },
]

# Reference distance to sanity-check against (km). Manali-Leh via Atal
# Tunnel is documented at ~427-430 km across multiple independent sources.
EXPECTED_DISTANCE_KM = {
    "manali_leh": (420, 435),
}

TARGET_SPACING_M = 300          # distance between sampled points, in meters
OPENTOPO_BATCH_SIZE = 100       # max coords per Open Topo Data /path call
OPENTOPO_RATE_LIMIT_SEC = 1.05  # stay under 1 req/sec free-tier limit

OSRM_BASE = "https://router.project-osrm.org"
OPENTOPO_BASE = "https://api.opentopodata.org/v1/srtm90m"  # or aster30m

VEHICLE = {
    "vehicle_class": "loaded_touring_bike",
    "vehicle_mass_kg": 220,       # bike + rider + luggage
    "Crr": 0.02,                  # rolling resistance, broken mountain road
    "Cd_A": 0.6,                  # drag coefficient * frontal area (m^2)
    "thermal_efficiency": 0.27,   # ICE thermal efficiency
    "fuel_calorific_value_MJ_per_L": 32.0,  # petrol, per liter (~44 MJ/kg * ~0.74 kg/L)
    "flat_speed_kmh": 40.0,       # placeholder until gradient-limited speed model is added
}

G = 9.81


# ---------------------------------------------------------------------------
# Step 1 — OSRM: get the actual road-following path through ALL waypoints
# ---------------------------------------------------------------------------

def get_osrm_route(waypoints):
    """
    waypoints: list of (name, lat, lon) tuples, in travel order.
    Returns (path_points, leg_distances_km) where path_points is the full
    road-following polyline across every leg, and leg_distances_km is a
    list of (name_from, name_to, distance_km) for diagnostics -- this is
    what lets you see if any single leg is suspiciously short instead of
    just staring at one mystery total.
    """
    coord_str = ";".join(f"{lon},{lat}" for _, lat, lon in waypoints)
    url = f"{OSRM_BASE}/route/v1/driving/{coord_str}"
    params = {"overview": "full", "geometries": "geojson", "steps": "false"}
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "Ok":
        raise RuntimeError(f"OSRM error: {data}")

    route = data["routes"][0]
    coords = route["geometry"]["coordinates"]  # [lon, lat] pairs, whole route
    path_points = [(lat, lon) for lon, lat in coords]

    leg_distances_km = []
    for i, leg in enumerate(route["legs"]):
        name_from = waypoints[i][0]
        name_to = waypoints[i + 1][0]
        leg_distances_km.append((name_from, name_to, leg["distance"] / 1000.0))

    return path_points, leg_distances_km


# ---------------------------------------------------------------------------
# Step 2 — Haversine distance between two points
# ---------------------------------------------------------------------------

def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Step 3 — Resample the OSRM polyline at fixed arc-length intervals
# ---------------------------------------------------------------------------

def resample_by_distance(path_points, spacing_m):
    """
    Walk the dense OSRM polyline and emit a new point every `spacing_m` meters
    of cumulative arc length, interpolating linearly between the original
    vertices when a target distance falls between two of them.
    """
    if len(path_points) < 2:
        return path_points

    resampled = [path_points[0]]
    next_target = spacing_m
    cum_dist = 0.0

    for i in range(1, len(path_points)):
        lat1, lon1 = path_points[i - 1]
        lat2, lon2 = path_points[i]
        seg_len = haversine_m(lat1, lon1, lat2, lon2)

        if seg_len == 0:
            continue

        while cum_dist + seg_len >= next_target:
            frac = (next_target - cum_dist) / seg_len
            lat = lat1 + frac * (lat2 - lat1)
            lon = lon1 + frac * (lon2 - lon1)
            resampled.append((lat, lon))
            next_target += spacing_m

        cum_dist += seg_len

    if resampled[-1] != path_points[-1]:
        resampled.append(path_points[-1])

    return resampled


# ---------------------------------------------------------------------------
# Step 4 — Open Topo Data: elevation for each resampled point, batched
# ---------------------------------------------------------------------------

def get_elevations(points, batch_size=OPENTOPO_BATCH_SIZE):
    elevations = []
    for i in range(0, len(points), batch_size):
        batch = points[i:i + batch_size]
        locations = "|".join(f"{lat},{lon}" for lat, lon in batch)
        r = requests.get(OPENTOPO_BASE, params={"locations": locations}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "OK":
            raise RuntimeError(f"Open Topo Data error: {data}")
        elevations.extend(res["elevation"] for res in data["results"])
        time.sleep(OPENTOPO_RATE_LIMIT_SEC)
    return elevations


# ---------------------------------------------------------------------------
# Step 4b — smooth the elevation profile before differencing
# ---------------------------------------------------------------------------

SMOOTHING_WINDOW = 5       # total window size
NOISE_FLOOR_M = 3.0        # deltas smaller than this (after smoothing) are treated as flat


def smooth_elevations(elevations, window=SMOOTHING_WINDOW):
    """Centered moving average, shrinking the window at the ends of the route."""
    n = len(elevations)
    half = window // 2
    smoothed = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        window_vals = elevations[lo:hi]
        smoothed.append(sum(window_vals) / len(window_vals))
    return smoothed


# ---------------------------------------------------------------------------
# Step 5 — build_route_geometry: derivative/integration logic
# ---------------------------------------------------------------------------

def build_route_geometry(route_id, points, elevations):
    rows = []
    cum_distance_km = 0.0
    cum_gain_m = 0.0
    cum_loss_m = 0.0

    smoothed_elevations = smooth_elevations(elevations)

    for i, ((lat, lon), elev, smoothed_elev) in enumerate(zip(points, elevations, smoothed_elevations)):
        if i == 0:
            segment_length_m = 0.0
            delta_elevation_m = 0.0
            gradient_percent = 0.0
        else:
            prev_lat, prev_lon = points[i - 1]
            prev_smoothed_elev = smoothed_elevations[i - 1]
            segment_length_m = haversine_m(prev_lat, prev_lon, lat, lon)
            raw_delta = smoothed_elev - prev_smoothed_elev

            delta_elevation_m = raw_delta if abs(raw_delta) > NOISE_FLOOR_M else 0.0

            cum_distance_km += segment_length_m / 1000.0

            gradient_percent = (delta_elevation_m / segment_length_m * 100.0) if segment_length_m > 0 else 0.0

            if delta_elevation_m > 0:
                cum_gain_m += delta_elevation_m
            else:
                cum_loss_m += -delta_elevation_m

        rows.append({
            "route_id": route_id,
            "point_index": i,
            "lat": lat,
            "lon": lon,
            "cum_distance_km": round(cum_distance_km, 3),
            "elevation_m_raw": elev,
            "elevation_m_smoothed": round(smoothed_elev, 1),
            "segment_length_m": round(segment_length_m, 1),
            "delta_elevation_m": round(delta_elevation_m, 1),
            "gradient_percent": round(gradient_percent, 2),
            "cum_elevation_gain_m": round(cum_gain_m, 1),
            "cum_elevation_loss_m": round(cum_loss_m, 1),
        })

    return rows


# ---------------------------------------------------------------------------
# Step 6 — physics_baseline: energy balance -> fuel, placeholder time
# ---------------------------------------------------------------------------

def air_density_ratio(altitude_m):
    return math.exp(-altitude_m / 8500.0)


def physics_baseline(route_id, route_name, geometry_rows, vehicle):
    total_distance_km = geometry_rows[-1]["cum_distance_km"]
    total_gain_m = geometry_rows[-1]["cum_elevation_gain_m"]
    max_altitude_m = max(r["elevation_m_smoothed"] for r in geometry_rows)
    mean_altitude_m = sum(r["elevation_m_smoothed"] for r in geometry_rows) / len(geometry_rows)

    m = vehicle["vehicle_mass_kg"]

    E_pe_J = m * G * total_gain_m
    E_roll_J = vehicle["Crr"] * m * G * (total_distance_km * 1000)

    v = vehicle["flat_speed_kmh"] / 3.6
    rho_ratio = air_density_ratio(mean_altitude_m)
    rho_sea_level = 1.225
    rho = rho_sea_level * rho_ratio
    E_aero_J = 0.5 * rho * vehicle["Cd_A"] * v ** 2 * (total_distance_km * 1000)

    E_total_J = E_pe_J + E_roll_J + E_aero_J
    E_total_MJ = E_total_J / 1e6

    derating_factor = 1.0 + 0.04 * (mean_altitude_m / 300.0) * 0.15  # damped placeholder
    E_effective_MJ = E_total_MJ * derating_factor

    fuel_L = E_effective_MJ / (vehicle["thermal_efficiency"] * vehicle["fuel_calorific_value_MJ_per_L"])

    baseline_time_hr = total_distance_km / vehicle["flat_speed_kmh"]

    return {
        "route_id": route_id,
        "route_name": route_name,
        "vehicle_class": vehicle["vehicle_class"],
        "vehicle_mass_kg": m,
        "total_distance_km": round(total_distance_km, 1),
        "total_elevation_gain_m": round(total_gain_m, 1),
        "max_altitude_m": round(max_altitude_m, 1),
        "mean_air_density_ratio": round(rho_ratio, 3),
        "baseline_energy_MJ": round(E_effective_MJ, 1),
        "baseline_fuel_L": round(fuel_L, 2),
        "baseline_time_hr": round(baseline_time_hr, 1),
    }


# ---------------------------------------------------------------------------
# Step 7 — write CSVs
# ---------------------------------------------------------------------------

def write_geometry_csv(all_geometry_rows, path):
    fieldnames = ["route_id", "point_index", "lat", "lon", "cum_distance_km",
                  "elevation_m_raw", "elevation_m_smoothed", "segment_length_m", "delta_elevation_m",
                  "gradient_percent", "cum_elevation_gain_m", "cum_elevation_loss_m"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_geometry_rows)


def write_baseline_csv(all_baseline_rows, path):
    fieldnames = ["route_id", "route_name", "vehicle_class", "vehicle_mass_kg",
                  "total_distance_km", "total_elevation_gain_m", "max_altitude_m",
                  "mean_air_density_ratio", "baseline_energy_MJ",
                  "baseline_fuel_L", "baseline_time_hr"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_baseline_rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    all_geometry_rows = []
    all_baseline_rows = []

    for route in ROUTES:
        route_id = route["route_id"]
        print(f"--- {route_id} ---")

        print(f"  fetching OSRM road path through {len(route['waypoints'])} waypoints...")
        osrm_path, leg_distances_km = get_osrm_route(route["waypoints"])
        print(f"  OSRM returned {len(osrm_path)} raw vertices")

        print("  per-leg distances (check for any suspiciously short leg):")
        leg_total = 0.0
        for name_from, name_to, dist_km in leg_distances_km:
            print(f"    {name_from:>12} -> {name_to:<12} {dist_km:6.1f} km")
            leg_total += dist_km
        print(f"    {'TOTAL':>12}    {'':<12} {leg_total:6.1f} km")

        print(f"  resampling every {TARGET_SPACING_M} m along the road path...")
        resampled_points = resample_by_distance(osrm_path, TARGET_SPACING_M)
        print(f"  resampled to {len(resampled_points)} points")

        print("  fetching elevations from Open Topo Data...")
        elevations = get_elevations(resampled_points)

        geometry_rows = build_route_geometry(route_id, resampled_points, elevations)
        all_geometry_rows.extend(geometry_rows)

        baseline_row = physics_baseline(route_id, route["route_name"], geometry_rows, VEHICLE)
        all_baseline_rows.append(baseline_row)

        print(f"  total_distance_km = {baseline_row['total_distance_km']}")
        if route_id in EXPECTED_DISTANCE_KM:
            lo, hi = EXPECTED_DISTANCE_KM[route_id]
            if not (lo <= baseline_row["total_distance_km"] <= hi):
                print(f"  *** WARNING: expected {lo}-{hi} km for {route_id}, "
                      f"got {baseline_row['total_distance_km']} km. Check the "
                      f"per-leg breakdown above for the short leg. ***")
            else:
                print(f"  distance check OK (expected {lo}-{hi} km)")
        print(f"  total_elevation_gain_m = {baseline_row['total_elevation_gain_m']}")
        print(f"  baseline_fuel_L = {baseline_row['baseline_fuel_L']}")
        print(f"  baseline_time_hr (placeholder, flat-speed) = {baseline_row['baseline_time_hr']}")
        print()

    write_geometry_csv(all_geometry_rows, "all_routes_geometry.csv")
    write_baseline_csv(all_baseline_rows, "all_routes_physics_baseline.csv")
    print("Wrote all_routes_geometry.csv and all_routes_physics_baseline.csv")


if __name__ == "__main__":
    main()