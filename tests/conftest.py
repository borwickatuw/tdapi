"""Shared fixtures. No test in this suite touches the network."""

import logging

import pytest

import tdapi

API_ROOT = "https://example.teamdynamix.com/TDWebApi/api/"


@pytest.fixture(autouse=True)
def suppress_logging():
    """Keep the retry path's warnings out of test output."""
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture
def login_route(requests_mock):
    """Register the admin login route every connection calls on construction."""
    requests_mock.post(API_ROOT + "auth/loginadmin", text="BEARER-TOKEN")
    return requests_mock


@pytest.fixture
def conn(login_route):
    """A live-looking connection with pacing off, so tests do not sleep."""
    return tdapi.TDConnection(
        BEID="beid",
        WebServicesKey="key",
        url_root=API_ROOT,
        request_delay=0,
    )


@pytest.fixture
def global_conn(conn):
    """`conn`, also installed as the package-global connection.

    The higher-level object classes read tdapi.TD_CONNECTION rather than
    taking a connection argument, so they need this. Restored afterwards
    so tests cannot leak a connection into each other.
    """
    previous = tdapi.get_connection()
    tdapi.set_connection(conn)
    yield conn
    tdapi.set_connection(previous)
