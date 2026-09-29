"""Tests for jurisdiction activation via revisions."""

from __future__ import annotations

import datetime as dt

import pytest

from logic.revisions import (
    AuthorityNotPendingError,
    NothingToPublishError,
    OtherAuthoritiesPendingError,
    PendingRevision,
    PublishNotConfirmedError,
    activate_jurisdiction,
    default_revision_number,
    get_pending,
)


def jurisdiction(authority_id, jid=4261, ingress=2, egress=3):
    return {
        "authority_id": authority_id,
        "id": jid,
        "ingress_status": ingress,
        "egress_status": egress,
        "shapes": [{"id": 1}],
        "authority": {"id": authority_id, "name": "gDTest"},
    }


def revision(rid, *, number=None, date=None, created=(), modified=(), deleted=()):
    return {
        "id": rid,
        "revision_number": number,
        "revision_date": date,
        "created": list(created),
        "modified": list(modified),
        "deleted": list(deleted),
    }


class FakeClient:
    """Serves a queue of pending states; each GET of pending advances it."""

    def __init__(self, pending_states):
        self._states = list(pending_states)
        self.posts = []

    def get(self, path):
        if path.endswith("/pending"):
            return self._states[0] if len(self._states) == 1 else self._states.pop(0)
        return revision("0")

    def post(self, path, json=None):
        self.posts.append((path, json))
        return None


# --------------------------------------------------------------- numbering


def test_revision_number_is_day_then_padded_month():
    assert default_revision_number(dt.date(2026, 9, 2)) == 209
    assert default_revision_number(dt.date(2026, 9, 22)) == 2209
    assert default_revision_number(dt.date(2026, 12, 5)) == 512


# ----------------------------------------------------------------- parsing


def test_authority_ids_dedupes_across_all_three_lists():
    rev = PendingRevision.from_api(
        revision("1986",
                 created=[jurisdiction(4958)],
                 modified=[jurisdiction(4958, jid=9), jurisdiction(5001)],
                 deleted=[jurisdiction(5002)])
    )
    assert rev.authority_ids == ["4958", "5001", "5002"]
    assert len(rev.entries) == 4
    assert len(rev.entries_for("4958")) == 2


def test_empty_revision_is_detected():
    assert PendingRevision.from_api(revision("1986")).is_empty


def test_get_pending_parses_the_real_shape():
    client = FakeClient([revision("1986", modified=[jurisdiction(4958)])])
    pending = get_pending(client)
    assert pending.id == "1986"
    assert pending.revision_number is None
    assert pending.authority_ids == ["4958"]


# ------------------------------------------------------------- happy path


def test_activates_and_confirms_by_id_change():
    client = FakeClient([
        revision("1986", modified=[jurisdiction(4958)]),          # before
        revision("2019", modified=[jurisdiction(4958)]),          # after publish
    ])
    result = activate_jurisdiction(client, "4958", revision_date=dt.date(2026, 9, 22))

    assert [p[0] for p in client.posts] == [
        "/v1/andromeda/revisions/pending",
        "/v1/andromeda/revisions/active",
    ]
    assert client.posts[0][1] == {"revision_number": 2209, "revision_date": "2026-09-22"}
    assert client.posts[1][1] is None          # publish sends no body
    assert result.published_revision_id == "1986"
    assert result.next_pending_id == "2019"
    assert result.revision_number == 2209


def test_queue_not_emptying_is_not_a_failure():
    """The same jurisdiction reappears in the new revision -- observed behaviour."""
    client = FakeClient([
        revision("1986", modified=[jurisdiction(4958)]),
        revision("2019", modified=[jurisdiction(4958)]),   # still there
    ])
    result = activate_jurisdiction(client, "4958")
    assert result.next_pending_id == "2019"


def test_explicit_revision_number_wins():
    client = FakeClient([
        revision("1986", modified=[jurisdiction(4958)]),
        revision("2019"),
    ])
    activate_jurisdiction(client, "4958", revision_number=777, revision_date="2026-01-05")
    assert client.posts[0][1] == {"revision_number": 777, "revision_date": "2026-01-05"}


# ----------------------------------------------------------------- guards


def test_refuses_when_nothing_is_pending():
    client = FakeClient([revision("1986")])
    with pytest.raises(NothingToPublishError):
        activate_jurisdiction(client, "4958")
    assert client.posts == []


def test_refuses_when_this_authority_has_nothing_pending():
    client = FakeClient([revision("1986", modified=[jurisdiction(5001)])])
    with pytest.raises(AuthorityNotPendingError) as exc:
        activate_jurisdiction(client, "4958")
    assert exc.value.present == ["5001"]
    assert client.posts == []


def test_refuses_when_other_authorities_are_in_the_batch():
    """Publishing is environment-wide; do not activate someone else's work."""
    client = FakeClient([
        revision("1986", modified=[jurisdiction(4958), jurisdiction(5001)])
    ])
    with pytest.raises(OtherAuthoritiesPendingError) as exc:
        activate_jurisdiction(client, "4958")
    assert exc.value.others == ["5001"]
    assert client.posts == []


def test_other_authorities_can_be_allowed_explicitly():
    client = FakeClient([
        revision("1986", modified=[jurisdiction(4958), jurisdiction(5001)]),
        revision("2019"),
    ])
    result = activate_jurisdiction(client, "4958", allow_other_authorities=True)
    assert result.authority_ids == ["4958", "5001"]
    assert len(client.posts) == 2


def test_publish_without_effect_is_reported():
    client = FakeClient([
        revision("1986", modified=[jurisdiction(4958)]),
        revision("1986", modified=[jurisdiction(4958)]),   # id unchanged
    ])
    with pytest.raises(PublishNotConfirmedError):
        activate_jurisdiction(client, "4958")


def test_dry_run_writes_nothing():
    client = FakeClient([revision("1986", modified=[jurisdiction(4958)])])
    result = activate_jurisdiction(client, "4958", dry_run=True,
                                   revision_date=dt.date(2026, 9, 22))
    assert client.posts == []
    assert result.published_revision_id == "1986"
    assert result.next_pending_id == ""


def test_prefetched_pending_is_reused_not_refetched():
    """The harness reads pending for display; activate must not read it again."""
    client = FakeClient([
        revision("1986", modified=[jurisdiction(4958)]),   # the prefetch
        revision("2019", modified=[jurisdiction(4958)]),   # the post-publish read
    ])
    pending = get_pending(client)
    result = activate_jurisdiction(client, "4958", pending=pending)
    assert result.published_revision_id == "1986"
    assert result.next_pending_id == "2019"


# ------------------------------------------------- revision number conflicts


class ConflictingClient(FakeClient):
    """Rejects the given revision numbers the way Andromeda does."""

    def __init__(self, pending_states, taken):
        super().__init__(pending_states)
        self.taken = set(taken)
        self.attempted = []

    def post(self, path, json=None):
        if path.endswith("/pending"):
            number = json["revision_number"]
            self.attempted.append(number)
            if number in self.taken:
                raise RuntimeError(
                    f'409 on POST {path}: {{"detail":"GeofenceRevision record with '
                    f'revision_number {number} already exists."}}'
                )
        return super().post(path, json)


def test_candidate_numbers_keep_the_date_prefix():
    from logic.revisions import revision_number_candidates
    assert list(revision_number_candidates(2209, 4)) == [2209, 220901, 220902, 220903]


def test_conflict_detection_matches_the_real_message():
    from logic.revisions import is_number_conflict
    assert is_number_conflict(RuntimeError(
        '409 on POST /x: {"detail":"GeofenceRevision record with '
        'revision_number 2209 already exists."}'))
    assert not is_number_conflict(RuntimeError("500 Internal Server Error"))


def test_retries_past_a_taken_number():
    client = ConflictingClient(
        [revision("1986", modified=[jurisdiction(4958)]), revision("2019")],
        taken={2209},
    )
    result = activate_jurisdiction(client, "4958", revision_date="2026-09-22")
    assert client.attempted == [2209, 220901]
    assert result.revision_number == 220901


def test_retries_past_several_taken_numbers():
    client = ConflictingClient(
        [revision("1986", modified=[jurisdiction(4958)]), revision("2019")],
        taken={2209, 220901, 220902},
    )
    result = activate_jurisdiction(client, "4958", revision_date="2026-09-22")
    assert result.revision_number == 220903


def test_an_explicit_number_is_also_retried_from():
    client = ConflictingClient(
        [revision("1986", modified=[jurisdiction(4958)]), revision("2019")],
        taken={777},
    )
    result = activate_jurisdiction(client, "4958", revision_number=777)
    assert client.attempted == [777, 77701]
    assert result.revision_number == 77701


def test_non_conflict_errors_are_not_retried():
    class Broken(FakeClient):
        def post(self, path, json=None):
            raise RuntimeError("500 Internal Server Error")

    client = Broken([revision("1986", modified=[jurisdiction(4958)])])
    with pytest.raises(RuntimeError, match="500"):
        activate_jurisdiction(client, "4958")


def test_exhausting_every_candidate_raises():
    from logic.revisions import RevisionNumberConflictError
    client = ConflictingClient(
        [revision("1986", modified=[jurisdiction(4958)])],
        taken={2209} | {220900 + n for n in range(1, 20)},
    )
    with pytest.raises(RevisionNumberConflictError) as exc:
        activate_jurisdiction(client, "4958", revision_date="2026-09-22")
    assert len(exc.value.tried) == 20


def test_retry_can_be_disabled():
    client = ConflictingClient(
        [revision("1986", modified=[jurisdiction(4958)])], taken={2209})
    with pytest.raises(RuntimeError, match="409"):
        activate_jurisdiction(client, "4958", revision_date="2026-09-22",
                              retry_on_conflict=False)
    assert client.attempted == [2209]