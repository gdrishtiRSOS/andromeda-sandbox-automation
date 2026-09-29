"""Place lookup for the page: Census frames loaded once, then reused.

`resolve_place` reads the county and place shapefiles on every call unless it
is handed pre-loaded frames. A form that resolves on blur would pay that each
time, so the frames are loaded once -- in the background at startup -- and
passed in. Loading uses the same private helpers `resolve_place` itself uses.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

__all__ = ["PlaceLookup", "candidate_choices", "place_summary"]

VINTAGE = 2025
_GEOID_IN_LABEL = re.compile(r"\((\d{5})\)")


class PlaceLookup:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counties: Any = None
        self._places: Any = None
        self.state = "not loaded"          # not loaded | loading | ready | unavailable
        self.problem: Optional[str] = None

    def warm(self) -> None:
        """Load in a background thread so the first lookup is quick."""
        threading.Thread(target=self._load, name="place-warmup", daemon=True).start()

    def _load(self) -> None:
        with self._lock:
            if self.state in ("ready", "unavailable"):
                return
            self.state = "loading"
            try:
                from logic.places import _ecc_lookup, _load_places
                ecc = _ecc_lookup()
                self._counties = ecc._load_counties(VINTAGE, None)
                try:
                    self._places = _load_places(VINTAGE, None)
                except Exception as exc:  # cities unavailable; counties still work
                    log.warning("Census places file unavailable: %s", exc)
                self.state = "ready"
            except Exception as exc:
                self.state = "unavailable"
                self.problem = str(exc)
                log.warning("place lookup unavailable: %s", exc)

    def resolve(self, query: str, *, allow_unverified: bool = False):
        from logic.places import resolve_place

        self._load()
        return resolve_place(
            query, require_status=not allow_unverified,
            counties=self._counties, places=self._places,
        )


def candidate_choices(candidates: List[str]) -> List[Dict[str, Optional[str]]]:
    """AmbiguousPlaceError's labels, each with the GEOID that selects it.

    Every label the resolver builds ends "(<5-digit GEOID>)"; one without it
    is shown but cannot be chosen.
    """
    out = []
    for label in candidates:
        match = _GEOID_IN_LABEL.search(label)
        out.append({"label": label, "geoid": match.group(1) if match else None})
    return out


def place_summary(place: Any) -> Dict[str, Any]:
    """What the page shows for a resolved place, plus the polygon to attach."""
    from logic.jurisdictions import bbox
    from logic.places import account_fields, to_andromeda_polygon

    polygon = to_andromeda_polygon(place)
    fields = account_fields(place)
    return {
        "source": "place",
        "label": str(place),
        "query": place.query,
        "geoid": place.geoid,
        "county": place.county_name,
        "state": place.state,
        "matched_as": place.matched_as,
        "matched_name": place.matched_name,
        "scope_status": place.status,
        "eccs": [{"name": e.get("name"), "fcc_psap_id": e.get("fcc_psap_id")}
                 for e in place.eccs],
        "bbox": bbox(polygon),
        "fields": {"account_id": fields["account_id"], "country": fields["country"],
                   "state": fields["state"]},
        "polygon": polygon,
    }
