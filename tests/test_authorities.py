"""Tests for the Account Info step."""

from __future__ import annotations

import pytest

from logic.authorities import (
    AccountInfoError,
    UnknownCountryError,
    UnknownStateError,
    list_countries,
    list_states,
    update_account_info,
)

AUTHORITY = {
    "id": 4958,
    "name": "gDTest",
    "display_name": "gDTest",
    "account_id": "GD_0209",
    "dispatch_type": 1,
    "organization_id": 15516,
    "revision_introduced": {"id": 1656},
    "revision_last_modified": {"id": 1656},
    "attributes": {
        "cad_system": 0,
        "contact_email": "gdrishti+testtest@rapidsos.com",
        "contact_name": "Gerrard Drishti",
        "contact_phone": "12125551234",
        "contact_title": "abc",
        "country": "IRL",
        "mapping_system": 0,
        "non_emergency_phone": "12125551234",
        "phone_system": 0,
        "population": 123,
        "state": "LK",
    },
}

COUNTRIES = [{"code": "IRL", "name": "Ireland"}, {"code": "USA", "name": "United States"}]
STATES = {"IRL": [{"code": "LK", "name": "Limerick"}, {"code": "D", "name": "Dublin"}],
          "USA": [{"code": "TX", "name": "Texas"}]}


def fresh_authority():
    """A newly created authority: account_id not yet set, so it is writable."""
    import copy
    record = copy.deepcopy(AUTHORITY)
    record["account_id"] = None
    return record


class FakeClient:
    def __init__(self, authority=None, *, countries=COUNTRIES, states=STATES):
        import copy
        self.authority = copy.deepcopy(authority or AUTHORITY)
        self.countries = countries
        self.states = states
        self.put_body = None

    def get(self, path):
        if path == "/v1/andromeda/country":
            return self.countries
        if path.startswith("/v1/andromeda/country/"):
            return self.states.get(path.rsplit("/", 1)[1], [])
        return self.authority

    def put(self, path, json):
        self.put_body = json
        import copy
        stored = copy.deepcopy(json)
        stored["revision_introduced"] = None
        stored["revision_last_modified"] = None
        return stored


# ------------------------------------------------------------ catalogs


def test_country_catalog_is_parsed():
    assert list_countries(FakeClient()) == {"IRL": "Ireland", "USA": "United States"}


def test_state_catalog_is_parsed():
    assert list_states(FakeClient(), "IRL") == {"LK": "Limerick", "D": "Dublin"}


@pytest.mark.parametrize("payload,expected", [
    ([{"code": "IRL", "name": "Ireland"}], {"IRL": "Ireland"}),
    ([{"alpha3": "IRL", "display_name": "Ireland"}], {"IRL": "Ireland"}),
    ({"countries": [{"code": "IRL", "name": "Ireland"}]}, {"IRL": "Ireland"}),
    ({"IRL": "Ireland"}, {"IRL": "Ireland"}),
    (["IRL", "USA"], {"IRL": "IRL", "USA": "USA"}),
    ({"unexpected": {"deeply": "nested"}}, {}),
])
def test_catalog_shapes_are_handled(payload, expected):
    client = FakeClient(countries=payload)
    assert list_countries(client) == expected


# -------------------------------------------------------------- writes


def test_only_named_fields_change_and_the_rest_survive():
    client = FakeClient(fresh_authority())
    report = update_account_info(client, "4958", account_id="GD_NEW", state="D")

    body = client.put_body
    assert body["account_id"] == "GD_NEW"
    assert body["attributes"]["state"] == "D"
    # untouched fields preserved -- a PUT would otherwise blank them
    assert body["organization_id"] == 15516
    assert body["name"] == "gDTest"
    assert body["attributes"]["contact_email"] == "gdrishti+testtest@rapidsos.com"
    assert body["attributes"]["population"] == 123
    assert set(report.changed) == {"account_id", "state"}


def test_server_owned_revision_fields_are_not_sent_back():
    client = FakeClient(fresh_authority())
    update_account_info(client, "4958", account_id="GD_NEW")
    assert "revision_introduced" not in client.put_body
    assert "revision_last_modified" not in client.put_body


def test_attribute_fields_accepted_as_keywords():
    client = FakeClient()
    update_account_info(client, "4958", contact_name="Someone Else", population=5000)
    assert client.put_body["attributes"]["contact_name"] == "Someone Else"
    assert client.put_body["attributes"]["population"] == 5000


def test_dispatch_type_is_top_level_not_an_attribute():
    client = FakeClient()
    update_account_info(client, "4958", dispatch_type=2)
    assert client.put_body["dispatch_type"] == 2
    assert "dispatch_type" not in client.put_body["attributes"]


def test_unchanged_values_send_nothing():
    client = FakeClient()
    report = update_account_info(client, "4958", account_id="GD_0209", country="IRL")
    assert report.is_noop
    assert client.put_body is None


def test_none_values_are_ignored():
    client = FakeClient()
    report = update_account_info(client, "4958", account_id=None, state=None)
    assert report.is_noop


def test_unknown_field_is_rejected():
    client = FakeClient()
    with pytest.raises(AccountInfoError, match="unknown field"):
        update_account_info(client, "4958", nonsense="x")
    assert client.put_body is None


def test_dry_run_writes_nothing():
    client = FakeClient(fresh_authority())
    report = update_account_info(client, "4958", account_id="GD_NEW", dry_run=True)
    assert client.put_body is None
    assert report.changed
    assert not report.applied


# ---------------------------------------------------------- validation


def test_unknown_country_is_rejected_before_writing():
    client = FakeClient()
    with pytest.raises(UnknownCountryError) as exc:
        update_account_info(client, "4958", country="ZZZ")
    assert "IRL" in exc.value.available
    assert client.put_body is None


def test_unknown_state_is_rejected():
    client = FakeClient()
    with pytest.raises(UnknownStateError) as exc:
        update_account_info(client, "4958", country="IRL", state="XX")
    assert exc.value.country == "IRL"
    assert client.put_body is None


def test_state_is_checked_against_the_stored_country_when_none_given():
    client = FakeClient()
    with pytest.raises(UnknownStateError) as exc:
        update_account_info(client, "4958", state="TX")   # Texas, but country is IRL
    assert exc.value.country == "IRL"


def test_validation_can_be_skipped():
    client = FakeClient()
    update_account_info(client, "4958", country="ZZZ", validate=False)
    assert client.put_body["attributes"]["country"] == "ZZZ"


def test_unreadable_catalog_does_not_block_the_write():
    """Better to proceed than to fail because a dropdown shape changed.

    Note a flat {code: name} mapping IS a recognised shape, so the unreadable
    case has to be something genuinely unlike a catalog.
    """
    client = FakeClient(countries={"unexpected": {"deeply": "nested"}})
    update_account_info(client, "4958", country="ZZZ")
    assert client.put_body["attributes"]["country"] == "ZZZ"


# -------------------------------------------------------- verification


def test_silent_rejection_is_caught():
    class Stubborn(FakeClient):
        def put(self, path, json):
            self.put_body = json
            stored = dict(json)
            stored["account_id"] = "GD_0209"     # server ignored the change
            return stored

    client = Stubborn(fresh_authority())
    with pytest.raises(AccountInfoError, match="did not persist"):
        update_account_info(client, "4958", account_id="GD_NEW")


def test_summary_reads_well():
    client = FakeClient(fresh_authority())
    report = update_account_info(client, "4958", account_id="GD_NEW", state="D")
    assert report.summary() == (
        "authority 4958: 2 field(s) updated (account_id, state)"
    )


# --------------------------------------------------------- finding by name


class ListingClient:
    """Serves the paginated authorities list, newest first, like the real one."""

    def __init__(self, records, *, page_size=None):
        self.records = list(records)
        self.page_size = page_size
        self.pages_fetched = 0

    def get(self, path):
        if path.startswith("/v1/andromeda/authorities?"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(path).query)
            page = int(q["page"][0])
            limit = self.page_size or int(q["limit"][0])
            self.pages_fetched += 1
            start = (page - 1) * limit
            return {"authorities": self.records[start:start + limit],
                    "total_records": len(self.records)}
        raise AssertionError(path)

    def put(self, path, json):
        raise AssertionError("should not write")


def authority(id_, name, account_id=None, display_name=None):
    return {"id": id_, "name": name, "display_name": display_name or name,
            "account_id": account_id, "attributes": {}}


def test_finds_an_authority_by_name():
    from logic.authorities import find_authority
    client = ListingClient([authority(5222, "Broth 911"),
                            authority(4958, "gDTest", "GD_0209")])
    assert find_authority(client, "gDTest")["id"] == 4958


def test_name_match_is_case_insensitive_by_default():
    from logic.authorities import find_authority
    client = ListingClient([authority(4958, "gDTest")])
    assert find_authority(client, "GDTEST")["id"] == 4958


def test_case_sensitivity_can_be_required():
    from logic.authorities import AuthorityNotFoundError, find_authority
    client = ListingClient([authority(4958, "gDTest")])
    with pytest.raises(AuthorityNotFoundError):
        find_authority(client, "GDTEST", case_sensitive=True)


def test_display_name_also_matches():
    from logic.authorities import find_authority
    client = ListingClient([authority(4958, "internal-slug", display_name="gDTest")])
    assert find_authority(client, "gDTest")["id"] == 4958


def test_substring_does_not_match():
    """'gDT' must not silently resolve to gDTest."""
    from logic.authorities import AuthorityNotFoundError, find_authority
    client = ListingClient([authority(4958, "gDTest")])
    with pytest.raises(AuthorityNotFoundError):
        find_authority(client, "gDT")


def test_can_find_by_account_id():
    from logic.authorities import find_authority
    client = ListingClient([authority(4958, "gDTest", "GD_0209")])
    assert find_authority(client, account_id="GD_0209")["id"] == 4958


def test_pages_through_everything():
    from logic.authorities import find_authority
    records = [authority(5000 - n, f"auth-{n}") for n in range(120)]
    records.append(authority(1, "needle"))
    client = ListingClient(records, page_size=25)
    assert find_authority(client, "needle")["id"] == 1
    assert client.pages_fetched == 5


def test_duplicate_names_are_refused_not_guessed():
    from logic.authorities import AmbiguousAuthorityError, find_authority
    client = ListingClient([authority(4958, "gDTest", "GD_A"),
                            authority(5001, "gDTest", "GD_B")])
    with pytest.raises(AmbiguousAuthorityError) as exc:
        find_authority(client, "gDTest")
    assert len(exc.value.matches) == 2
    assert "Pass the numeric id" in str(exc.value)


def test_a_typo_suggests_close_names():
    from logic.authorities import AuthorityNotFoundError, find_authority
    client = ListingClient([authority(4958, "gDTest"), authority(5222, "Broth 911")])
    with pytest.raises(AuthorityNotFoundError) as exc:
        find_authority(client, "gDTeest")
    assert "gDTest" in exc.value.near


def test_resolve_accepts_a_numeric_id_without_listing():
    from logic.authorities import resolve_authority_id
    client = ListingClient([])
    assert resolve_authority_id(client, "4958") == "4958"
    assert client.pages_fetched == 0


def test_resolve_accepts_a_name():
    from logic.authorities import resolve_authority_id
    client = ListingClient([authority(4958, "gDTest")])
    assert resolve_authority_id(client, "gDTest") == "4958"


def test_empty_environment_is_a_clear_error():
    from logic.authorities import AuthorityNotFoundError, find_authority
    with pytest.raises(AuthorityNotFoundError) as exc:
        find_authority(ListingClient([]), "gDTest")
    assert exc.value.searched == 0


def test_server_capping_the_page_size_does_not_truncate_the_search():
    """We ask for limit=500; if the server caps at 25 we must keep paging."""
    from logic.authorities import find_authority
    records = [authority(5000 - n, f"auth-{n}") for n in range(60)]
    records.append(authority(1, "needle"))
    client = ListingClient(records, page_size=25)
    assert find_authority(client, "needle", limit=500)["id"] == 1
    assert client.pages_fetched == 3


def test_paging_without_a_total_falls_back_to_short_pages():
    from logic.authorities import iter_authorities

    class NoTotal(ListingClient):
        def get(self, path):
            payload = super().get(path)
            payload.pop("total_records")
            return payload

    client = NoTotal([authority(n, f"a-{n}") for n in range(30)], page_size=25)
    assert len(list(iter_authorities(client, limit=25))) == 30


# ------------------------------------------------------ immutable fields


def test_account_id_already_set_is_skipped_not_fatal():
    """Andromeda refuses to change Account ID once set; keep the rest."""
    client = FakeClient()          # account_id is already "GD_0209"
    report = update_account_info(client, "4958", account_id="GD_NEW", state="D")

    assert "account_id" in report.skipped
    assert "already set" in report.skipped["account_id"]
    assert set(report.changed) == {"state"}
    # the existing value is sent back untouched, and the other field applied
    assert client.put_body["account_id"] == "GD_0209"
    assert client.put_body["attributes"]["state"] == "D"
    assert report.applied


def test_setting_account_id_for_the_first_time_works():
    client = FakeClient(fresh_authority())
    report = update_account_info(client, "4958", account_id="SAND_31109")
    assert report.skipped == {}
    assert client.put_body["account_id"] == "SAND_31109"


def test_the_same_account_id_is_not_treated_as_a_change():
    client = FakeClient()
    report = update_account_info(client, "4958", account_id="GD_0209")
    assert report.is_noop
    assert report.skipped == {}
    assert client.put_body is None


def test_nothing_is_written_when_only_the_immutable_field_changed():
    client = FakeClient()
    report = update_account_info(client, "4958", account_id="GD_NEW")
    assert report.skipped
    assert report.is_noop
    assert client.put_body is None


def test_skipping_can_be_turned_off():
    client = FakeClient()
    update_account_info(client, "4958", account_id="GD_NEW", skip_immutable=False)
    assert client.put_body["account_id"] == "GD_NEW"    # let the API reject it


def test_a_server_side_rejection_is_retried_without_the_field():
    """The pre-check can miss it if the value was set between read and write."""

    class Refuses(FakeClient):
        def __init__(self):
            super().__init__(fresh_authority())
            self.attempts = []

        def put(self, path, json):
            self.attempts.append(dict(json))
            if json.get("account_id") == "GD_NEW":
                raise RuntimeError(
                    '400 on PUT /v1/andromeda/authorities/4958: '
                    '{"detail":"Cannot update Account ID once set."}'
                )
            return super().put(path, json)

    client = Refuses()
    report = update_account_info(client, "4958", account_id="GD_NEW", state="D")

    assert len(client.attempts) == 2
    assert report.fallback_used
    assert "account_id" in report.skipped
    assert set(report.changed) == {"state"}
    assert client.attempts[1]["attributes"]["state"] == "D"


def test_an_unrelated_error_is_not_swallowed():
    class Broken(FakeClient):
        def put(self, path, json):
            raise RuntimeError("500 Internal Server Error")

    with pytest.raises(RuntimeError, match="500"):
        update_account_info(Broken(fresh_authority()), "4958", account_id="GD_NEW")


def test_summary_mentions_what_was_skipped():
    client = FakeClient()
    report = update_account_info(client, "4958", account_id="GD_NEW", state="D")
    assert report.summary() == (
        "authority 4958: 1 field(s) updated (state); 1 skipped (account_id)"
    )