"""Tests for place resolution.

geopandas is not required: the county and place tables are stubbed with small
objects that expose only what the module touches.
"""

from __future__ import annotations

import pytest

from logic.places import (
    AmbiguousPlaceError,
    PlaceError,
    PlaceNotFoundError,
    ResolvedPlace,
    UnusableBoundaryError,
    account_fields,
    normalise_state,
    parse_query,
    to_andromeda_polygon,
)

SQUARE = [[[[-96.79, 30.04], [-96.08, 30.04], [-96.08, 30.39],
            [-96.79, 30.39], [-96.79, 30.04]]]]


def result(geoid="48477", name="Washington", state="TX", status="ok", eccs=None):
    return {
        "query": geoid,
        "county": {
            "name": name, "state": state, "geoid": geoid,
            "county_equivalent_type": "county",
            "boundary_type": "county_footprint",
            "aland_sq_m": 1564859859, "awater_sq_m": 45466952,
        },
        "geometry": {"type": "MultiPolygon", "coordinates": SQUARE},
        "jurisdiction_scope": {
            "status": status,
            "eccs": eccs if eccs is not None else [
                {"name": "Washington County 9-1-1", "fcc_psap_id": "6452",
                 "agency_type": "unknown", "counties_listed": ["Washington"]},
            ],
        },
        "sources": [{"name": "US Census Bureau", "url": "https://example"}],
        "disclaimer": "Boundary is a Census county geography, NOT an ECC service area.",
    }


def resolved(**kw):
    data = kw.pop("result", result(**kw))
    county = data["county"]
    return ResolvedPlace(
        query=county["geoid"], geoid=county["geoid"], county_name=county["name"],
        state=county["state"], matched_as="geoid", matched_name=county["geoid"],
        result=data,
    )


# ---------------------------------------------------------------- parsing


@pytest.mark.parametrize("text,expected", [
    ("NE", "NE"), ("ne", "NE"), ("Nebraska", "NE"), ("nebraska", "NE"),
    ("new hampshire", "NH"), ("Puerto Rico", "PR"),
])
def test_state_names_and_codes(text, expected):
    assert normalise_state(text) == expected


def test_non_states_are_rejected():
    assert normalise_state("Atlantis") is None


@pytest.mark.parametrize("query,place,state", [
    ("Lincoln, NE", "Lincoln", "NE"),
    ("Lincoln, Nebraska", "Lincoln", "NE"),
    ("Lincoln, Nebraska, USA", "Lincoln", "NE"),
    ("Washington County, TX", "Washington County", "TX"),
    ("  St. Louis city ,  MO  ", "St. Louis city", "MO"),
])
def test_queries_are_parsed(query, place, state):
    assert parse_query(query) == (place, state)


def test_a_bare_place_name_is_refused():
    """There are Lincolns in a dozen states."""
    with pytest.raises(PlaceError, match="no state"):
        parse_query("Lincoln")


def test_an_unknown_state_is_refused():
    with pytest.raises(PlaceError, match="not a US state"):
        parse_query("Lincoln, Atlantis")


# ------------------------------------------------------------- adapting


def test_polygon_carries_only_the_useful_properties():
    polygon = to_andromeda_polygon(resolved())
    props = polygon["features"][0]["properties"]
    assert props == {"name": "Washington", "state": "TX", "geoid": "48477",
                     "boundary_type": "county_footprint"}


def test_polygon_omits_the_disclaimer_and_sources():
    """'not for 9-1-1 routing use' does not belong in a 911 platform's DB."""
    polygon = to_andromeda_polygon(resolved())
    text = str(polygon)
    assert "disclaimer" not in text
    assert "sources" not in text
    assert "jurisdiction_scope" not in text


def test_polygon_is_a_valid_feature_collection():
    polygon = to_andromeda_polygon(resolved())
    assert polygon["type"] == "FeatureCollection"
    assert len(polygon["features"]) == 1
    assert polygon["features"][0]["geometry"]["type"] == "MultiPolygon"


def test_polygon_passes_our_own_geometry_validation():
    from logic.jurisdictions import normalize_geojson, validate_geojson
    polygon = to_andromeda_polygon(resolved())
    validate_geojson(normalize_geojson(polygon))


# --------------------------------------------------------- account fields


def test_account_fields_are_derived_from_the_place():
    fields = account_fields(resolved())
    assert fields["authority_name"] == "Washington County TX Sandbox"
    assert fields["account_id"] == "SAND_48477"
    assert fields["country"] == "USA"
    assert fields["state"] == "TX"


def test_account_id_is_stable_for_the_same_county():
    assert account_fields(resolved())["account_id"] == account_fields(resolved())["account_id"]


def test_templates_can_be_overridden():
    fields = account_fields(resolved(), name_template="{county}/{state} demo",
                            account_id_template="DEMO-{geoid}")
    assert fields["authority_name"] == "Washington/TX demo"
    assert fields["account_id"] == "DEMO-48477"


def test_the_real_ecc_name_can_be_used():
    fields = account_fields(resolved(), use_ecc_name=True)
    assert fields["authority_name"] == "Washington County 9-1-1 (Sandbox)"


def test_ecc_name_falls_back_when_the_registry_is_empty():
    fields = account_fields(resolved(eccs=[]), use_ecc_name=True)
    assert fields["authority_name"] == "Washington County TX Sandbox"


# ------------------------------------------------------- status handling


def test_unverified_scope_is_refused_by_default():
    from logic.places import _finish
    with pytest.raises(UnusableBoundaryError) as exc:
        _finish("48477", result(status="no_entries"), "geoid", "48477", True)
    assert exc.value.status == "no_entries"


def test_unverified_scope_can_be_accepted_explicitly():
    from logic.places import _finish
    place = _finish("48477", result(status="unavailable"), "geoid", "48477", False)
    assert place.status == "unavailable"


def test_ok_scope_passes():
    from logic.places import _finish
    place = _finish("48477", result(), "geoid", "48477", True)
    assert place.geoid == "48477"
    assert len(place.eccs) == 1


# ------------------------------------------------------------ describing


def test_a_county_match_reads_plainly():
    place = ResolvedPlace(query="Washington County, TX", geoid="48477",
                          county_name="Washington", state="TX",
                          matched_as="county", matched_name="Washington County")
    assert str(place) == "Washington County, TX (48477)"


def test_a_city_match_says_how_it_got_there():
    place = ResolvedPlace(query="Lincoln, NE", geoid="31109",
                          county_name="Lancaster", state="NE",
                          matched_as="city", matched_name="Lincoln")
    assert str(place) == "Lancaster County, NE (31109) via city 'Lincoln'"


# ------------------------------------------------------ ring normalisation


def test_rings_are_closed():
    from logic.places import _fix_multipolygon
    open_ring = [[[[0, 0], [1, 0], [1, 1], [0, 1]]]]
    fixed = _fix_multipolygon(open_ring)
    assert fixed[0][0][0] == fixed[0][0][-1]


def test_exterior_rings_wind_counterclockwise():
    from logic.places import _fix_multipolygon, _signed_area
    clockwise = [[[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]]
    fixed = _fix_multipolygon(clockwise)
    assert _signed_area(fixed[0][0][:-1]) > 0


def test_holes_wind_clockwise():
    from logic.places import _fix_multipolygon, _signed_area
    poly = [[
        [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],          # exterior
        [[1, 1], [2, 1], [2, 2], [1, 2], [1, 1]],          # hole, given CCW
    ]]
    fixed = _fix_multipolygon(poly)
    assert _signed_area(fixed[0][0][:-1]) > 0
    assert _signed_area(fixed[0][1][:-1]) < 0


def test_coordinates_are_rounded_to_six_places():
    from logic.places import _fix_multipolygon
    precise = [[[[-96.7945521234, 30.1605459876], [-96.08, 30.04],
                 [-96.08, 30.39], [-96.7945521234, 30.1605459876]]]]
    fixed = _fix_multipolygon(precise)
    assert fixed[0][0][0] == [-96.794552, 30.160546]


def test_already_correct_geometry_is_unchanged():
    from logic.places import _fix_multipolygon
    good = [[[[-96.79, 30.04], [-96.08, 30.04], [-96.08, 30.39],
              [-96.79, 30.39], [-96.79, 30.04]]]]
    assert _fix_multipolygon(good) == good


# ------------------------------------------- county / city name collisions
#
# Minimal stand-ins for the GeoDataFrames: only the operations the module
# performs are supported, so geopandas is not needed to test the logic.


class Row(dict):
    def __getitem__(self, key):
        return dict.__getitem__(self, key)


class Column:
    def __init__(self, values):
        self.values = list(values)

    def __eq__(self, other):
        return [v == other for v in self.values]

    def map(self, fn):
        return Column([fn(v) for v in self.values])

    @property
    def str(self):
        return self

    def casefold(self):
        return Column([str(v).casefold() for v in self.values])

    def __iter__(self):
        return iter(self.values)


class Table:
    """Just enough of a GeoDataFrame for _match_county and _label."""

    def __init__(self, rows):
        self.rows = [Row(r) for r in rows]

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, key):
        if isinstance(key, list):
            return Table([r for r, keep in zip(self.rows, key) if keep])
        return Column([r.get(key) for r in self.rows])

    @property
    def iloc(self):
        return self.rows

    @property
    def columns(self):
        return list(self.rows[0].keys()) if self.rows else []


NE_COUNTIES = Table([
    {"GEOID": "31111", "NAME": "Lincoln", "NAMELSAD": "Lincoln County", "STUSPS": "NE"},
    {"GEOID": "31109", "NAME": "Lancaster", "NAMELSAD": "Lancaster County", "STUSPS": "NE"},
])


def test_lincoln_nebraska_is_refused_as_ambiguous(monkeypatch):
    """The CITY of Lincoln is in LANCASTER county; Lincoln County is elsewhere.

    Silently preferring either one gets the other case wrong, so both are
    reported and the caller picks.
    """
    import logic.places as places_mod
    monkeypatch.setattr(places_mod, "_ecc_lookup", lambda: object())
    monkeypatch.setattr(places_mod, "_match_city",
                        lambda place, state, places, counties: [("Lincoln", "31109")])

    with pytest.raises(AmbiguousPlaceError) as exc:
        places_mod.resolve_place("Lincoln, Nebraska", counties=NE_COUNTIES,
                                 places=object())

    text = str(exc.value)
    assert "Lincoln County, NE (31111)" in text
    assert "Lancaster County, NE (31109)" in text
    assert "contains the city of Lincoln" in text


def test_a_county_with_no_same_named_city_resolves(monkeypatch):
    import logic.places as places_mod

    class FakeEcc:
        @staticmethod
        def lookup_county(geoid, **kw):
            return result(geoid="31109", name="Lancaster", state="NE")

    monkeypatch.setattr(places_mod, "_ecc_lookup", lambda: FakeEcc)
    monkeypatch.setattr(places_mod, "_match_city",
                        lambda place, state, places, counties: [])

    place = places_mod.resolve_place("Lancaster County, NE", counties=NE_COUNTIES,
                                     places=object())
    assert place.geoid == "31109"
    assert place.matched_as == "county"


def test_a_city_inside_its_own_county_is_not_ambiguous(monkeypatch):
    """Washington, TX: the city sits inside Washington County -- same GEOID."""
    import logic.places as places_mod

    tx = Table([{"GEOID": "48477", "NAME": "Washington",
                 "NAMELSAD": "Washington County", "STUSPS": "TX"}])

    class FakeEcc:
        @staticmethod
        def lookup_county(geoid, **kw):
            return result()

    monkeypatch.setattr(places_mod, "_ecc_lookup", lambda: FakeEcc)
    monkeypatch.setattr(places_mod, "_match_city",
                        lambda place, state, places, counties: [("Washington", "48477")])

    place = places_mod.resolve_place("Washington, TX", counties=tx, places=object())
    assert place.geoid == "48477"


def test_a_city_only_match_resolves_to_its_county(monkeypatch):
    import logic.places as places_mod

    class FakeEcc:
        @staticmethod
        def lookup_county(geoid, **kw):
            return result(geoid="31109", name="Lancaster", state="NE")

    monkeypatch.setattr(places_mod, "_ecc_lookup", lambda: FakeEcc)
    monkeypatch.setattr(places_mod, "_match_city",
                        lambda place, state, places, counties: [("Omaha", "31109")])

    place = places_mod.resolve_place("Omaha, NE", counties=NE_COUNTIES, places=object())
    assert place.geoid == "31109"
    assert place.matched_as == "city"


def test_two_counties_of_the_same_name_are_refused(monkeypatch):
    import logic.places as places_mod
    monkeypatch.setattr(places_mod, "_ecc_lookup", lambda: object())
    twins = Table([
        {"GEOID": "11111", "NAME": "Union", "NAMELSAD": "Union County", "STUSPS": "NE"},
        {"GEOID": "22222", "NAME": "Union", "NAMELSAD": "Union County", "STUSPS": "NE"},
    ])
    with pytest.raises(AmbiguousPlaceError):
        places_mod.resolve_place("Union County, NE", counties=twins, places=object())