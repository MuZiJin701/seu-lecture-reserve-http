import threading

import seu_reserve.app as app_module
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
            if len(calls) > 1:
                # Keep this fixture limited to one successful list read.
                raise RuntimeError('single-read poller')
            return [activity()]

    monkeypatch.setattr(client_module.Client, 'clone_readonly', lambda self: Clone())
    school = ActivitySchool()
    school.activities = [activity()]

    def wait(seconds):
        assert gate.wait(2.0)
        raise KeyboardInterrupt

    assert execute(school, tmp_path, clock=lambda: OPEN, sleep=wait,
                   background_poller=True) == 130
    assert calls


def test_background_readers_start_outside_openings(tmp_path, monkeypatch):
    clones = []
    class Clone:
        session = None
        def activities(self):
            return []
        def mine(self):
            return []
    def clone(client):
        reader = Clone()
        clones.append(reader)
        return reader
    monkeypatch.setattr(client_module.Client, 'clone_readonly', clone)
    assert execute(School(), tmp_path, background_poller=True) == 130
    assert len(clones) == 2


def test_server_wait_learned_during_captcha_blocks_submission(tmp_path, monkeypatch):
    import time
    from seu_reserve.app import Settings, run
    from seu_reserve.poller import WaitGate
    from test_unified_workers import install_reader_sessions
    base = time.monotonic()
    gate = WaitGate(lambda: OPEN + time.monotonic() - base)
    monkeypatch.setattr(app_module, 'WaitGate', lambda clock: gate)
    class Tracked(ActivitySchool):
        limited_at = None
        submitted_at = None
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if url.endswith('vcode.do') and self.limited_at is None:
                self.limited_at = time.monotonic()
                gate.record(.2)
            if url.endswith('addReservation.do'):
                self.submitted_at = time.monotonic()
            return response
    school = Tracked()
    school.activities = [activity()]
    calls = []
    install_reader_sessions(monkeypatch, school, calls)
    assert run(Settings(state_dir=tmp_path, prewarm=False, captcha_prefetch=False),
               session=school, environ={'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'test-password'},
               recognize=lambda _: '1234', clock=lambda: OPEN + time.monotonic() - base,
               stop=lambda: school.submitted_at is not None or time.monotonic() - base > 3) == 130
    assert school.submitted_at is not None and school.limited_at is not None
    assert school.submitted_at - school.limited_at >= .2
