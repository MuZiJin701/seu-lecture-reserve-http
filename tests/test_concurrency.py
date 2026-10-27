import threading

import pytest

import seu_reserve.client as client_module
from test_activities import ActivitySchool, OPEN, activity
from test_app import School, execute


def test_background_poller_reads_list_during_opening(tmp_path, monkeypatch):
    gate = threading.Event()
    calls = []

    class Clone:
        session = None

        def activities(self):
            calls.append('poller')
            gate.set()
            return [activity()]

    monkeypatch.setattr(client_module.Client, 'clone_readonly', lambda self: Clone())
    school = ActivitySchool()
    school.activities = [activity()]
    school.server_now = '2029-12-31 18:59:50'

    def wait(seconds):
        assert gate.wait(2.0)
        raise KeyboardInterrupt

    assert execute(school, tmp_path, clock=lambda: OPEN, sleep=wait,
                   background_poller=True) == 130
    assert calls


def test_background_poller_is_not_started_outside_openings(tmp_path, monkeypatch):
    def unexpected_clone(self):
        pytest.fail('poller must only run during an opening window')

    monkeypatch.setattr(client_module.Client, 'clone_readonly', unexpected_clone)
    assert execute(School(), tmp_path, background_poller=True) == 130
