"""Turn what a person types into the inputs the Andromeda tooling needs.

`ecc_lookup.lookup_county` is deliberately strict: a 5-digit GEOID or an exact
"<NAMELSAD>, <ST>". That is right for a library, but the account-creation tool
is handed things like "Lincoln, Nebraska" -- a *city*, with the state spelled
out -- and has to land on Lancaster County.

This module sits on top and does three things ecc_lookup should not:

* accepts city and town names, resolving them to the county that contains
  them, via the Census places file and a spatial join;
* accepts loose spellings of the county and state ("Washington, TX" for
  "Washington County, TX"; "Nebraska" for "NE");
* refuses ambiguity rather than guessing, listing the candidates -- the same
  rule the authority lookup follows, for the same reason.

It then adapts the result into what the rest of the tooling wants: a trimmed
FeatureCollection to POST as `exact_polygon`, and the account fields derived
from the place.

geopandas is only imported when a lookup actually runs, so the rest of the
package stays free of heavy geospatial dependencies.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger(__name__)

__all__ = [
    "AmbiguousPlaceError",
    "PlaceError",
    "PlaceNotFoundError",
    "ResolvedPlace",
    "UnusableBoundaryError",
    "account_fields",
    "normalise_state",
    "parse_query",
    "resolve_place",
    "to_andromeda_polygon",
]

def _ecc_lookup():
    """Import ecc_lookup from wherever it has been put.

    It is a standalone module rather than part of this package, so it may sit
    at the project root or have been vendored into `andromeda/`. Both work;
    the error says so rather than leaving a bare ModuleNotFoundError.
    """
    try:
        import ecc_lookup            # project root, or anywhere on sys.path
        return ecc_lookup
    except ModuleNotFoundError:
        pass
    try:
        from andromeda import ecc_lookup as vendored   # vendored into the package
        return vendored
    except ModuleNotFoundError as exc:
        missing = getattr(exc, "name", "") or ""
        if missing and missing not in ("ecc_lookup", "andromeda.ecc_lookup"):
            # ecc_lookup was found but its own dependencies were not
            raise PlaceError(
                f"ecc_lookup needs {missing!r}, which is not installed.\n"
                "  pip install geopandas pandas shapely"
            ) from exc
        raise PlaceError(
            "ecc_lookup.py was not found. Put it next to smoke_test.py at the "
            "project root, or inside the andromeda package, and make sure you "
            "run commands from the project root."
        ) from exc


_GEOID_RE = re.compile(r"\d{5}")
_PLACES_URL_TEMPLATE = (
    "https://www2.census.gov/geo/tiger/GENZ{vintage}/shp/cb_{vintage}_us_place_500k.zip"
)

#: Properties worth keeping on the polygon we send to Andromeda. The rest --
#: sources, the disclaimer, the full ECC roster -- stay in our own record.
#: In particular the disclaimer says "not for 9-1-1 routing or dispatch use",
#: which is true and useful in a file, and alarming in a 911 platform's
#: jurisdiction table.
ANDROMEDA_PROPERTY_KEYS = ("name", "state", "geoid", "boundary_type")

#: jurisdiction_scope statuses ecc_lookup can report.
STATUS_OK = "ok"
STATUS_NO_ENTRIES = "no_entries"
STATUS_UNAVAILABLE = "unavailable"

STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "puerto rico": "PR", "guam": "GU", "american samoa": "AS",
    "us virgin islands": "VI", "u.s. virgin islands": "VI",
    "virgin islands": "VI", "northern mariana islands": "MP",
}

#: Suffixes the Census attaches to county names that people usually omit.
COUNTY_SUFFIXES = (
    " County", " Parish", " Municipio", " Borough", " Census Area",
    " City and Borough", " Municipality", " city", " Planning Region",
)


class PlaceError(Exception):
    """Base for this module."""


class PlaceNotFoundError(PlaceError):
    def __init__(self, query: str, near: list[str] | None = None):
        self.query = query
        self.near = list(near or [])
        message = f"nothing found for {query!r}"
        if self.near:
            message += ". Did you mean: " + ", ".join(repr(n) for n in self.near)
        else:
            message += ". Try '<County> County, <ST>' or '<City>, <ST>'."
        super().__init__(message)


class AmbiguousPlaceError(PlaceError):
    """Several places matched. Never resolved by guessing -- provisioning the
    wrong county produces a plausible-looking but wrong demo account."""

    def __init__(self, query: str, candidates: list[str]):
        self.query = query
        self.candidates = list(candidates)
        super().__init__(
            f"{len(candidates)} places match {query!r}: "
            + "; ".join(candidates)
            + ". Be more specific, or pass the 5-digit county GEOID."
        )


class UnusableBoundaryError(PlaceError):
    """The boundary resolved, but its ECC scope could not be established."""

    def __init__(self, geoid: str, status: str):
        self.geoid = geoid
        self.status = status
        super().__init__(
            f"county {geoid} has jurisdiction_scope_status {status!r}. "
            f"{'No ECCs are registered for it' if status == STATUS_NO_ENTRIES else 'The FCC registry could not be read'}, "
            f"so the boundary is unverified. Pass require_status=False to use it anyway."
        )


@dataclass(frozen=True)
class ResolvedPlace:
    """What the tool needs to build an account, derived from one query."""

    query: str
    geoid: str
    county_name: str
    state: str
    matched_as: str                      # "geoid" | "county" | "city"
    matched_name: str                    # what actually matched
    result: Mapping[str, Any] = field(repr=False, default_factory=dict)

    @property
    def status(self) -> str:
        return self.result.get("jurisdiction_scope", {}).get("status", STATUS_UNAVAILABLE)

    @property
    def eccs(self) -> list[dict]:
        return self.result.get("jurisdiction_scope", {}).get("eccs", [])

    def __str__(self) -> str:
        via = "" if self.matched_as == "county" else f" via {self.matched_as} {self.matched_name!r}"
        return f"{self.county_name} County, {self.state} ({self.geoid}){via}"


# --------------------------------------------------------------- parsing


def normalise_state(text: str) -> str | None:
    """'NE', 'ne', 'Nebraska' -> 'NE'. None when it is not a state."""
    text = text.strip()
    if re.fullmatch(r"[A-Za-z]{2}", text):
        return text.upper()
    return STATE_NAMES.get(text.casefold())


def parse_query(query: str) -> tuple[str, str]:
    """Split '<place>, <state>' into (place, 'ST').

    Accepts a spelled-out state and tolerates a trailing country:
    'Lincoln, Nebraska, USA' works.

    Raises
    ------
    PlaceError
        No state could be identified. State is required -- there are Lincolns
        in a dozen states and Washington Counties in about thirty.
    """
    parts = [p.strip() for p in query.split(",") if p.strip()]
    if len(parts) >= 3 and parts[-1].upper() in ("US", "USA", "UNITED STATES"):
        parts = parts[:-1]
    if len(parts) < 2:
        raise PlaceError(
            f"{query!r} has no state. Write '<place>, <ST>' -- for example "
            f"'Lincoln, NE'. A bare place name is ambiguous across states."
        )

    state = normalise_state(parts[-1])
    if state is None:
        raise PlaceError(f"{parts[-1]!r} is not a US state or territory")
    place = ", ".join(parts[:-1]).strip()
    if not place:
        raise PlaceError(f"{query!r} has no place name")
    return place, state


def _strip_county_suffix(name: str) -> str:
    for suffix in COUNTY_SUFFIXES:
        if name.lower().endswith(suffix.lower()):
            return name[: -len(suffix)].strip()
    return name


# --------------------------------------------------------------- lookups


def _load_places(vintage: int, cache_dir: str | Path | None):
    """The Census places file, loaded the same way ecc_lookup loads counties."""
    ecc_lookup = _ecc_lookup()

    url = _PLACES_URL_TEMPLATE.format(vintage=vintage)
    cache = ecc_lookup._resolve_cache_dir(cache_dir)
    dest = cache / f"cb_{vintage}_us_place_500k.zip"
    if not dest.exists():
        log.info("downloading the Census places file (once): %s", url)
        ecc_lookup._download_to_cache(url, dest)

    try:
        import geopandas as gpd
    except ModuleNotFoundError as exc:
        raise PlaceError(
            "place lookup needs geopandas.  pip install geopandas pandas shapely"
        ) from exc

    places = gpd.read_file(f"zip://{dest}")
    return places.to_crs("EPSG:4326") if places.crs else places


def _match_county(place: str, state: str, counties) -> list:
    """Rows of `counties` whose name matches, with or without the suffix."""
    in_state = counties[counties["STUSPS"] == state]
    exact = in_state[in_state["NAMELSAD"].str.casefold() == place.casefold()]
    if len(exact):
        return [exact.iloc[i] for i in range(len(exact))]

    bare = _strip_county_suffix(place).casefold()
    loose = in_state[
        in_state["NAMELSAD"].map(lambda n: _strip_county_suffix(str(n)).casefold()) == bare
    ]
    return [loose.iloc[i] for i in range(len(loose))]


def _match_city(place: str, state: str, places, counties) -> list[tuple[str, str]]:
    """(city name, county GEOID) for each city matching, via a spatial join."""
    in_state = places[places["STUSPS"] == state] if "STUSPS" in places.columns else places
    hits = in_state[in_state["NAME"].str.casefold() == place.casefold()]
    if not len(hits):
        bare = re.sub(r"\s+(city|town|village|borough|CDP)$", "", place, flags=re.I)
        hits = in_state[in_state["NAME"].str.casefold() == bare.casefold()]
    if not len(hits):
        return []

    out: list[tuple[str, str]] = []
    counties_in_state = counties[counties["STUSPS"] == state]
    for i in range(len(hits)):
        row = hits.iloc[i]
        point = row.geometry.representative_point()
        containing = counties_in_state[counties_in_state.geometry.contains(point)]
        for j in range(len(containing)):
            out.append((str(row["NAME"]), str(containing.iloc[j]["GEOID"])))
    return out


def resolve_place(
    query: str,
    *,
    vintage: int = 2025,
    cache_dir: str | Path | None = None,
    counties: Any = None,
    places: Any = None,
    require_status: bool = True,
) -> ResolvedPlace:
    """Resolve a typed place to the county boundary an account should use.

    Accepts a 5-digit county GEOID, "<County>, <ST>" with or without the
    County/Parish/Borough suffix, or "<City>, <State>" -- resolved to the
    county containing that city, because ECC jurisdictions are county-level.

    `counties` and `places` can be pre-loaded GeoDataFrames so a batch of
    lookups downloads once.

    Raises
    ------
    PlaceError
        The query has no state, or the state is not recognised.
    PlaceNotFoundError
        Nothing matched.
    AmbiguousPlaceError
        More than one county matched, or a city name spans counties.
    UnusableBoundaryError
        The county resolved but its ECC scope is unknown and
        `require_status` is on.
    """
    ecc_lookup = _ecc_lookup()

    text = query.strip()

    if _GEOID_RE.fullmatch(text):
        result = ecc_lookup.lookup_county(text, vintage=vintage, cache_dir=cache_dir,
                                          counties=counties)
        return _finish(text, result, "geoid", text, require_status)

    place, state = parse_query(text)

    if counties is None:
        counties = ecc_lookup._load_counties(vintage, cache_dir)

    county_rows = _match_county(place, state, counties)
    if len(county_rows) > 1:
        raise AmbiguousPlaceError(
            text, [f"{r['NAMELSAD']}, {state} ({r['GEOID']})" for r in county_rows]
        )
    county_geoid = str(county_rows[0]["GEOID"]) if county_rows else None

    # Always check cities too, even when a county matched. "Lincoln, NE" is
    # both a county and a city -- and the city of Lincoln is in LANCASTER
    # county, 200 miles from Lincoln County. Preferring either silently gets
    # the other case wrong, so a collision is reported rather than resolved.
    city_hits: list[tuple[str, str]] = []
    if places is None:
        try:
            places = _load_places(vintage, cache_dir)
        except Exception as exc:  # network, missing file, unreadable archive
            if county_geoid is None:
                raise
            log.warning(
                "could not load the Census places file (%s), so %r was matched "
                "as a county without checking for a city of the same name",
                exc, place,
            )
    if places is not None:
        city_hits = _match_city(place, state, places, counties)

    city_geoids = sorted({geoid for _, geoid in city_hits})

    if county_geoid and city_geoids and set(city_geoids) != {county_geoid}:
        options = [f"{_label(counties, county_geoid)} -- the county named {place!r}"]
        for geoid in city_geoids:
            if geoid != county_geoid:
                options.append(
                    f"{_label(counties, geoid)} -- contains the city of {place}"
                )
        raise AmbiguousPlaceError(
            f"{place}, {state} is both a county and a city in different counties",
            options,
        )

    if county_geoid:
        result = ecc_lookup.lookup_county(county_geoid, vintage=vintage,
                                          cache_dir=cache_dir, counties=counties)
        return _finish(text, result, "county", str(county_rows[0]["NAMELSAD"]),
                       require_status)

    if not city_geoids:
        raise PlaceNotFoundError(text, _near_misses(place, state, counties))
    if len(city_geoids) > 1:
        raise AmbiguousPlaceError(
            f"the city of {place}, {state} spans several counties",
            [_label(counties, g) for g in city_geoids],
        )

    geoid = city_geoids[0]
    result = ecc_lookup.lookup_county(geoid, vintage=vintage, cache_dir=cache_dir,
                                      counties=counties)
    log.info("%r is a city; using %s County, %s (%s)", place,
             result["county"]["name"], state, geoid)
    return _finish(text, result, "city", city_hits[0][0], require_status)


def _label(counties, geoid: str) -> str:
    """'Lancaster County, NE (31109)' for one GEOID."""
    rows = counties[counties["GEOID"] == geoid]
    if not len(rows):
        return geoid
    row = rows.iloc[0]
    return f"{row['NAMELSAD']}, {row['STUSPS']} ({geoid})"


def _near_misses(place: str, state: str, counties, limit: int = 5) -> list[str]:
    import difflib

    names = [str(n) for n in counties[counties["STUSPS"] == state]["NAMELSAD"]]
    return difflib.get_close_matches(place, names, n=limit, cutoff=0.6)


def _finish(query: str, result: Mapping[str, Any], matched_as: str,
            matched_name: str, require_status: bool) -> ResolvedPlace:
    resolved = ResolvedPlace(
        query=query,
        geoid=result["county"]["geoid"],
        county_name=result["county"]["name"],
        state=result["county"]["state"],
        matched_as=matched_as,
        matched_name=matched_name,
        result=result,
    )
    if require_status and resolved.status != STATUS_OK:
        raise UnusableBoundaryError(resolved.geoid, resolved.status)
    if resolved.status != STATUS_OK:
        log.warning("jurisdiction_scope_status is %r for %s", resolved.status, resolved)
    return resolved


# -------------------------------------------------- adapting for Andromeda
#
# RFC 7946 ring handling, mirroring ecc_lookup.to_geojson. Duplicated rather
# than imported because it is pure arithmetic, and importing ecc_lookup would
# drag geopandas into every caller that only wants to reshape a polygon.


def _signed_area(ring: list[list[float]]) -> float:
    """Shoelace. Positive is counterclockwise, negative is clockwise."""
    total = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _closed(ring: list[list[float]]) -> list[list[float]]:
    if ring and ring[0] != ring[-1]:
        return [*ring, list(ring[0])]
    return list(ring)


def _wound(ring: list[list[float]], *, clockwise: bool) -> list[list[float]]:
    if not ring:
        return ring
    is_clockwise = _signed_area(ring) < 0
    return list(reversed(ring)) if is_clockwise != clockwise else ring


def _rounded(ring: list[list[float]], places: int = 6) -> list[list[float]]:
    return [[round(float(x), places), round(float(y), places)] for x, y in ring]


def _fix_multipolygon(coordinates: list) -> list:
    """Close rings, wind exteriors counterclockwise and holes clockwise, round
    coordinates to 6 decimals -- the same shape ecc_lookup writes to file."""
    fixed = []
    for polygon in coordinates:
        rings = []
        for index, ring in enumerate(polygon):
            ring = _rounded([list(p) for p in ring])
            ring = _wound(_closed(ring)[:-1], clockwise=index > 0)
            rings.append(_closed(ring))
        fixed.append(rings)
    return fixed


def to_andromeda_polygon(
    place: ResolvedPlace,
    *,
    property_keys: tuple[str, ...] = ANDROMEDA_PROPERTY_KEYS,
) -> dict[str, Any]:
    """The FeatureCollection to POST as `exact_polygon`.

    Only a few properties travel with it; sources, the disclaimer and the ECC
    roster belong in the local record, not in a 911 platform's jurisdiction
    table.
    """
    source = dict(place.result["county"])
    properties = {k: source[k] for k in property_keys if k in source}

    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": properties,
            "geometry": {
                "type": "MultiPolygon",
                "coordinates": _fix_multipolygon(
                    place.result["geometry"]["coordinates"]
                ),
            },
        }],
    }


def account_fields(
    place: ResolvedPlace,
    *,
    name_template: str = "{county} County {state} Sandbox",
    account_id_template: str = "SAND_{geoid}",
    use_ecc_name: bool = False,
) -> dict[str, Any]:
    """The account details derived from the place.

    `geoid` is the natural key: it is stable, unique per county, and makes
    "has this county already been created?" a lookup rather than a guess.

    With `use_ecc_name`, the first registered ECC's name is used as the agency
    name -- far better demo material ("Washington County 9-1-1"), but it is a
    *real* agency name on a fictional account, so decide that deliberately.
    """
    name = name_template.format(county=place.county_name, state=place.state,
                                geoid=place.geoid)
    if use_ecc_name and place.eccs:
        name = f"{place.eccs[0]['name']} (Sandbox)"

    return {
        "authority_name": name,
        "account_id": account_id_template.format(geoid=place.geoid,
                                                 state=place.state),
        "country": "USA",
        "state": place.state,
        "geoid": place.geoid,
        "county_name": place.county_name,
        "ecc_count": len(place.eccs),
    }