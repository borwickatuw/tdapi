"""Caching is opt-in, and scoped to the connection that asked for it."""

import requests
import requests_cache

import tdapi


def test_default_session_is_an_uncached_requests_session():
    assert type(tdapi.make_session()) is requests.Session


def test_importing_tdapi_does_not_patch_requests_globally():
    # The import-time install_cache() this replaced made every HTTP client
    # in the process cache for 15 minutes.
    assert not requests_cache.is_installed()


def test_cache_expire_after_opts_into_a_cached_session():
    session = tdapi.make_session(900)
    assert isinstance(session, requests_cache.CachedSession)


def test_connection_defaults_to_no_cache(conn):
    assert type(conn.session) is requests.Session


def test_connection_records_its_timeout(conn):
    assert conn.timeout == tdapi.DEFAULT_TIMEOUT


def test_every_request_carries_the_timeout(conn, requests_mock):
    requests_mock.get("https://example.teamdynamix.com/TDWebApi/api/ping", json={"ok": True})
    conn.timeout = 12
    conn.json_request(method="get", url_stem="ping")
    assert requests_mock.last_request.timeout == 12
