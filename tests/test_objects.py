"""The higher-level object layer, including the four fixed defects."""

import pytest

import tdapi
import tdapi.asset
import tdapi.obj
import tdapi.project
import tdapi.ticket
import tdapi.util
from tests.conftest import API_ROOT

TDTicket = tdapi.ticket.TDTicketAppFactory(46)


def test_feed_is_fetched_from_the_ticket_s_feed_route(global_conn, requests_mock):
    requests_mock.get(
        API_ROOT + "46/tickets/1234/feed",
        json=[{"ID": 1, "Body": "first"}, {"ID": 2, "Body": "second"}],
    )
    ticket = TDTicket({"ID": 1234, "Title": "Broken printer"})
    assert [entry["Body"] for entry in ticket.feed()] == ["first", "second"]


def test_an_empty_feed_is_an_empty_list(global_conn, requests_mock):
    requests_mock.get(API_ROOT + "46/tickets/1234/feed", json=[])
    assert TDTicket({"ID": 1234}).feed() == []


def test_a_single_feed_entry_still_arrives_as_a_list(global_conn, requests_mock):
    # json_request_roller's whole job: TD returns a bare object for one result.
    requests_mock.get(API_ROOT + "46/tickets/1234/feed", json={"ID": 1})
    assert TDTicket({"ID": 1234}).feed() == [{"ID": 1}]


def test_tasks_are_fetched_from_the_ticket_s_tasks_route(global_conn, requests_mock):
    requests_mock.get(API_ROOT + "46/tickets/1234/tasks", json=[{"ID": 9, "Title": "Order part"}])
    assert TDTicket({"ID": 1234}).tasks()[0]["Title"] == "Order part"


def test_patch_update_sends_the_notify_query_string(global_conn, requests_mock):
    # Regression: patch_url was built and then thrown away.
    requests_mock.patch(API_ROOT + "46/tickets/1234", json={})
    TDTicket({"ID": 1234}).patch_update([{"op": "replace"}], notify_responsible=True)
    assert requests_mock.last_request.qs["notifynewresponsible"] == ["true"]


def test_patch_update_defaults_to_not_notifying(global_conn, requests_mock):
    requests_mock.patch(API_ROOT + "46/tickets/1234", json={})
    TDTicket({"ID": 1234}).patch_update([{"op": "replace"}])
    assert requests_mock.last_request.qs["notifynewresponsible"] == ["false"]


def test_object_manager_json_request_reaches_the_connection(global_conn, requests_mock):
    # Regression: this referenced an undefined `settings` global.
    requests_mock.get(API_ROOT + "accounts", json=[{"ID": 1}])
    manager = tdapi.obj.TDObjectManager()
    assert manager.json_request(method="get", url_stem="accounts") == [{"ID": 1}]


def test_obj_module_keeps_its_docstring():
    # It sat below an import, so it was a bare expression, not a docstring.
    assert tdapi.obj.__doc__ is not None


class TestKeyMatcher:
    """Regression: every method used Python 2's dict.has_key."""

    def test_matches_on_the_first_tracked_key(self):
        matcher = tdapi.util.KeyMatcher(["Email", "Username"])
        matcher.add("person-a", {"Email": "a@example.edu"})
        assert matcher.match({"Email": "a@example.edu"}) == "person-a"

    def test_falls_through_to_a_later_key(self):
        matcher = tdapi.util.KeyMatcher(["Email", "Username"])
        matcher.add("person-b", {"Username": "bee"})
        assert matcher.match({"Email": "nobody@example.edu", "Username": "bee"}) == "person-b"

    def test_key_order_decides_which_match_wins(self):
        matcher = tdapi.util.KeyMatcher(["Email", "Username"])
        matcher.add("by-email", {"Email": "a@example.edu"})
        matcher.add("by-username", {"Username": "aye"})
        assert matcher.match({"Email": "a@example.edu", "Username": "aye"}) == "by-email"

    def test_no_match_is_none(self):
        matcher = tdapi.util.KeyMatcher(["Email"])
        matcher.add("person-a", {"Email": "a@example.edu"})
        assert matcher.match({"Email": "z@example.edu"}) is None

    def test_empty_and_none_values_are_not_tracked(self):
        matcher = tdapi.util.KeyMatcher(["Email"])
        matcher.add("person-a", {"Email": ""})
        matcher.add("person-b", {"Email": None})
        assert matcher.match({"Email": ""}) is None


def test_cached_record_manager_finds_by_all_keys():
    records = [{"Name": "a", "Active": True}, {"Name": "b", "Active": False}]
    manager = tdapi.util.CachedRecordManager(records)
    assert manager.find({"Name": "a", "Active": True}) == [records[0]]
    assert manager.find({"Name": "a", "Active": False}) == []


def test_version_is_reported_from_package_metadata():
    assert tdapi.__version__


class TestProject:
    """Regressions in the project wrapper, which had never been importable."""

    def test_the_module_imports_at_all(self):
        # setup.py declared iso8601; project.py was the only user of it,
        # and the dependency is gone.
        assert tdapi.project.TDProject

    def test_td_urlstem_formats_the_project_id(self):
        # Regression: this referenced an undefined `project_details_url`,
        # which was also format()'s only positional argument.
        project = tdapi.project.TDProject({"ID": 77})
        assert project.td_urlstem() == "Projects/Details/?TID=77"

    def test_dates_parse_with_the_trailing_z_teamdynamix_sends(self):
        project = tdapi.project.TDProject({"StartDate": "2020-03-04T00:00:00Z"})
        assert project.start_date() == "2020-03-04"

    def test_dates_parse_with_an_offset(self):
        project = tdapi.project.TDProject({"EndDate": "2026-09-18T12:34:56.789-07:00"})
        assert project.end_date() == "2026-09-18"


def test_an_ambiguous_room_lookup_raises_rather_than_returning_none():
    # Regression: `assert "Too many matching rooms"` always passes, and
    # the method then returned None for the case it meant to reject.
    location = tdapi.asset.TDLocation({"ID": 5, "Rooms": [{"ID": 9}, {"ID": 9}]})
    with pytest.raises(tdapi.TDException):
        location.get_room(9)
