"""url_root resolution: the one breaking change in this release."""

import pytest

import tdapi


def test_bare_org_url_gets_the_production_application():
    assert (
        tdapi.resolve_url_root("https://example.teamdynamix.com")
        == "https://example.teamdynamix.com/TDWebApi/api/"
    )


def test_trailing_slash_is_not_doubled():
    assert (
        tdapi.resolve_url_root("https://example.teamdynamix.com/")
        == "https://example.teamdynamix.com/TDWebApi/api/"
    )


def test_sandbox_selects_the_sandbox_application():
    assert (
        tdapi.resolve_url_root("https://example.teamdynamix.com/", sandbox=True)
        == "https://example.teamdynamix.com/SBTDWebApi/api/"
    )


@pytest.mark.parametrize(
    "given",
    [
        "https://example.teamdynamix.com/TDWebApi/api/",
        "https://example.teamdynamix.com/TDWebApi/api",
    ],
)
def test_a_complete_api_root_is_used_as_given(given):
    assert tdapi.resolve_url_root(given) == "https://example.teamdynamix.com/TDWebApi/api/"


def test_a_sandbox_api_root_is_used_as_given_without_the_sandbox_flag():
    assert (
        tdapi.resolve_url_root("https://example.teamdynamix.com/SBTDWebApi/api/")
        == "https://example.teamdynamix.com/SBTDWebApi/api/"
    )


def test_missing_url_root_raises_and_names_the_requirement():
    with pytest.raises(tdapi.TDConfigurationException) as excinfo:
        tdapi.resolve_url_root(None)
    message = str(excinfo.value)
    assert "url_root is required" in message
    # The error has to explain *why* there is no default any more.
    assert "api.teamdynamix.com" in message


def test_sandbox_against_a_production_root_raises_rather_than_hitting_production():
    with pytest.raises(tdapi.TDConfigurationException) as excinfo:
        tdapi.resolve_url_root("https://example.teamdynamix.com/TDWebApi/api/", sandbox=True)
    assert "production" in str(excinfo.value)


def test_preview_rewrites_a_teamdynamix_host():
    assert (
        tdapi.resolve_url_root("https://example.teamdynamix.com/", preview=True)
        == "https://example.teamdynamixpreview.com/TDWebApi/api/"
    )


def test_preview_is_idempotent_on_an_already_preview_host():
    assert (
        tdapi.resolve_url_root("https://example.teamdynamixpreview.com/", preview=True)
        == "https://example.teamdynamixpreview.com/TDWebApi/api/"
    )


def test_preview_refuses_to_guess_at_an_unfamiliar_host():
    with pytest.raises(tdapi.TDConfigurationException):
        tdapi.resolve_url_root("https://helpdesk.example.edu/", preview=True)


def test_connection_construction_requires_url_root(requests_mock):
    requests_mock.post("https://example.teamdynamix.com/TDWebApi/api/auth/loginadmin", text="T")
    with pytest.raises(tdapi.TDConfigurationException):
        tdapi.TDConnection(BEID="b", WebServicesKey="k")
