"""Track whether anyone is looking at a slideshow.

Dashboard cards check in over the websocket while they are on screen, and
live camera streams (more-info, picture-glance) count for as long as they
stay open. An album is *unwatched* once ``VIEWER_GRACE_SECONDS`` pass with
neither. The camera holds its slide timer while unwatched if the user turned
on "Pause when not viewed".

State lives in ``hass.data`` rather than on the camera so check-ins keep
counting across an album reload.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import Callable

from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, VIEWER_GRACE_SECONDS, WS_VIEWER_HEARTBEAT

_LOGGER = logging.getLogger(__name__)

Listener = Callable[[], None]


@dataclass
class _AlbumViewers:
    streams: int = 0
    unwatched: bool = False
    timer: asyncio.TimerHandle | None = None
    listeners: list[Listener] = field(default_factory=list)


class ViewerTracker:
    """Per-album viewer state shared by the cards, streams and cameras."""

    def __init__(self, grace_seconds: float = VIEWER_GRACE_SECONDS) -> None:
        self._grace = grace_seconds
        self._albums: dict[str, _AlbumViewers] = {}

    def is_unwatched(self, entry_id: str) -> bool:
        album = self._albums.get(entry_id)
        return album is not None and album.unwatched

    @callback
    def async_track(self, entry_id: str, listener: Listener) -> Callable[[], None]:
        """Call ``listener`` whenever the album becomes watched or unwatched.

        Starts the grace period if nothing is watching yet, so after a restart
        the cards have time to reconnect before the slideshow pauses.
        """
        album = self._albums.setdefault(entry_id, _AlbumViewers())
        album.listeners.append(listener)
        if not album.streams and not album.unwatched and album.timer is None:
            self._restart_timer(entry_id, album)

        @callback
        def untrack() -> None:
            if listener in album.listeners:
                album.listeners.remove(listener)
            if not album.listeners and not album.streams and self._albums.get(entry_id) is album:
                if album.timer is not None:
                    album.timer.cancel()
                del self._albums[entry_id]

        return untrack

    @callback
    def async_heartbeat(self, entry_id: str) -> None:
        """A card is on screen; it counts until the grace period runs out."""
        album = self._albums.setdefault(entry_id, _AlbumViewers())
        self._set_watched(entry_id, album, "a card checked in")
        if not album.streams:
            self._restart_timer(entry_id, album)

    @callback
    def async_add_stream(self, entry_id: str) -> Callable[[], None]:
        """A live stream is open; it counts until the returned callback runs."""
        album = self._albums.setdefault(entry_id, _AlbumViewers())
        album.streams += 1
        _LOGGER.debug("Viewers %s: live stream opened (%d open)", entry_id, album.streams)
        if album.timer is not None:
            album.timer.cancel()
            album.timer = None
        self._set_watched(entry_id, album, "a live stream opened")
        closed = False

        @callback
        def close() -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            album.streams -= 1
            _LOGGER.debug("Viewers %s: live stream closed (%d open)", entry_id, album.streams)
            if not album.streams:
                self._restart_timer(entry_id, album)

        return close

    def _restart_timer(self, entry_id: str, album: _AlbumViewers) -> None:
        if album.timer is not None:
            album.timer.cancel()
        album.timer = asyncio.get_running_loop().call_later(
            self._grace, self._expire, entry_id, album
        )

    @callback
    def _expire(self, entry_id: str, album: _AlbumViewers) -> None:
        album.timer = None
        if album.streams or album.unwatched:
            return
        album.unwatched = True
        _LOGGER.debug(
            "Viewers %s: no card or stream for %ss, now unwatched", entry_id, self._grace
        )
        self._notify(album)

    def _set_watched(self, entry_id: str, album: _AlbumViewers, reason: str) -> None:
        if album.unwatched:
            album.unwatched = False
            _LOGGER.debug("Viewers %s: %s, watched again", entry_id, reason)
            self._notify(album)

    @staticmethod
    def _notify(album: _AlbumViewers) -> None:
        for listener in list(album.listeners):
            listener()


def async_get_tracker(hass: HomeAssistant) -> ViewerTracker:
    return hass.data.setdefault(DOMAIN, {}).setdefault("viewers", ViewerTracker())


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    """Register the command cards use to check in while they are on screen."""
    # Imported lazily so the module still loads in test environments that
    # stub out homeassistant.
    import voluptuous as vol
    from homeassistant.components import websocket_api

    @websocket_api.websocket_command(
        {vol.Required("type"): WS_VIEWER_HEARTBEAT, vol.Required("entry_id"): str}
    )
    @callback
    def ws_viewer_heartbeat(hass: HomeAssistant, connection, msg) -> None:
        entry = hass.config_entries.async_get_entry(msg["entry_id"])
        if entry is None or entry.domain != DOMAIN:
            connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown slideshow")
            return
        async_get_tracker(hass).async_heartbeat(msg["entry_id"])
        connection.send_result(msg["id"])

    websocket_api.async_register_command(hass, ws_viewer_heartbeat)
