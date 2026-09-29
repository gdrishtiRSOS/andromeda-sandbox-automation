"""Tests for attaching a jurisdiction boundary."""

from __future__ import annotations

import json

import pytest

from logic.jurisdictions import (
    EgressStatus,
    IngressStatus,
    InvalidGeometryError,
    Jurisdiction,
    JurisdictionError,
    attach_jurisdiction,
    available_boundaries,
    bbox,
    load_geojson,
    normalize_geojson,
    resolve_boundary,
    validate_geojson,
)

SQUARE = [[[-6.3, 53.3], [-6.2, 53.3], [-6.2, 53.4], [-6.3, 53.4], [-6.3, 53.3]]]

GEOMETRY = {"type": "Polygon", "coordinates": SQUARE}
FEATURE = {"type": "Feature", "properties": {"name": "Dublin"}, "geometry": GEOMETRY}
COLLECTION = {"type": "FeatureCollection", "features": [FEATURE]}


class FakeClient:
    def __init__(self, existing=()):
        self._existing = list(existing)
        self.posted = None
        self.patched = None
        self._next_id = 3799

    def get(self, path):
        return self._existing

    def post(self, path, json):
        self.posted = (path, json)
        return {
            "id": self._next_id,
            "authority_id": 4958,
            "ingress_status": json["ingress_status"],
            "egress_status": json["egress_status"],
            "exact_polygon": json["exact_polygon"],
            "expanded_polygon": json["exact_polygon"],
            "shapes": [{"top_left": [-6.3, 53.4], "bottom_right": [-6.2, 53.3]}],
        }

    def patch(self, path, json):
        self.patched = (path, json)
        return {
            "id": json["id"],
            "authority_id": 4958,
            "ingress_status": json["ingress_status"],
            "egress_status": json["egress_status"],
            "shapes": [],
        }


# ------------------------------------------------------------- geometry


def test_feature_collection_passes_through():
    assert normalize_geojson(COLLECTION)["features"] == [FEATURE]


def test_bare_feature_is_wrapped():
    out = normalize_geojson(FEATURE)
    assert out["type"] == "FeatureCollection"
    assert out["features"] == [FEATURE]


def test_bare_geometry_is_wrapped():
    out = normalize_geojson(GEOMETRY)
    assert out["features"][0]["geometry"] == GEOMETRY


def test_empty_collection_is_rejected():
    with pytest.raises(InvalidGeometryError):
        normalize_geojson({"type": "FeatureCollection", "features": []})


def test_unsupported_type_is_rejected():
    with pytest.raises(InvalidGeometryError, match="unsupported"):
        normalize_geojson({"type": "Point", "coordinates": [-6.3, 53.3]})


def test_bbox_walks_nested_coordinates():
    assert bbox(COLLECTION) == (-6.3, 53.3, -6.2, 53.4)


def test_projected_coordinates_are_rejected():
    """State-plane feet, not lon/lat -- would post fine and mean nothing."""
    projected = {"type": "Polygon",
                 "coordinates": [[[2103458.1, 731234.5], [2103999.0, 731234.5],
                                  [2103999.0, 731999.0], [2103458.1, 731234.5]]]}
    with pytest.raises(InvalidGeometryError, match="longitude out of range"):
        validate_geojson(normalize_geojson(projected))


def test_swapped_lat_lon_is_caught_when_out_of_range():
    swapped = {"type": "Polygon",
               "coordinates": [[[53.3, -186.0], [53.4, -186.0], [53.4, -185.0],
                                [53.3, -186.0]]]}
    with pytest.raises(InvalidGeometryError):
        validate_geojson(normalize_geojson(swapped))


def test_degenerate_point_is_rejected():
    point = {"type": "Polygon",
             "coordinates": [[[-6.3, 53.3], [-6.3, 53.3], [-6.3, 53.3]]]}
    with pytest.raises(InvalidGeometryError, match="single point"):
        validate_geojson(normalize_geojson(point))


# ----------------------------------------------------------------- files


def test_load_geojson_reads_and_normalizes(tmp_path):
    path = tmp_path / "boundary.geojson"
    path.write_text(json.dumps(FEATURE))
    assert load_geojson(path)["type"] == "FeatureCollection"


def test_missing_file_is_a_clear_error(tmp_path):
    with pytest.raises(JurisdictionError, match="no boundary file matching"):
        load_geojson(tmp_path / "nope.geojson", data_dir=tmp_path)


# ------------------------------------------------------- data directory


def test_boundary_found_by_bare_name(tmp_path):
    (tmp_path / "dublin.geojson").write_text(json.dumps(COLLECTION))
    assert resolve_boundary("dublin", tmp_path).name == "dublin.geojson"
    assert resolve_boundary("dublin.geojson", tmp_path).name == "dublin.geojson"


def test_explicit_path_still_wins(tmp_path):
    elsewhere = tmp_path / "elsewhere.geojson"
    elsewhere.write_text(json.dumps(COLLECTION))
    assert resolve_boundary(str(elsewhere), tmp_path / "data") == elsewhere


def test_load_by_bare_name_from_the_data_dir(tmp_path):
    (tmp_path / "dublin.geojson").write_text(json.dumps(COLLECTION))
    assert load_geojson("dublin", data_dir=tmp_path)["type"] == "FeatureCollection"


def test_unknown_name_lists_what_is_available(tmp_path):
    (tmp_path / "dublin.geojson").write_text(json.dumps(COLLECTION))
    (tmp_path / "cork.geojson").write_text(json.dumps(COLLECTION))
    with pytest.raises(JurisdictionError) as exc:
        resolve_boundary("galway", tmp_path)
    assert "cork" in str(exc.value) and "dublin" in str(exc.value)


def test_empty_data_dir_says_so(tmp_path):
    with pytest.raises(JurisdictionError, match="no .geojson files"):
        resolve_boundary("anything", tmp_path)


def test_available_boundaries_is_sorted(tmp_path):
    for name in ("zed", "alpha", "mid"):
        (tmp_path / f"{name}.geojson").write_text(json.dumps(COLLECTION))
    (tmp_path / "notes.txt").write_text("ignore me")
    assert [p.stem for p in available_boundaries(tmp_path)] == ["alpha", "mid", "zed"]


def test_malformed_json_is_a_clear_error(tmp_path):
    path = tmp_path / "bad.geojson"
    path.write_text("{not json")
    with pytest.raises(InvalidGeometryError, match="not valid JSON"):
        load_geojson(path)


# ------------------------------------------------------------ the flow


def test_creates_verified_then_sets_pending():
    client = FakeClient()
    result = attach_jurisdiction(client, "4958", polygon=COLLECTION)

    post_path, post_body = client.posted
    assert post_path == "/v1/andromeda/authorities/4958/jurisdictions"
    assert post_body["ingress_status"] == IngressStatus.VERIFIED
    assert post_body["egress_status"] == EgressStatus.ACTIVE
    assert post_body["exact_polygon"] == COLLECTION

    patch_path, patch_body = client.patched
    assert patch_path == "/v1/andromeda/authorities/4958/jurisdictions/3799"
    assert patch_body == {"id": 3799, "ingress_status": 2, "egress_status": 3}

    assert result.id == "3799"
    assert result.ingress_status == IngressStatus.PENDING
    assert result.ingress_label == "Pending"


def test_reads_from_a_file(tmp_path):
    path = tmp_path / "boundary.geojson"
    path.write_text(json.dumps(COLLECTION))
    client = FakeClient()
    result = attach_jurisdiction(client, "4958", path)
    assert result.id == "3799"
    assert client.posted[1]["exact_polygon"]["features"][0]["properties"]["name"] == "Dublin"


def test_existing_jurisdiction_is_not_an_obstacle():
    """Andromeda handles overlap; nothing lands until the revision is published."""
    client = FakeClient(existing=[{"id": 4261, "authority_id": 4958,
                                   "ingress_status": 2, "egress_status": 3}])
    result = attach_jurisdiction(client, "4958", polygon=COLLECTION)
    assert result.id == "3799"
    assert client.posted is not None


def test_bad_geometry_never_reaches_the_api():
    client = FakeClient()
    with pytest.raises(InvalidGeometryError):
        attach_jurisdiction(client, "4958", polygon={"type": "Polygon",
                                                     "coordinates": [[[999, 999]]]})
    assert client.posted is None


def test_dry_run_writes_nothing():
    client = FakeClient()
    result = attach_jurisdiction(client, "4958", polygon=COLLECTION, dry_run=True)
    assert client.posted is None
    assert client.patched is None
    assert result.id == ""


def test_neither_path_nor_polygon_is_an_error():
    with pytest.raises(JurisdictionError, match="pass geojson_path or polygon"):
        attach_jurisdiction(FakeClient(), "4958")


def test_jurisdiction_str_is_readable():
    j = Jurisdiction(id="3799", authority_id="4958", ingress_status=2, egress_status=3)
    assert str(j) == ("jurisdiction 3799 on authority 4958 "
                      "(ingress Pending, egress 3, 0 shape(s))")


# ------------------------------------------------------------- exporting


class ListingClient(FakeClient):
    """Serves an authority's jurisdictions, optionally without the geometry."""

    def __init__(self, jurisdictions, *, geometry_in_list=True):
        super().__init__()
        self._jurisdictions = list(jurisdictions)
        self._geometry_in_list = geometry_in_list
        self.single_reads = []

    def get(self, path):
        if path.rstrip("/").endswith("/jurisdictions"):
            if self._geometry_in_list:
                return self._jurisdictions
            return [{k: v for k, v in j.items() if k != "exact_polygon"}
                    for j in self._jurisdictions]
        jid = path.rsplit("/", 1)[1]
        self.single_reads.append(jid)
        for j in self._jurisdictions:
            if str(j["id"]) == jid:
                return j
        raise AssertionError(jid)


def stored(jid=3799, polygon=None):
    return {"id": jid, "authority_id": 4958, "ingress_status": 3,
            "egress_status": 3, "shapes": [],
            "exact_polygon": polygon or COLLECTION}


def test_boundary_is_exported_as_a_feature_collection():
    from logic.jurisdictions import export_boundary
    polygon = export_boundary(ListingClient([stored()]), "4958")
    assert polygon["type"] == "FeatureCollection"
    assert polygon["features"][0]["geometry"] == GEOMETRY


def test_an_exported_boundary_can_be_posted_straight_back():
    """The round trip that makes copying between accounts work."""
    from logic.jurisdictions import export_boundary
    polygon = export_boundary(ListingClient([stored()]), "4958")

    target = FakeClient()
    attach_jurisdiction(target, "5001", polygon=polygon)
    assert target.posted[1]["exact_polygon"] == polygon


def test_geometry_is_fetched_individually_when_the_list_omits_it():
    from logic.jurisdictions import export_boundary
    client = ListingClient([stored()], geometry_in_list=False)
    polygon = export_boundary(client, "4958")
    assert client.single_reads == ["3799"]
    assert polygon["features"][0]["geometry"] == GEOMETRY


def test_a_specific_jurisdiction_can_be_named():
    from logic.jurisdictions import export_boundary
    other = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "Cork"},
         "geometry": {"type": "Polygon", "coordinates": [[
             [-8.5, 51.8], [-8.4, 51.8], [-8.4, 51.9], [-8.5, 51.8]]]}}]}
    client = ListingClient([stored(3799), stored(4000, other)])
    polygon = export_boundary(client, "4958", "4000")
    assert polygon["features"][0]["properties"]["name"] == "Cork"


def test_several_jurisdictions_without_a_choice_is_refused():
    from logic.jurisdictions import export_boundary
    client = ListingClient([stored(3799), stored(4000)])
    with pytest.raises(JurisdictionError, match="name one explicitly"):
        export_boundary(client, "4958")


def test_an_authority_with_no_jurisdiction_is_a_clear_error():
    from logic.jurisdictions import export_boundary
    with pytest.raises(JurisdictionError, match="no jurisdiction"):
        export_boundary(ListingClient([]), "4958")


def test_an_unknown_jurisdiction_id_lists_what_exists():
    from logic.jurisdictions import export_boundary
    client = ListingClient([stored(3799)])
    with pytest.raises(JurisdictionError, match="3799"):
        export_boundary(client, "4958", "9999")


def test_a_jurisdiction_without_geometry_is_reported():
    from logic.jurisdictions import export_boundary
    empty = {"id": 3799, "authority_id": 4958, "ingress_status": 3,
             "egress_status": 3, "shapes": []}
    client = ListingClient([empty], geometry_in_list=False)
    with pytest.raises(JurisdictionError, match="no exact_polygon"):
        export_boundary(client, "4958")