"""Authentication expiry and malformed-data regression tests."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ikb_direkt.api import (
    IKBDirektClient, IKBAuthenticationError, IKBDataError, MeterPoint,
)


def response(status=200, data=None, body=""):
    resp = AsyncMock()
    resp.status = status
    resp.headers = {}
    resp.url = "https://direkt.ikb.at/"
    resp.json.return_value = data
    resp.text.return_value = body
    context = AsyncMock()
    context.__aenter__.return_value = resp
    return context, resp


def client_with_responses(*responses):
    session = MagicMock()
    session.closed = False
    session.get.side_effect = [pair[0] for pair in responses]
    client = IKBDirektClient("user", "password", session)
    client._is_logged_in = True
    client.login = AsyncMock(return_value=True)
    return client, session


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [401, 403, "html", "envelope"])
async def test_expired_session_retries_once(expired):
    first = response(expired if isinstance(expired, int) else 200)
    if expired == "html":
        first[1].json.side_effect = ValueError("not json")
        first[1].text.return_value = '<form id="kc-form-login">'
    elif expired == "envelope":
        first[1].json.return_value = {"redirect": "https://sso.ikb.at/login"}
    client, session = client_with_responses(first, response(data=[]))
    assert await client._request_json("https://direkt.ikb.at/test") == []
    client.login.assert_awaited_once()
    session.cookie_jar.clear.assert_called_once()


@pytest.mark.asyncio
async def test_repeated_expiration_is_bounded():
    client, session = client_with_responses(response(401), response(401))
    with pytest.raises(IKBAuthenticationError):
        await client._request_json("https://direkt.ikb.at/test")
    assert session.get.call_count == 2
    client.login.assert_awaited_once()
    assert not client._is_logged_in


@pytest.mark.asyncio
async def test_unknown_error_envelope_is_not_empty_success():
    client, _ = client_with_responses(response(data={"error": "server failure"}))
    meter = MeterPoint("AT1", "M1", "Verbrauch", "", "", "{}", "Meter")
    with pytest.raises(IKBDataError):
        await client.get_load_profile(meter, "01.01.2026", "02.01.2026")


@pytest.mark.asyncio
async def test_missing_intervals_skipped_and_real_overlap_beats_placeholder():
    data = [{"data": [[900, None, 0], [1800, 0, 900]]},
            {"data": [[1800, 0.25, 900], [2700, 0, 1800]]}]
    client, _ = client_with_responses(response(data=data))
    meter = MeterPoint("AT1", "M1", "Verbrauch", "", "", "{}", "Meter")
    readings = await client.get_load_profile(meter, "01.01.2026", "02.01.2026")
    assert [(r.start_timestamp, r.kwh) for r in readings] == [(900, 0.25), (1800, 0)]


@pytest.mark.parametrize("url", ["http://sso.ikb.at/login", "https://evil.test/login",
    "https://sso.ikb.at.evil.test/login", "https://user@sso.ikb.at/login"])
def test_untrusted_auth_destinations_rejected(url):
    with pytest.raises(IKBAuthenticationError):
        IKBDirektClient._validate_auth_url(url)


@pytest.mark.asyncio
async def test_account_clients_own_separate_cookie_jars():
    a, b = IKBDirektClient("a", "a"), IKBDirektClient("b", "b")
    try:
        sa, sb = await a._get_session(), await b._get_session()
        assert sa is not sb
        assert sa.cookie_jar is not sb.cookie_jar
    finally:
        await a.close()
        await b.close()
    assert sa.closed and sb.closed


@pytest.mark.asyncio
async def test_json_login_form_also_refreshes_session():
    client, _ = client_with_responses(
        response(data={"content": '<form id="kc-form-login">'}), response(data=[]))
    assert await client._request_json("https://direkt.ikb.at/test") == []
    client.login.assert_awaited_once()


@pytest.mark.asyncio
async def test_credentials_are_not_posted_to_untrusted_form():
    landing = response(body='<meta name="csrf-token" content="csrf">')
    sso = response(body='<form action="https://evil.test/collect"></form>')
    sso[1].url = "https://sso.ikb.at/login"
    initiation = response(data={"redirect": "https://sso.ikb.at/login"})
    session = MagicMock()
    session.closed = False
    session.get.side_effect = [landing[0], sso[0]]
    session.post.return_value = initiation[0]
    client = IKBDirektClient("user", "secret", session)
    with pytest.raises(IKBAuthenticationError, match="Untrusted"):
        await client.login()
    assert session.post.call_count == 1  # Initiation only; no credentials submitted.


@pytest.mark.asyncio
async def test_login_updates_csrf_and_does_not_forward_credential_redirects():
    landing = response(body='<meta name="csrf-token" content="old">')
    sso = response(body='<form action="/submit"><input type="hidden" name="username" value="wrong"></form>')
    sso[1].url = "https://sso.ikb.at/login"
    complete = response(body='<meta name="csrf-token" content="new"><a href="?page=logout">Logout</a>')
    initiation = response(data={"redirect": "https://sso.ikb.at/login"})
    submission = response(status=302)
    submission[1].url = "https://sso.ikb.at/submit"
    submission[1].headers = {"Location":"https://direkt.ikb.at/callback"}
    session = MagicMock()
    session.closed = False
    session.get.side_effect = [landing[0], sso[0], complete[0]]
    session.post.side_effect = [initiation[0], submission[0]]
    client = IKBDirektClient("user", "secret", session)
    assert await client.login()
    assert client._csrf_token == "new"
    assert session.post.call_args.kwargs["allow_redirects"] is False
    assert session.post.call_args.kwargs["data"]["username"] == "user"
