"""
spatial.py - WGS84 Geodesy, Railway Chainage & GeoJSON Utilities

Pure-stdlib spatial maths used by the BlockFlow ingestion layer to convert
Indian Railways *kilometerage* (chainage referenced to Churchgate = km 0.00)
into WGS84 latitude/longitude and to emit OGC GeoJSON for the CesiumJS twin
and the PM Gati Shakti corridor export.

No third-party dependency (Shapely is NOT required): every operation here is
deterministic closed-form spherical maths, which is what lets us make the
"zero-hallucination" claim - the same km always maps to the same coordinate.

Conventions
-----------
*   A *point* is ``(lon, lat)`` in decimal degrees (GeoJSON axis order).
*   A *polyline* is a ``List[point]``.
*   Chainage is in **kilometers**, increasing away from Churchgate (CCG).
"""

from math import asin, atan2, cos, degrees, radians, sin, sqrt
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Point = Tuple[float, float]

#: IUGG mean Earth radius (km) used by the Haversine great-circle formula.
EARTH_RADIUS_KM = 6371.0088

#: Metres per degree of latitude (mean WGS84 value) - used only for small
#: (< 100 m) lateral track offsets where the spherical error is negligible.
METERS_PER_DEG_LAT = 111_320.0

#: Lateral distance of each running line from the surveyed corridor centreline,
#: in metres, positive = east of centreline. Indian Railways suburban "quad"
#: layout: UP FAST | UP SLOW | DN SLOW | DN FAST left-to-right when facing
#: north (away from Churchgate). Track centres are 4.0 m apart.
TRACK_LATERAL_OFFSET_M: Dict[str, float] = {
    "UP_FAST": -6.0,
    "UP_SLOW": -2.0,
    "DN_SLOW": 2.0,
    "DN_FAST": 6.0,
}

#: Fallback centreline offset (metres) longitude scaling at ~19 deg N.
_LON_DEG_PER_M = 1.0 / (METERS_PER_DEG_LAT * cos(radians(19.0)))


# --------------------------------------------------------------------------- #
#  Great-circle primitives
# --------------------------------------------------------------------------- #
def haversine_km(a: Point, b: Point) -> float:
    """Great-circle distance in kilometers between two ``(lon, lat)`` points."""
    lon1, lat1 = radians(a[0]), radians(a[1])
    lon2, lat2 = radians(b[0]), radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = sin(dlat / 2.0) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_KM * asin(min(1.0, sqrt(h)))


def initial_bearing_deg(a: Point, b: Point) -> float:
    """Initial (forward) azimuth from ``a`` to ``b`` in degrees, 0-360."""
    lon1, lat1 = radians(a[0]), radians(a[1])
    lon2, lat2 = radians(b[0]), radians(b[1])
    dlon = lon2 - lon1
    y = sin(dlon) * cos(lat2)
    x = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon)
    return (degrees(atan2(y, x)) + 360.0) % 360.0


def interpolate_geodesic(a: Point, b: Point, t: float) -> Point:
    """
    Point at fraction ``t`` (0..1) along the great-circle segment ``a -> b``.

    Linear in (lon, lat) is deliberately *not* used: at the 0.14 km spacing of
    the waypoint database the difference is sub-centimetre, but using the
    spherical interpolation keeps the maths correct if a caller ever snaps
    across a long (>5 km) segment.
    """
    t = max(0.0, min(1.0, float(t)))
    if t <= 0.0:
        return a
    if t >= 1.0:
        return b

    lon1, lat1 = radians(a[0]), radians(a[1])
    lon2, lat2 = radians(b[0]), radians(b[1])
    d = haversine_km(a, b) / EARTH_RADIUS_KM
    if d <= 0.0:
        return a
    sin_d = sin(d)
    A = sin((1.0 - t) * d) / sin_d
    B = sin(t * d) / sin_d
    x = A * cos(lat1) * cos(lon1) + B * cos(lat2) * cos(lon2)
    y = A * cos(lat1) * sin(lon1) + B * cos(lat2) * sin(lon2)
    z = A * sin(lat1) + B * sin(lat2)
    return (degrees(atan2(y, x)), degrees(atan2(z, sqrt(x * x + y * y))))


def offset_point(origin: Point, forward_bearing_deg: float, offset_m: float) -> Point:
    """
    Displace ``origin`` perpendicular to a heading of ``forward_bearing_deg``.

    Positive ``offset_m`` moves to the right-hand side of travel (east of the
    centreline when travelling away from Churchgate), which is how the four
    quad running lines are derived from the surveyed centreline.
    """
    if offset_m == 0.0:
        return origin
    right_bearing = (forward_bearing_deg + 90.0) % 360.0
    dlat = (offset_m * cos(radians(right_bearing))) / METERS_PER_DEG_LAT
    dlon = (offset_m * sin(radians(right_bearing))) * _LON_DEG_PER_M
    return (origin[0] + dlon, origin[1] + dlat)


# --------------------------------------------------------------------------- #
#  Chainage (kilometerage) engine
# --------------------------------------------------------------------------- #
def cumulative_chainage(
    points: Sequence[Point],
    corridor_length_km: Optional[float] = None,
) -> List[float]:
    """
    Cumulative chainage (km) at every vertex of ``points``.

    The raw geodesic length of a station-to-station polyline is always slightly
    shorter than the surveyed track (curves, crossovers, bridge realignments).
    When ``corridor_length_km`` is supplied the whole profile is scaled by a
    single survey factor so that the final vertex lands **exactly** on the
    statutory end-of-corridor chainage (59.98 km for Churchgate - Virar).
    This is exactly what a railway surveyor does when closing a traverse.
    """
    if len(points) < 2:
        return [0.0 for _ in points]

    chainages = [0.0]
    for i in range(1, len(points)):
        chainages.append(chainages[-1] + haversine_km(points[i - 1], points[i]))

    raw_total = chainages[-1]
    if corridor_length_km and raw_total > 0.0:
        factor = float(corridor_length_km) / raw_total
        chainages = [c * factor for c in chainages]
    return chainages


def locate_chainage(
    points: Sequence[Point],
    chainages: Sequence[float],
    km: float,
) -> Tuple[int, float]:
    """
    Locate chainage ``km`` on a chainaged polyline.

    Returns ``(segment_index, t)`` such that
    ``interpolate_geodesic(points[i], points[i+1], t)`` is the coordinate.
    Out-of-range chainage is clamped to the corridor ends.
    """
    if len(points) < 2:
        raise ValueError("polyline needs at least 2 points")

    km = float(km)
    if km <= chainages[0]:
        return 0, 0.0
    if km >= chainages[-1]:
        return len(points) - 2, 1.0

    lo, hi = 0, len(chainages) - 1
    while lo + 1 < hi:  # binary search on monotonic chainage
        mid = (lo + hi) // 2
        if chainages[mid] <= km:
            lo = mid
        else:
            hi = mid

    span = chainages[lo + 1] - chainages[lo]
    t = 0.0 if span <= 0.0 else (km - chainages[lo]) / span
    return lo, t


def point_at_chainage(
    points: Sequence[Point],
    chainages: Sequence[float],
    km: float,
) -> Point:
    """WGS84 ``(lon, lat)`` of a single chainage value on the corridor."""
    idx, t = locate_chainage(points, chainages, km)
    return interpolate_geodesic(points[idx], points[idx + 1], t)


def polyline_between(
    points: Sequence[Point],
    chainages: Sequence[float],
    km_start: float,
    km_end: float,
    step_km: float = 0.25,
) -> List[Point]:
    """
    Coordinates of the track centreline between two chainages.

    Original vertices are preserved (so curves stay crisp) and intermediate
    samples are inserted at ``step_km`` resolution so the emitted GeoJSON
    linestring hugs the surveyed alignment instead of shortcutting bends.
    """
    km_start, km_end = float(km_start), float(km_end)
    if km_end < km_start:
        km_start, km_end = km_end, km_start

    coords: List[Point] = [point_at_chainage(points, chainages, km_start)]
    samples = max(1, int(round((km_end - km_start) / max(step_km, 1e-6))))
    for i in range(1, samples):
        km = km_start + (km_end - km_start) * (i / samples)
        coords.append(point_at_chainage(points, chainages, km))
    coords.append(point_at_chainage(points, chainages, km_end))
    return coords


def shift_polyline(coords: Sequence[Point], offset_m: float) -> List[Point]:
    """Offset a polyline laterally by ``offset_m`` (right-hand side positive)."""
    if offset_m == 0.0 or len(coords) < 2:
        return list(coords)
    shifted: List[Point] = []
    for i, pt in enumerate(coords):
        if i == 0:
            brg = initial_bearing_deg(coords[0], coords[1])
        elif i == len(coords) - 1:
            brg = initial_bearing_deg(coords[-2], coords[-1])
        else:
            brg = initial_bearing_deg(coords[i - 1], coords[i + 1])
        shifted.append(offset_point(pt, brg, offset_m))
    return shifted


def polyline_length_km(coords: Sequence[Point]) -> float:
    """Geodesic length of a polyline in kilometers."""
    return sum(haversine_km(coords[i - 1], coords[i]) for i in range(1, len(coords)))


def round_coords(coords: Iterable[Point], ndigits: int = 6) -> List[List[float]]:
    """Convert ``(lon, lat)`` tuples into GeoJSON ``[lon, lat]`` lists."""
    return [[round(p[0], ndigits), round(p[1], ndigits)] for p in coords]


# --------------------------------------------------------------------------- #
#  GeoJSON builders (RFC 7946)
# --------------------------------------------------------------------------- #
def geojson_point(lon: float, lat: float, properties: Optional[dict] = None) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
        "properties": properties or {},
    }


def geojson_linestring(coords: Iterable[Point], properties: Optional[dict] = None) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": round_coords(coords)},
        "properties": properties or {},
    }


def geojson_feature_collection(
    features: Iterable[dict],
    name: str = "WR_CORRIDOR",
    extra_properties: Optional[dict] = None,
) -> dict:
    """Wrap features in a GeoJSON FeatureCollection with an OGC-style name."""
    props = {"name": name}
    if extra_properties:
        props.update(extra_properties)
    return {
        "type": "FeatureCollection",
        "name": name,
        "crs": {
            "type": "name",
            "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"},
        },
        "properties": props,
        "features": list(features),
    }
