"""Tests for the sign-up wizard (runbook step 1)."""

from __future__ import annotations

import pytest

from logic.signup import (
    PsapDetails,
    SignupError,
    confirm_email,
    create_psap,
    default_password,
    register,
    sign_up,
    token_from_link,
)

# Trimmed from a real register response.
USER = {
    "id": "42407",
    "email": "gdrishti+test2@rapidsos.com",
    "email_confirmed": False,
    "first_name": "Gerrard",
    "last_name": "Drishti",
    "organizations": [{"id": 15750, "name": "GDTest", "roles": [], "entitlements": []}],
}

TOKEN = (
    "eyJhbGciOiJIUzUxMiJ9"
    ".eyJlbWFpbCI6ImdkcmlzaHRpK3Rlc3QyQHJhcGlkc29zLmNvbSIsInR5cGUiOiJjb25maXJtYXRpb24ifQ"
    ".F6KHsjlOA-O6M1K9sZnPAP1Q8kkcFtE2s8KPB23zXcNe887IeGVLpuMkTCLLJkVfxAAlOHhBFnNbGkg29ffstQ"
)
LINK = f"https://sandbox.rapidsosportal.com/confirmation-email/?token={TOKEN}"


class FakeClient:
    def __init__(self, *, psap_status=500):
        self.psap_status = psap_status
        self.posts = []

    def post(self, path, json):
        self.posts.append((path, json))
        if path.endswith("/register"):
            return USER
        if path.endswith("/psaps/"):
            if self.psap_status == 500:
                raise RuntimeError(
                    "500 on POST /v1/capstone/psaps/: "
                )
            if self.psap_status >= 400:
                raise RuntimeError(
                    f"{self.psap_status} on POST /v1/capstone/psaps/: "
                    '{"detail":"bad request"}'
                )
            return {"id": 1}
        return None


# -------------------------------------------------------------- password


@pytest.mark.parametrize("email", [
    "gdrishti+test2@rapidsos.com",
    "gdrishti+lancaster@rapidsos.com",
    "someone@rapidsos.com",
    None,
])
def test_the_password_is_the_same_for_every_account(email):
    assert default_password(email) == "AutomatedAccount123!"


def test_the_password_satisfies_the_rules_the_api_enforces():
    """A tag-derived password was rejected as 'Invalid value.'"""
    pw = default_password()
    assert len(pw) >= 8
    assert any(c.isupper() for c in pw)
    assert any(c.islower() for c in pw)
    assert any(c.isdigit() for c in pw)
    assert any(not c.isalnum() for c in pw)


# ----------------------------------------------------------------- token


def test_a_bare_token_passes_through():
    assert token_from_link(TOKEN) == TOKEN


def test_the_token_is_pulled_out_of_the_emailed_link():
    assert token_from_link(LINK) == TOKEN


def test_surrounding_whitespace_is_ignored():
    assert token_from_link(f"  {LINK}  ") == TOKEN


def test_a_link_without_a_token_is_refused():
    with pytest.raises(SignupError, match="no token"):
        token_from_link("https://sandbox.rapidsosportal.com/confirmation-email/")


def test_an_empty_value_is_refused():
    with pytest.raises(SignupError, match="no confirmation token"):
        token_from_link("   ")


# -------------------------------------------------------------- register


def test_register_sends_what_the_wizard_sends():
    client = FakeClient()
    register(client, email="gdrishti+test2@rapidsos.com",
             password="AutomatedAccount123!",
             first_name="Gerrard", last_name="Drishti", organization_name="GDTest")
    path, body = client.posts[0]
    assert path == "/v1/scorpius/user/register"
    assert body == {
        "email": "gdrishti+test2@rapidsos.com",
        "password": "AutomatedAccount123!",
        "first_name": "Gerrard",
        "last_name": "Drishti",
        "organization_name": "GDTest",
        "application": "capstone",
    }


# ------------------------------------------------------------------ psap


def test_psap_body_matches_the_wizard():
    client = FakeClient(psap_status=200)
    details = PsapDetails(name="GDTest", contact_name="Gerrard Drishti",
                          contact_email="gdrishti+test2@rapidsos.com")
    create_psap(client, details)
    _, body = client.posts[0]
    assert body == {
        "name": "GDTest",
        "display_name": "GDTest",
        "state": "AL",
        "fcc_id": "",
        "contact_name": "Gerrard Drishti",
        "contact_title": "Automated",
        "contact_email": "gdrishti+test2@rapidsos.com",
        "contact_phone": "12125551234",
        "non_emergency_phone": "12125551234",
        "population": 12345,
        "phone_system": 0,
        "cad_system": 0,
        "mapping_system": 0,
    }


def test_the_known_500_is_tolerated():
    """The API errors but stores the data -- confirmed against a real signup."""
    status = create_psap(FakeClient(psap_status=500),
                         PsapDetails(name="X", contact_name="Y", contact_email="z@x.com"))
    assert "500" in status


def test_a_client_error_is_not_tolerated():
    """A 4xx means the payload was wrong, which is a real failure."""
    with pytest.raises(RuntimeError, match="400"):
        create_psap(FakeClient(psap_status=400),
                    PsapDetails(name="X", contact_name="Y", contact_email="z@x.com"))


def test_tolerance_can_be_turned_off():
    with pytest.raises(RuntimeError, match="500"):
        create_psap(FakeClient(psap_status=500),
                    PsapDetails(name="X", contact_name="Y", contact_email="z@x.com"),
                    tolerate_server_error=False)


def test_display_name_defaults_to_the_name():
    details = PsapDetails(name="Lancaster County NE Sandbox", contact_name="A",
                          contact_email="a@b.com")
    assert details.to_body()["display_name"] == "Lancaster County NE Sandbox"


# ------------------------------------------------------------- confirming


def test_confirm_posts_the_token():
    client = FakeClient()
    confirm_email(client, LINK)
    assert client.posts == [("/v1/scorpius/user/confirm", {"token": TOKEN})]


# ------------------------------------------------------------ the whole flow


def test_sign_up_runs_both_calls_in_order():
    client = FakeClient()
    result = sign_up(client, email="gdrishti+test2@rapidsos.com",
                     agency_name="GDTest", first_name="Gerrard", last_name="Drishti")

    assert [p for p, _ in client.posts] == [
        "/v1/scorpius/user/register", "/v1/capstone/psaps/",
    ]
    assert result.user_id == "42407"
    assert result.organization_id == "15750"
    assert "500" in result.psap_status
    assert result.needs_confirmation


def test_the_password_and_organization_are_filled_in():
    client = FakeClient()
    sign_up(client, email="gdrishti+lancaster@rapidsos.com", agency_name="Lancaster NE",
            first_name="Gerrard", last_name="Drishti")
    _, body = client.posts[0]
    assert body["password"] == "AutomatedAccount123!"
    assert body["organization_name"] == "Lancaster NE"


def test_an_explicit_password_still_wins():
    client = FakeClient()
    sign_up(client, email="a+b@c.com", agency_name="X", first_name="A", last_name="B",
            password="SomethingElse1!")
    assert client.posts[0][1]["password"] == "SomethingElse1!"


def test_contact_name_comes_from_the_person():
    client = FakeClient()
    sign_up(client, email="a+b@c.com", agency_name="X",
            first_name="Ada", last_name="Lovelace")
    _, body = client.posts[1]
    assert body["contact_name"] == "Ada Lovelace"
    assert body["contact_email"] == "a+b@c.com"


def test_details_can_be_overridden_individually():
    client = FakeClient()
    sign_up(client, email="a+b@c.com", agency_name="X", first_name="A", last_name="B",
            population=99999, state="NE", contact_title="QA")
    _, body = client.posts[1]
    assert body["population"] == 99999
    assert body["state"] == "NE"
    assert body["contact_title"] == "QA"


def test_details_object_and_overrides_together_are_refused():
    details = PsapDetails(name="X", contact_name="Y", contact_email="z@x.com")
    with pytest.raises(SignupError, match="not both"):
        sign_up(FakeClient(), email="a+b@c.com", agency_name="X", first_name="A",
                last_name="B", details=details, population=1)


def test_dry_run_sends_nothing():
    client = FakeClient()
    result = sign_up(client, email="a+b@c.com", agency_name="X",
                     first_name="A", last_name="B", dry_run=True)
    assert client.posts == []
    assert result.user_id is None
    assert result.needs_confirmation


def test_summary_says_confirmation_is_outstanding():
    client = FakeClient()
    result = sign_up(client, email="gdrishti+test2@rapidsos.com", agency_name="GDTest",
                     first_name="Gerrard", last_name="Drishti")
    assert "awaiting email confirmation" in result.summary()
    assert "organization 15750" in result.summary()


# ------------------------------------------------------ logging back in


def _fake_jwt(username="gdrishti+autotest2@rapidsos.com", exp_in=7200):
    import base64, json as _json, time
    head = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(_json.dumps({
        "iss": "RapidSOS", "sub": "42419", "username": username,
        "type": "access", "exp": int(time.time()) + exp_in,
    }).encode()).decode().rstrip("=")
    return f"{head}.{payload}.signature"


class LoginClient(FakeClient):
    def __init__(self, *, token=None, refresh="refresh-abc", **kw):
        super().__init__(**kw)
        self.token = token or _fake_jwt()
        self.refresh = refresh

    def post(self, path, json):
        self.posts.append((path, json))
        if path.endswith("/api-token-auth"):
            return {"token": self.token, "refresh_token": self.refresh}
        if path.endswith("/api-token-auth/refresh"):
            return {"token": self.token, "refresh_token": "refresh-def"}
        return super().post(path, json)


def test_an_account_can_log_itself_in():
    from logic.signup import log_in
    client = LoginClient()
    session = log_in(client, "gdrishti+autotest2@rapidsos.com")

    path, body = client.posts[0]
    assert path == "/v1/scorpius/user/api-token-auth"
    assert body == {"email": "gdrishti+autotest2@rapidsos.com",
                    "password": "AutomatedAccount123!"}
    assert session.token
    assert session.refresh_token == "refresh-abc"
    assert session.username == "gdrishti+autotest2@rapidsos.com"
    assert session.seconds_left() > 0


def test_a_different_password_can_be_given():
    from logic.signup import log_in
    client = LoginClient()
    log_in(client, "someone@rapidsos.com", "Different1!")
    assert client.posts[0][1]["password"] == "Different1!"


def test_the_token_is_never_in_the_repr():
    from logic.signup import log_in
    session = log_in(LoginClient(), "a+b@c.com")
    assert session.token not in repr(session)
    assert "gdrishti+autotest2@rapidsos.com" in repr(session)


def test_an_opaque_token_still_works():
    """Claims are a convenience; a token that is not a JWT must not break login."""
    from logic.signup import log_in
    session = log_in(LoginClient(token="not-a-jwt"), "a+b@c.com")
    assert session.token == "not-a-jwt"
    assert session.username is None
    assert session.seconds_left() is None


def test_a_login_response_without_a_token_is_refused():
    from logic.signup import SignupError, log_in

    class NoToken(FakeClient):
        def post(self, path, json):
            return {"detail": "ok"}

    with pytest.raises(SignupError, match="no token"):
        log_in(NoToken(), "a+b@c.com")


def test_a_refresh_token_can_be_exchanged():
    from logic.signup import refresh_session
    client = LoginClient()
    session = refresh_session(client, "refresh-abc")
    assert client.posts[0] == ("/v1/scorpius/user/api-token-auth/refresh",
                               {"refresh_token": "refresh-abc"})
    assert session.refresh_token == "refresh-def"


def test_an_expired_token_reports_negative_time():
    from logic.signup import log_in
    session = log_in(LoginClient(token=_fake_jwt(exp_in=-60)), "a+b@c.com")
    assert session.seconds_left() < 0