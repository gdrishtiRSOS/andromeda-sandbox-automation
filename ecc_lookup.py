"""ecc_lookup — county in, boundary geometry and ECC jurisdiction scope out.

This module returns a **county footprint**, not an ECC service-area boundary.
ECC/PSAP service areas are operational jurisdictions defined by local
agreements and frequently don't follow county lines; authoritative polygons
for them are not reliably available. Every result carries `disclaimer` and
`county.boundary_type == "county_footprint"` so this distinction survives
even when the result is separated from this docstring.

Informational only — never authoritative for 9-1-1 routing, dispatch, or
emergency response.

Public API:
    lookup_county(query, *, vintage=2025, cache_dir=None, counties=None) -> dict
    lookup_county_to_geojson(query, output_dir, *, vintage=2025, cache_dir=None, counties=None) -> dict
    to_geojson(result, path) -> None

"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
from shapely.geometry import MultiPolygon, mapping

_USER_AGENT = "ecc_lookup/0.1 (; informational county lookup)"
_DOWNLOAD_TIMEOUT_S = 30
_CENSUS_URL_TEMPLATE = "https://www2.census.gov/geo/tiger/GENZ{vintage}/shp/cb_{vintage}_us_county_500k.zip"

# FCC 911 Master PSAP Registry. fcc.gov's own registry page is behind
# Akamai bot protection and not reachable via urllib (verified: both a
# direct urllib fetch and an authenticated web-fetch tool get HTTP 403).
# This opendata.fcc.gov Socrata mirror is the only programmatically
# reachable copy, and it is confirmed STALE - its dataset metadata reports
# last modified 2019-02-26, not the ~2025 date FCC's own page claims.
# Recorded honestly via the `vintage` field on every result rather than
# presented as current; revisit if fcc.gov ever becomes fetchable.
_FCC_REGISTRY_URL = "https://opendata.fcc.gov/api/v3/views/dpq5-ta9j/export.csv?accessType=DOWNLOAD"
_FCC_REGISTRY_VINTAGE = "2019-02-26 (stale mirror; FCC's own registry page is not programmatically reachable)"

# The FCC registry's `County` column drops these generic suffixes for
# ordinary county-equivalents (verified against the real registry: LA
# parishes lose "Parish", PR municipios lose "Municipio", most counties
# lose "County") but KEEPS suffixes that disambiguate a county-equivalent
# from an ordinary county of the same base name (AK "Borough"/"Census
# Area"/"City and Borough", VA/MD/MO independent-city "City"). So only
# these three generic suffixes are stripped when building the join key.
_GENERIC_SUFFIXES_DROPPED_BY_REGISTRY = (" County", " Parish", " Municipio")

# Known Alaska Census-area split where the registry still uses the legacy,
# pre-split name (Valdez-Cordova Census Area -> Chugach + Copper River,
# effective 2019). A registry row keyed to the legacy name is attributed
# to BOTH successor county-equivalents, since it predates the split and
# there's no way to tell which side it now serves - counties_listed still
# shows the real (legacy) registry value, never a fabricated new one.
#
# Other AK naming drift observed in the live registry (e.g. "Wade Hampton"
# vs current "Kusilvak", "Prince Of Wales-outer Ketchikan" vs current
# "Prince of Wales-Hyder", "Wrangell-petersburg" vs the current split into
# Wrangell City and Borough / Petersburg Borough, "Anchorage Borough" vs
# current "Anchorage Municipality") is NOT crosswalked here - only the
# Valdez-Cordova case this project's spec explicitly calls out - so those
# other counties will legitimately (if misleadingly) come back
# "no_entries" until a verified crosswalk is built for them too.
_AK_LEGACY_TO_CURRENT = {
    "valdez-cordova census area": ["Chugach Census Area", "Copper River Census Area"],
}

# LSAD (Legal/Statistical Area Description) code -> county-equivalent label.
# Built from the actual 2025 cb_us_county_500k layer's observed LSAD values
# (verified by inspection, not assumed from Census documentation alone).
_LSAD_TO_TYPE = {
    "00": "county_equivalent",  # e.g. Guam - unclassified, whole territory
    "03": "city_and_borough",  # AK: Juneau, Sitka, Wrangell, Yakutat
    "04": "borough",  # AK boroughs
    "05": "census_area",  # AK unorganized census areas
    "06": "county",  # standard county
    "07": "district",  # American Samoa districts
    "10": "island",  # US Virgin Islands
    "12": "municipality",  # AK: Anchorage, Skagway
    "13": "municipio",  # Puerto Rico
    "15": "parish",  # Louisiana
    "25": "independent_city",  # VA, MD (Baltimore city), MO (St. Louis city)
    "PL": "planning_region",  # Connecticut, post-2022
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_cache_dir(cache_dir: str | Path | None) -> Path:
    """Resolve the cache directory. None -> a stable subdirectory under the
    platform temp path. Never the caller's working directory or repo root.
    Creates the directory (and only this directory) if it doesn't exist.
    """
    resolved = Path(cache_dir) if cache_dir is not None else Path(tempfile.gettempdir()) / "ecc_lookup_cache"
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _download_to_cache(url: str, dest_path: Path) -> None:
    """Download `url` to `dest_path`, skipping if it already exists.
    Downloads to a temp file in the same directory first, then renames -
    so an interrupted download can never leave a corrupt cache file.
    """
    if dest_path.exists():
        return
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    tmp_path = dest_path.with_name(dest_path.name + ".part")
    with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_S) as response:
        with open(tmp_path, "wb") as tmp_file:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                tmp_file.write(chunk)
    tmp_path.replace(dest_path)


def _load_counties(vintage: int, cache_dir: str | Path | None) -> gpd.GeoDataFrame:
    """Return the national county-equivalent boundary layer for `vintage`,
    downloading and caching the Census cartographic boundary ZIP if needed.

    Network-touching. Columns are read as-published (GEOID, NAME, NAMELSAD,
    STUSPS, LSAD, ALAND, AWATER, geometry as of the 2023+ posting) rather
    than assumed, since the schema has changed across vintages before
    (AFFGEOID -> GEOIDFQ). Reprojects to EPSG:4326 (source is EPSG:4269).
    """
    resolved_dir = _resolve_cache_dir(cache_dir)
    zip_path = resolved_dir / f"cb_{vintage}_us_county_500k.zip"
    url = _CENSUS_URL_TEMPLATE.format(vintage=vintage)
    _download_to_cache(url, zip_path)

    gdf = gpd.read_file(f"zip://{zip_path}")
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    return gdf


_GEOID_RE = re.compile(r"\d{5}")


def _resolve_county(query: str, counties: gpd.GeoDataFrame) -> pd.Series:
    """Resolve `query` (5-digit GEOID or "Name, ST") to exactly one row of
    `counties`. Exact match only - no fuzzy/partial matching, no bare
    names, no multi-county queries.

    Matches on GEOID where possible; otherwise on the full NAMELSAD (e.g.
    "St. Louis County" vs "St. Louis city") plus STUSPS, since NAME alone
    collides for county/independent-city pairs like St. Louis and
    Baltimore.

    Raises:
        ValueError: query is malformed (not a 5-digit GEOID or "Name, ST").
        LookupError: query is well-formed but matches zero rows (or, in
            defensive cases, more than one).
    """
    query = query.strip()

    if _GEOID_RE.fullmatch(query):
        matched = counties[counties["GEOID"] == query]
    else:
        if "," not in query:
            raise ValueError(f"query must be a 5-digit GEOID or 'Name, ST': {query!r}")
        name_part, _, state_part = query.rpartition(",")
        name_part = name_part.strip()
        state_part = state_part.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", state_part):
            raise ValueError(f"query must be a 5-digit GEOID or 'Name, ST': {query!r}")
        if not name_part:
            raise ValueError(f"query must be a 5-digit GEOID or 'Name, ST': {query!r}")
        matched = counties[(counties["NAMELSAD"] == name_part) & (counties["STUSPS"] == state_part)]

    if len(matched) == 0:
        raise LookupError(f"No county found for query {query!r}")
    if len(matched) > 1:
        raise LookupError(f"Query {query!r} matched more than one county")
    return matched.iloc[0]


def _to_multipolygon(geom):
    """Promote a Polygon to MultiPolygon; pass MultiPolygon through as-is,
    so callers of lookup_county never branch on geometry type.
    """
    if geom.geom_type == "Polygon":
        return MultiPolygon([geom])
    if geom.geom_type == "MultiPolygon":
        return geom
    raise ValueError(f"Unexpected county geometry type: {geom.geom_type!r}")


def _geometry_to_coords(multipolygon: MultiPolygon) -> list:
    """Convert a shapely MultiPolygon to a plain-Python nested coordinate
    list (no numpy floats, no tuples) suitable for json.dumps. No winding-
    order correction or rounding here - that's an RFC7946 GeoJSON-file
    concern, applied only in to_geojson, not in the API return contract.
    """
    raw = mapping(multipolygon)["coordinates"]

    def _walk(node):
        if isinstance(node, (list, tuple)):
            return [_walk(child) for child in node]
        return float(node)

    return _walk(raw)


def _classify_county_equivalent(county: pd.Series) -> str:
    return _LSAD_TO_TYPE.get(str(county["LSAD"]), "county_equivalent")


def _build_result(
    county: pd.Series,
    roster: list[dict] | None,
    sources: list[dict],
    *,
    jurisdiction_status: str,
    query: str,
) -> dict:
    """Assemble the exact return contract dict. The ONLY function that
    defines the output shape - never construct this dict anywhere else.

    All values are cast to plain Python types (str, int, list, dict) at
    this boundary: no numpy scalars, no shapely objects, no pandas
    Timestamp/NA, so json.dumps(result) always succeeds.
    """
    geom_mp = _to_multipolygon(county.geometry)
    coords = _geometry_to_coords(geom_mp)

    result = {
        "query": query,
        "county": {
            "name": str(county["NAME"]),
            "state": str(county["STUSPS"]),
            "geoid": str(county["GEOID"]),
            "county_equivalent_type": _classify_county_equivalent(county),
            "boundary_type": "county_footprint",
            "aland_sq_m": int(county["ALAND"]),
            "awater_sq_m": int(county["AWATER"]),
        },
        "geometry": {
            "type": "MultiPolygon",
            "coordinates": coords,
        },
        "jurisdiction_scope": {
            "status": jurisdiction_status,
            "eccs": roster or [],
        },
        "sources": sources,
        "disclaimer": (
            "Boundary is a Census county geography, NOT an ECC service area. "
            "Informational only; not for 9-1-1 routing or dispatch use."
        ),
    }
    return result


def _fetch_registry(cache_dir: str | Path | None) -> tuple[list[dict], str]:
    """Download (or read from cache) the FCC PSAP registry and parse it into
    a flat list of dicts: {fcc_psap_id, name, state, county, agency_type}.
    PSAP identity attributes only - no geometry, ever.

    Network-touching. Same cache/temp-then-rename/skip-if-cached/timeout/
    User-Agent discipline as _load_counties. Never raises for network or
    parse failure - returns ([], "unavailable") instead, so a host project
    never loses geometry to an FCC outage.
    """
    try:
        resolved_dir = _resolve_cache_dir(cache_dir)
        csv_path = resolved_dir / "fcc_psap_registry.csv"
        _download_to_cache(_FCC_REGISTRY_URL, csv_path)
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            raw_rows = list(csv.DictReader(f))
    except (urllib.error.URLError, OSError, TimeoutError, csv.Error):
        return [], "unavailable"

    try:
        rows = [
            {
                "fcc_psap_id": row["PSAP ID"],
                "name": row["PSAP Name"],
                "state": row["State"],
                "county": row["County"] or "",
                # The registry carries no primary/secondary PSAP-type field
                # (verified by inspecting its actual columns) - never
                # fabricate one; report unknown.
                "agency_type": "unknown",
            }
            for row in raw_rows
        ]
    except KeyError:
        return [], "unavailable"

    return rows, "ok"


def _registry_join_keys(county: pd.Series) -> list[str]:
    """Candidate FCC-registry `county` values for `county`, per the suffix
    rule in _GENERIC_SUFFIXES_DROPPED_BY_REGISTRY, plus any known legacy
    Alaska name this county-equivalent is a successor to.
    """
    namelsad = str(county["NAMELSAD"])
    key = namelsad
    for suffix in _GENERIC_SUFFIXES_DROPPED_BY_REGISTRY:
        if key.endswith(suffix):
            key = key[: -len(suffix)]
            break

    keys = {key}
    for legacy_name, current_names in _AK_LEGACY_TO_CURRENT.items():
        if namelsad in current_names:
            keys.add(legacy_name)
    return list(keys)


def _roster_for(registry: list[dict], county: pd.Series) -> list[dict]:
    """Return the ECC roster entries whose registry county/state match
    `county`, via exact (case-normalized) matching against the documented
    join keys only - never fuzzy matching, never inferring an ECC name
    from the county name.
    """
    state = str(county["STUSPS"])
    candidate_keys = {key.casefold() for key in _registry_join_keys(county)}

    matched_ids = {
        row["fcc_psap_id"]
        for row in registry
        if row["state"] == state and row["county"].strip().casefold() in candidate_keys
    }
    if not matched_ids:
        return []

    # counties_listed aggregates every county the same PSAP ID appears
    # against in the FULL registry, not just the queried county - ECCs
    # often serve several counties, and that's information, not an error.
    counties_by_id: dict[str, list[str]] = {}
    name_by_id: dict[str, str] = {}
    agency_type_by_id: dict[str, str] = {}
    for row in registry:
        if row["fcc_psap_id"] in matched_ids:
            counties_by_id.setdefault(row["fcc_psap_id"], []).append(row["county"])
            name_by_id[row["fcc_psap_id"]] = row["name"]
            agency_type_by_id[row["fcc_psap_id"]] = row["agency_type"]

    return [
        {
            "name": name_by_id[psap_id],
            "fcc_psap_id": psap_id,
            "agency_type": agency_type_by_id[psap_id],
            "counties_listed": counties_by_id[psap_id],
            "note": "Operates in this county per FCC registry. Service area boundary unknown.",
        }
        for psap_id in sorted(matched_ids)
    ]


def _jurisdiction_status(registry_status: str, county: pd.Series, roster: list[dict]) -> str:
    if registry_status == "unavailable":
        return "unavailable"
    # Connecticut: the registry still lists CT's legacy counties, and the
    # county->planning-region relationship is many-to-many at the
    # sub-county (town) level - not a crosswalk derivable from this
    # dataset alone. An empty roster here is a crosswalk failure, not an
    # absence of ECCs, so report unavailable rather than a misleading ok.
    if str(county["STUSPS"]) == "CT" and _classify_county_equivalent(county) == "planning_region":
        return "unavailable"
    if not roster:
        return "no_entries"
    return "ok"


def lookup_county(
    query: str,
    *,
    vintage: int = 2025,
    cache_dir: str | Path | None = None,
    counties: Any = None,
) -> dict:
    """Resolve a single county or county-equivalent to its boundary geometry
    and known ECC jurisdiction scope.

    Args:
        query: A 5-digit county GEOID (e.g. "06037") or "<Name>, <ST>"
            (e.g. "Los Angeles County, CA"). State is required; no bare
            names, no regions, no multi-county merging, no batch input,
            no fuzzy matching.
        vintage: Census cartographic boundary file year to use.
        cache_dir: Directory for cached downloads. None resolves to a
            platform temp path; never the caller's working directory.
        counties: An already-loaded GeoDataFrame of counties, so a host
            project doing many lookups can download once and control its
            own caching. If None, one is loaded (and cached) internally.

    Returns:
        A dict matching the return contract in CLAUDE.md: query, county,
        geometry, jurisdiction_scope, sources, disclaimer.

    Raises:
        ValueError: query is malformed (not a 5-digit GEOID or "Name, ST").
        LookupError: query is well-formed but no county resolves.
    """
    if counties is None:
        counties = _load_counties(vintage, cache_dir)
    county = _resolve_county(query, counties)

    registry, registry_status = _fetch_registry(cache_dir)
    roster = _roster_for(registry, county) if registry_status == "ok" else []
    jurisdiction_status = _jurisdiction_status(registry_status, county, roster)

    sources = [
        {
            "name": "US Census Bureau Cartographic Boundary Files",
            "url": _CENSUS_URL_TEMPLATE.format(vintage=vintage),
            "retrieved_at": _now_iso(),
            "vintage": str(vintage),
            "license": "US Government Work (public domain)",
        },
        {
            "name": "FCC 911 Master PSAP Registry (opendata.fcc.gov mirror)",
            "url": _FCC_REGISTRY_URL,
            "retrieved_at": _now_iso(),
            "vintage": _FCC_REGISTRY_VINTAGE,
            "license": "Public Domain U.S. Government",
        },
    ]

    return _build_result(
        county,
        roster=roster,
        sources=sources,
        jurisdiction_status=jurisdiction_status,
        query=query,
    )


def _signed_area(ring: list[list[float]]) -> float:
    """Shoelace formula. Positive => counterclockwise, negative => clockwise."""
    total = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _ensure_ring_winding(ring: list[list[float]], *, clockwise: bool) -> list[list[float]]:
    """Reverse `ring` if its winding doesn't match the requested direction."""
    area = _signed_area(ring)
    is_clockwise = area < 0
    if is_clockwise != clockwise:
        return list(reversed(ring))
    return ring


def _ensure_ring_closed(ring: list[list[float]]) -> list[list[float]]:
    if ring[0] != ring[-1]:
        return ring + [ring[0]]
    return ring


def _round_ring(ring: list[list[float]]) -> list[list[float]]:
    return [[round(lon, 6), round(lat, 6)] for lon, lat in ring]


def _fix_polygon_rings(polygon: list[list[list[float]]]) -> list[list[list[float]]]:
    """RFC 7946: exterior ring counterclockwise, holes clockwise, rings
    closed, coordinates rounded to 6 decimal places (~10 cm).
    """
    fixed = []
    for index, ring in enumerate(polygon):
        ring = _ensure_ring_winding(ring, clockwise=(index != 0))
        ring = _ensure_ring_closed(ring)
        ring = _round_ring(ring)
        fixed.append(ring)
    return fixed


def _fix_multipolygon(coordinates: list) -> list:
    return [_fix_polygon_rings(polygon) for polygon in coordinates]


def to_geojson(result: dict, path: str | Path) -> None:
    """Write `result` (as returned by lookup_county) to a single-Feature
    GeoJSON FeatureCollection at `path`. Uses only the stdlib `json` module.

    Exactly one Feature; geometry is always MultiPolygon. Coordinates are
    [lon, lat], rounded to 6 decimals, rings closed, exterior rings wound
    counterclockwise per RFC 7946 (holes clockwise) - regardless of how
    the source shapefile wound them. No `crs`, no `bbox`, no feature `id`.
    """
    eccs = result["jurisdiction_scope"]["eccs"]

    properties = {
        "name": result["county"]["name"],
        "state": result["county"]["state"],
        "geoid": result["county"]["geoid"],
        "boundary_type": result["county"]["boundary_type"],
        "aland_sq_m": result["county"]["aland_sq_m"],
        "awater_sq_m": result["county"]["awater_sq_m"],
        "ecc_count": len(eccs),
        "jurisdiction_scope_status": result["jurisdiction_scope"]["status"],
        "disclaimer": result["disclaimer"],
        "jurisdiction_scope": {"eccs": eccs},
        "sources": result["sources"],
    }

    feature = {
        "type": "Feature",
        "properties": properties,
        "geometry": {
            "type": "MultiPolygon",
            "coordinates": _fix_multipolygon(result["geometry"]["coordinates"]),
        },
    }
    feature_collection = {"type": "FeatureCollection", "features": [feature]}

    with open(path, "w", encoding="utf-8") as f:
        json.dump(feature_collection, f, indent=2)
        f.write("\n")


def lookup_county_to_geojson(
    query: str,
    output_dir: str | Path,
    *,
    vintage: int = 2025,
    cache_dir: str | Path | None = None,
    counties: Any = None,
) -> dict:
    """Resolve `query` via lookup_county and write the result to a
    .geojson file under `output_dir`, named by the county's GEOID (e.g.
    "06037.geojson") so the filename is stable and filesystem-safe
    regardless of punctuation in the county name (e.g. "St. Louis").

    Creates `output_dir` if it doesn't exist (and only that directory -
    same discipline as cache_dir). Returns the same dict lookup_county
    would return; the dict remains the primary interface, the file is a
    side effect for callers that want one.

    Raises the same exceptions as lookup_county.
    """
    result = lookup_county(query, vintage=vintage, cache_dir=cache_dir, counties=counties)

    resolved_output_dir = Path(output_dir)
    resolved_output_dir.mkdir(parents=True, exist_ok=True)
    out_path = resolved_output_dir / f"{result['county']['geoid']}.geojson"
    to_geojson(result, out_path)

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Look up a US county's boundary footprint and known ECC jurisdiction scope.",
    )
    parser.add_argument("query", help='5-digit county GEOID or "<Name>, <ST>"')
    parser.add_argument("--vintage", type=int, default=2025, help="Census cartographic boundary file year")
    parser.add_argument("--cache-dir", default=None, help="Directory for cached downloads")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="If set, also write a <geoid>.geojson file to this directory",
    )
    args = parser.parse_args()

    try:
        if args.output_dir:
            result = lookup_county_to_geojson(
                args.query, args.output_dir, vintage=args.vintage, cache_dir=args.cache_dir
            )
        else:
            result = lookup_county(args.query, vintage=args.vintage, cache_dir=args.cache_dir)
    except (LookupError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, indent=2))
