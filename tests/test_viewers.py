"""Pause when not viewed: cards and live streams keep a slideshow running."""
from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.album_slideshow import viewers
from custom_components.album_slideshow.const import DOMAIN, VIEWER_GRACE_SECONDS, WS_VIEWER_HEARTBEAT
from custom_components.album_slideshow.store import SlideshowStore

from .test_camera_next_slide import _make_cam

GRACE = 0.05


def _tracked(tracker, entry_id="album"):
    changes = []
    untrack = tracker.async_track(entry_id, lambda: changes.append(tracker.is_unwatched(entry_id)))
    return changes, untrack


def test_grace_is_a_minute_and_longer_than_the_card_heartbeat():
    assert VIEWER_GRACE_SECONDS == 60
    assert viewers.ViewerTracker()._grace == VIEWER_GRACE_SECONDS


def test_album_becomes_unwatched_once_the_grace_period_passes_without_viewers():
    async def run():
        tracker = viewers.ViewerTracker(GRACE)
        changes, _untrack = _tracked(tracker)
        # Starting up counts as watched so cards have time to reconnect.
        assert not tracker.is_unwatched("album")
        await asyncio.sleep(GRACE / 2)
        assert not tracker.is_unwatched("album")
        await asyncio.sleep(GRACE)
        assert tracker.is_unwatched("album")
        return changes

    assert asyncio.run(run()) == [True]


def test_heartbeats_keep_the_album_watched_and_a_returning_card_resumes_it():
    async def run():
        tracker = viewers.ViewerTracker(GRACE)
        changes, _untrack = _tracked(tracker)
        for _ in range(4):
            await asyncio.sleep(GRACE / 2)
            tracker.async_heartbeat("album")
        assert not tracker.is_unwatched("album")
        await asyncio.sleep(GRACE * 1.5)
        assert tracker.is_unwatched("album")
        tracker.async_heartbeat("album")
        assert not tracker.is_unwatched("album")
        return changes

    assert asyncio.run(run()) == [True, False]


def test_open_streams_count_until_closed_then_the_grace_period_restarts():
    async def run():
        tracker = viewers.ViewerTracker(GRACE)
        changes, _untrack = _tracked(tracker)
        await asyncio.sleep(GRACE * 1.5)
        assert tracker.is_unwatched("album")
        first = tracker.async_add_stream("album")
        second = tracker.async_add_stream("album")
        assert not tracker.is_unwatched("album")
        await asyncio.sleep(GRACE * 1.5)
        first()
        first()  # closing twice must not drop the other stream
        await asyncio.sleep(GRACE * 1.5)
        assert not tracker.is_unwatched("album")
        second()
        await asyncio.sleep(GRACE / 2)
        assert not tracker.is_unwatched("album")
        await asyncio.sleep(GRACE)
        assert tracker.is_unwatched("album")
        return changes

    assert asyncio.run(run()) == [True, False, True]


def test_restarting_the_grace_period_counts_the_album_as_watched_again():
    async def run():
        tracker = viewers.ViewerTracker(GRACE)
        changes, _untrack = _tracked(tracker)
        await asyncio.sleep(GRACE * 1.5)
        assert tracker.is_unwatched("album")
        # Quietly: the camera only does this as the setting turns on.
        tracker.async_restart_grace("album")
        assert not tracker.is_unwatched("album")
        await asyncio.sleep(GRACE / 2)
        assert not tracker.is_unwatched("album")
        await asyncio.sleep(GRACE)
        assert tracker.is_unwatched("album")
        return changes

    assert asyncio.run(run()) == [True, True]


def test_albums_are_tracked_independently_and_forgotten_when_untracked():
    async def run():
        tracker = viewers.ViewerTracker(GRACE)
        _changes, untrack = _tracked(tracker, "first")
        _tracked(tracker, "second")
        for _ in range(3):
            await asyncio.sleep(GRACE / 2)
            tracker.async_heartbeat("second")
        assert tracker.is_unwatched("first")
        assert not tracker.is_unwatched("second")
        untrack()
        assert "first" not in tracker._albums
        assert not tracker.is_unwatched("first")

    asyncio.run(run())


def test_tracker_is_shared_through_hass_data_so_it_survives_album_reloads():
    hass = SimpleNamespace(data={})
    assert viewers.async_get_tracker(hass) is viewers.async_get_tracker(hass)
    assert hass.data[DOMAIN]["viewers"] is viewers.async_get_tracker(hass)


# ── camera hold ─────────────────────────────────────────────────────────────


def _viewer_cam(*, enabled=True, unwatched=True, paused=False):
    cam = _make_cam(paused=paused)
    cam.entry = SimpleNamespace(entry_id="album", title="Test")
    cam.store.pause_when_unwatched = enabled
    cam._viewers = SimpleNamespace(is_unwatched=lambda entry_id: unwatched and entry_id == "album")
    return cam


@pytest.mark.parametrize(("enabled", "unwatched", "paused", "held"), [
    (True, True, False, True),
    (True, False, False, False),
    (False, True, False, False),
    (False, False, True, True),
    (True, False, True, True),
])
def test_slideshow_holds_only_when_enabled_and_unwatched_or_paused(enabled, unwatched, paused, held):
    cam = _viewer_cam(enabled=enabled, unwatched=unwatched, paused=paused)
    assert cam._held is held
    assert cam._paused_unwatched is (enabled and unwatched)


def test_unwatched_slideshow_waits_past_its_deadline_until_a_viewer_returns():
    cam = _viewer_cam()

    async def run():
        waiter = asyncio.create_task(cam._wait_or_interrupt(timeout=0.01))
        await asyncio.sleep(0.05)
        assert not waiter.done()
        cam._viewers.is_unwatched = lambda entry_id: False
        cam._slide_deadline = 123.0
        cam._on_viewers_change()
        assert cam._slide_deadline is None
        return await waiter

    assert asyncio.run(run()) is True
    assert cam._state_writes == 1


def test_viewer_changes_are_ignored_while_the_setting_is_off():
    cam = _viewer_cam(enabled=False)
    cam._slide_deadline = 123.0
    cam._on_viewers_change()
    assert cam._slide_deadline == 123.0
    assert not cam._interrupt_event.is_set()
    assert cam._state_writes == 0


def test_the_render_loop_does_not_advance_while_unwatched(monkeypatch):
    cam = _viewer_cam()
    cam._current_frame = object()
    cam.store.slide_interval = 0
    cam._slide_hold_seconds = lambda: 0.0
    advanced = []

    async def show_next():
        advanced.append(True)
        return True

    cam._show_next_frame = show_next

    async def run():
        loop = asyncio.create_task(cam._render_loop())
        await asyncio.sleep(0.05)
        assert advanced == []
        cam._viewers.is_unwatched = lambda entry_id: False
        cam._on_viewers_change()
        await asyncio.sleep(0.05)
        loop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop

    asyncio.run(run())
    assert advanced


def test_turning_the_setting_on_gives_cards_a_grace_period_to_check_in():
    from .test_camera_next_slide import _history_cam

    cam, _update, store, _items, _frames = _history_cam()
    restarts = []
    cam._viewers = SimpleNamespace(
        async_restart_grace=restarts.append, is_unwatched=lambda entry_id: False,
    )
    cam._unsub_viewers = lambda: None
    store.pause_when_unwatched = True
    store.notify()
    store.notify()
    assert restarts == ["test"]
    assert cam.extra_state_attributes["pause_when_unwatched"] is True
    store.pause_when_unwatched = False
    store.notify()
    assert cam.extra_state_attributes["pause_when_unwatched"] is False
    store.pause_when_unwatched = True
    store.notify()
    assert restarts == ["test", "test"]


def test_the_setting_keeps_rendered_history_like_pause():
    from .test_camera_next_slide import _history_cam

    cam, _update, store, _items, frames = _history_cam()
    store.pause_when_unwatched = True
    store.notify()
    assert not cam._timeline_dirty
    assert cam._current_frame is frames[2]


# ── switch ──────────────────────────────────────────────────────────────────


@pytest.fixture
def switch_module(monkeypatch):
    stub = types.ModuleType("homeassistant.components.switch")
    stub.SwitchEntity = type("SwitchEntity", (), {})
    monkeypatch.setitem(sys.modules, "homeassistant.components.switch", stub)
    monkeypatch.delitem(sys.modules, "custom_components.album_slideshow.switch", raising=False)
    from custom_components.album_slideshow import switch

    return switch


def test_switch_defaults_off_toggles_the_store_and_restores(switch_module):
    store = SlideshowStore()
    notifications = []
    store.add_listener(lambda: notifications.append(store.pause_when_unwatched))
    control = switch_module.PauseWhenUnwatchedSwitch(SimpleNamespace(entry_id="album", title="A"), store)
    assert control._attr_unique_id == "album_pause_when_unwatched"
    assert not control.is_on
    asyncio.run(control.async_turn_on())
    assert control.is_on
    asyncio.run(control.async_turn_off())
    assert notifications == [True, False]

    restored_store = SlideshowStore()
    restored = switch_module.PauseWhenUnwatchedSwitch(SimpleNamespace(entry_id="album", title="A"), restored_store)
    restored.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state="on"))
    asyncio.run(restored.async_added_to_hass())
    assert restored_store.pause_when_unwatched is True
    # The manual pause switch keeps its own state.
    assert restored_store.paused is False


# ── websocket command ───────────────────────────────────────────────────────


@pytest.fixture
def websocket(monkeypatch):
    registered = []
    stub = types.ModuleType("homeassistant.components.websocket_api")
    stub.ERR_NOT_FOUND = "not_found"
    stub.websocket_command = lambda schema: (lambda handler: setattr(handler, "schema", schema) or handler)
    stub.async_register_command = lambda hass, handler: registered.append(handler)
    monkeypatch.setitem(sys.modules, "homeassistant.components.websocket_api", stub)
    monkeypatch.setattr(sys.modules["homeassistant.components"], "websocket_api", stub, raising=False)
    entries = {
        "album": SimpleNamespace(domain=DOMAIN),
        "other": SimpleNamespace(domain="light"),
    }
    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(async_get_entry=entries.get),
    )
    viewers.async_register_websocket(hass)
    assert len(registered) == 1
    return hass, registered[0]


class _Connection:
    def __init__(self):
        self.results, self.errors = [], []

    def send_result(self, msg_id, result=None):
        self.results.append(msg_id)

    def send_error(self, msg_id, code, message):
        self.errors.append((msg_id, code))


def test_websocket_heartbeat_marks_the_album_watched(websocket):
    hass, handler = websocket
    assert WS_VIEWER_HEARTBEAT in handler.schema.values()

    async def run():
        tracker = viewers.async_get_tracker(hass)
        tracker._grace = GRACE
        tracker.async_track("album", lambda: None)
        await asyncio.sleep(GRACE * 1.5)
        assert tracker.is_unwatched("album")
        connection = _Connection()
        handler(hass, connection, {"id": 7, "type": WS_VIEWER_HEARTBEAT, "entry_id": "album"})
        assert connection.results == [7]
        assert not tracker.is_unwatched("album")

    asyncio.run(run())


@pytest.mark.parametrize("entry_id", ["missing", "other"])
def test_websocket_heartbeat_rejects_unknown_or_foreign_entries(websocket, entry_id):
    hass, handler = websocket
    connection = _Connection()
    handler(hass, connection, {"id": 3, "type": WS_VIEWER_HEARTBEAT, "entry_id": entry_id})
    assert connection.errors == [(3, "not_found")]
    assert connection.results == []
    assert "viewers" not in hass.data.get(DOMAIN, {})


def test_watched_and_unwatched_transitions_are_logged_once(caplog):
    async def run():
        tracker = viewers.ViewerTracker(GRACE)
        _tracked(tracker)
        await asyncio.sleep(GRACE * 1.5)
        tracker.async_heartbeat("album")
        # A second check-in while already watched is not a transition.
        tracker.async_heartbeat("album")
        close = tracker.async_add_stream("album")
        close()

    with caplog.at_level("DEBUG", logger="custom_components.album_slideshow.viewers"):
        asyncio.run(run())
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        f"Viewers album: no card or stream for {GRACE}s, now unwatched",
        "Viewers album: a card checked in, watched again",
        "Viewers album: live stream opened (1 open)",
        "Viewers album: live stream closed (0 open)",
    ]
