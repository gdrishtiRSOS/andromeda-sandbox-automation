"""Attach a jurisdiction boundary to an authority.

Runbook step 4. There is no file-upload endpoint -- the browser parses the
boundary client-side and submits it inline, so this module reads a GeoJSON
file and posts its geometry.

    POST  /v1/andromeda/authorities/{id}/jurisdictions
          {ingress_status: 1, egress_status: 3, exact_polygon: <FeatureCollection>}
          -> 201 {id, authority_id, ingress_status, egress_status,
                  exact_polygon, expanded_polygon, shapes: [...]}
    PATCH /v1/andromeda/authorities/{id}/jurisdictions/{jurisdictionId}
          {id, ingress_status: 2, egress_status: 3} -> 200

Design notes
------------
* Two calls, not one. The runbook creates the jurisdiction as Verified and
  then sets it back to Pending. Both are needed: creating it Pending outright
  has not been observed, and the intermediate Verified state is what the UI
  does. The status lifecycle is 1 Verified -> 2 Pending -> 3 Active, the last
  reached only by publishing a revision (runbook step 7).

* Nothing takes effect on the authority until that revision is published, so
  creating a jurisdiction is safe in isolation and an existing jurisdiction is
  not an obstacle -- Andromeda handles overlap itself.

* Boundary files live in `andromeda/data/` alongside the standard capability
  set, so a boundary can be named rather than pathed: "dublin" finds
  data/dublin.geojson. A full path still works.

* The geometry is validated before posting. Coordinates outside longitude
  +/-180 or latitude +/-90 mean the file is in a projected coordinate system
  rather than WGS84, which the API would accept and place nowhere useful.
  Better to fail on the file than to debug a boundary in the wrong hemisphere.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol, runtime_checkable

log = logging.getLogger(__name__)

__all__ = [
    "DATA_DIR",
    "EgressStatus",
    "IngressStatus",
    "Jurisdiction",
    "JurisdictionClient",
    "JurisdictionError",
    "InvalidGeometryError",
    "attach_jurisdiction",
    "available_boundaries",
    "resolve_boundary",
    "create_jurisdiction",
    "list_jurisdictions",
    "load_geojson",
    "set_status",
]


class IngressStatus(IntEnum):
    VERIFIED = 1
    PENDING = 2
    ACTIVE = 3


class EgressStatus(IntEnum):
    ACTIVE = 3


class JurisdictionError(Exception):
    """Base for this module."""


class InvalidGeometryError(JurisdictionError):
    """The GeoJSON is unusable as a boundary."""


#: Boundary files live beside the standard capability set.
DATA_DIR = Path(__file__).parent / "data"


def available_boundaries(data_dir: Path | None = None) -> list[Path]:
    """Every .geojson file in the data directory, sorted by name."""
    directory = data_dir or DATA_DIR
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.geojson"))


def resolve_boundary(name: str | Path, data_dir: Path | None = None) -> Path:
    """Find a boundary file by name, path, or bare name without the extension.

    Tried in order: the value as given (so an explicit path still works), then
    `data/<name>`, then `data/<name>.geojson`.

    Raises
    ------
    JurisdictionError
        Nothing matched; the message lists what is in the data directory.
    """
    directory = data_dir or DATA_DIR
    candidates = [Path(name), directory / name, directory / f"{name}.geojson"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    known = available_boundaries(directory)
    if known:
        listing = "\n  ".join(p.stem for p in known)
        raise JurisdictionError(
            f"no boundary file matching {name!r}.\nAvailable in {directory}:\n  {listing}"
        )
    raise JurisdictionError(
        f"no boundary file matching {name!r}, and {directory} holds no .geojson files. "
        "Put the processed boundary there, or pass a full path."
    )


@dataclass(frozen=True)
class Jurisdiction:
    id: str
    authority_id: str
    ingress_status: int
    egress_status: int
    shapes: list[dict[str, Any]] = field(default_factory=list)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "Jurisdiction":
        return cls(
            id=str(data["id"]),
            authority_id=str(data.get("authority_id", "")),
            ingress_status=data.get("ingress_status", 0),
            egress_status=data.get("egress_status", 0),
            shapes=list(data.get("shapes") or []),
            raw=data,
        )

    @property
    def ingress_label(self) -> str:
        try:
            return IngressStatus(self.ingress_status).name.title()
        except ValueError:
            return str(self.ingress_status)

    def __str__(self) -> str:
        return (
            f"jurisdiction {self.id} on authority {self.authority_id} "
            f"(ingress {self.ingress_label}, egress {self.egress_status}, "
            f"{len(self.shapes)} shape(s))"
        )


@runtime_checkable
class JurisdictionClient(Protocol):
    def get(self, path: str) -> Any: ...

    def post(self, path: str, json: Mapping[str, Any]) -> Any: ...

    def patch(self, path: str, json: Mapping[str, Any]) -> Any: ...


# -------------------------------------------------------------- geometry


def _iter_positions(node: Any) -> Iterator[tuple[float, float]]:
    """Yield every [lon, lat] pair anywhere in a GeoJSON structure.

    Walks dicts as well as lists, so it works on a whole FeatureCollection,
    a single Feature, a geometry, or a bare coordinate array.
    """
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key in ("bbox", "crs"):   # not part of the geometry itself
                continue
            yield from _iter_positions(value)
    elif isinstance(node, (list, tuple)):
        if len(node) >= 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                  for v in node[:2]):
            yield float(node[0]), float(node[1])
        else:
            for child in node:
                yield from _iter_positions(child)


def bbox(polygon: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    """(min_lon, min_lat, max_lon, max_lat), or None if there are no positions."""
    lons, lats = [], []
    for lon, lat in _iter_positions(polygon):
        lons.append(lon)
        lats.append(lat)
    if not lons:
        return None
    return min(lons), min(lats), max(lons), max(lats)


def normalize_geojson(doc: Any) -> dict[str, Any]:
    """Coerce a Feature, geometry or FeatureCollection into a FeatureCollection."""
    if not isinstance(doc, Mapping):
        raise InvalidGeometryError("GeoJSON must be an object")

    kind = doc.get("type")
    if kind == "FeatureCollection":
        features = doc.get("features")
        if not isinstance(features, list) or not features:
            raise InvalidGeometryError("FeatureCollection has no features")
        return dict(doc)
    if kind == "Feature":
        return {"type": "FeatureCollection", "features": [dict(doc)]}
    if kind in ("Polygon", "MultiPolygon", "GeometryCollection"):
        return {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {}, "geometry": dict(doc)}],
        }
    raise InvalidGeometryError(
        f"unsupported GeoJSON type {kind!r}; expected FeatureCollection, Feature, "
        "Polygon or MultiPolygon"
    )


def validate_geojson(polygon: Mapping[str, Any]) -> None:
    """Reject geometry that would post successfully but mean nothing.

    Raises
    ------
    InvalidGeometryError
        No coordinates, or coordinates outside WGS84 bounds.
    """
    box = bbox(polygon)
    if box is None:
        raise InvalidGeometryError("no coordinates found in the geometry")

    min_lon, min_lat, max_lon, max_lat = box
    if not (-180 <= min_lon <= 180 and -180 <= max_lon <= 180):
        raise InvalidGeometryError(
            f"longitude out of range ({min_lon:.1f}..{max_lon:.1f}). The file is "
            "probably in a projected coordinate system, not WGS84 lon/lat."
        )
    if not (-90 <= min_lat <= 90 and -90 <= max_lat <= 90):
        raise InvalidGeometryError(
            f"latitude out of range ({min_lat:.1f}..{max_lat:.1f}). The file is "
            "probably in a projected coordinate system, or lon/lat are swapped."
        )
    if min_lon == max_lon and min_lat == max_lat:
        raise InvalidGeometryError("the geometry is a single point, not an area")


def load_geojson(path: str | Path, data_dir: Path | None = None) -> dict[str, Any]:
    """Read a boundary file and return a validated FeatureCollection.

    `path` may be a full path or the name of a file in the data directory,
    with or without the .geojson extension.
    """
    path = resolve_boundary(path, data_dir)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InvalidGeometryError(f"{path} is not valid JSON: {exc}") from None

    polygon = normalize_geojson(doc)
    validate_geojson(polygon)

    box = bbox(polygon)
    log.info(
        "loaded %s: %d feature(s), bbox lon %.4f..%.4f lat %.4f..%.4f",
        path.name, len(polygon["features"]), box[0], box[2], box[1], box[3],
    )
    return polygon


# ----------------------------------------------------------------- calls


def _path(authority_id: str, jurisdiction_id: str | None = None) -> str:
    base = f"/v1/andromeda/authorities/{authority_id}/jurisdictions"
    return f"{base}/{jurisdiction_id}" if jurisdiction_id else base


def list_jurisdictions(client: JurisdictionClient, authority_id: str) -> list[Jurisdiction]:
    data = client.get(_path(authority_id))
    return [Jurisdiction.from_api(item) for item in data or []]


def create_jurisdiction(
    client: JurisdictionClient,
    authority_id: str,
    polygon: Mapping[str, Any],
    *,
    ingress_status: IngressStatus = IngressStatus.VERIFIED,
    egress_status: EgressStatus = EgressStatus.ACTIVE,
) -> Jurisdiction:
    """Create the jurisdiction with the boundary inline."""
    body = {
        "ingress_status": int(ingress_status),
        "egress_status": int(egress_status),
        "exact_polygon": dict(polygon),
    }
    data = client.post(_path(authority_id), body)
    jurisdiction = Jurisdiction.from_api(data)
    log.info("created %s", jurisdiction)
    return jurisdiction


def set_status(
    client: JurisdictionClient,
    authority_id: str,
    jurisdiction_id: str,
    *,
    ingress_status: IngressStatus,
    egress_status: EgressStatus = EgressStatus.ACTIVE,
) -> Jurisdiction:
    """Change a jurisdiction's ingress/egress status."""
    body = {
        "id": int(jurisdiction_id) if str(jurisdiction_id).isdigit() else jurisdiction_id,
        "ingress_status": int(ingress_status),
        "egress_status": int(egress_status),
    }
    data = client.patch(_path(authority_id, jurisdiction_id), body)
    jurisdiction = Jurisdiction.from_api(data)
    log.info("set %s", jurisdiction)
    return jurisdiction


# --------------------------------------------------------- orchestration


def attach_jurisdiction(
    client: JurisdictionClient,
    authority_id: str,
    geojson_path: str | Path | None = None,
    *,
    polygon: Mapping[str, Any] | None = None,
    dry_run: bool = False,
) -> Jurisdiction:
    """Create the jurisdiction, then set ingress to Pending -- runbook step 4.

    Pass either `geojson_path` or an already-loaded `polygon`.

    Nothing reaches the authority until a revision is published (step 7), so
    this is safe to run against an authority that already has a jurisdiction.

    Raises
    ------
    JurisdictionError
        Neither a path nor a polygon was given, or the file is unreadable.
    InvalidGeometryError
        The geometry is empty or not in WGS84 lon/lat.
    """
    if polygon is None:
        if geojson_path is None:
            raise JurisdictionError("pass geojson_path or polygon")
        polygon = load_geojson(geojson_path)
    else:
        polygon = normalize_geojson(polygon)
        validate_geojson(polygon)

    existing = list_jurisdictions(client, authority_id)
    if existing:
        log.info(
            "authority %s already has %d jurisdiction(s): %s",
            authority_id, len(existing), ", ".join(j.id for j in existing),
        )

    if dry_run:
        box = bbox(polygon)
        log.info(
            "dry run: would create a jurisdiction on authority %s "
            "(ingress Verified -> Pending, egress Active), bbox %s",
            authority_id, tuple(round(v, 4) for v in box),
        )
        return Jurisdiction(
            id="", authority_id=str(authority_id),
            ingress_status=int(IngressStatus.PENDING),
            egress_status=int(EgressStatus.ACTIVE),
        )

    created = create_jurisdiction(client, authority_id, polygon)
    final = set_status(
        client, authority_id, created.id, ingress_status=IngressStatus.PENDING
    )

    if final.ingress_status != int(IngressStatus.PENDING):
        log.warning(
            "expected ingress Pending after the update, got %s", final.ingress_label
        )

    log.info(
        "jurisdiction %s ready; publish a revision to activate it (runbook step 7)",
        final.id,
    )
    return final
