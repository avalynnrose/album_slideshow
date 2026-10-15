"""iCloud signed links: renewed when they expire, cached across re-signing (#57)."""
from __future__ import annotations

import asyncio
import logging
import time
import types

import pytest
from aiohttp import ClientResponseError, RequestInfo
from multidict import CIMultiDict
from yarl import URL

from custom_components.album_slideshow import camera
from custom_components.album_slideshow import coordinator as coordinator_module
from custom_components.album_slideshow import icloud as ic
from custom_components.album_slideshow.const import PROVIDER_ICLOUD, PROVIDER_IMMICH
from custom_components.album_slideshow.coordinator import MediaItem


def _link(path: str, signature: str, expires_in: float = 900) -> str:
    expiry = int(time.time() + expires_in)
    return f"https://cvws-h2.icloud-content.com/B/{path}/IMG.JPG?o=x&e={expiry}&s={signature}"


def _item(url: str, source_id: str = "rec1") -> MediaItem:
    return MediaItem(
        url=url, width=None, height=None, mime_type=None, filename=None, source_id=source_id,
    )


class _Hass:
    def async_create_background_task(self, coro, name):
        return asyncio.ensure_future(coro)


def _coordinator(urls, *, provider=PROVIDER_ICLOUD, renewed=None):
    """Coordinator whose refresh signs every link again (or swaps in ``renewed``)."""
    coord = coordinator_module.AlbumCoordinator.__new__(coordinator_module.AlbumCoordinator)
    coord.hass = _Hass()
    coord.provider = provider
    coord.entry = types.SimpleNamespace(entry_id="entry", title="Family", data={})
    coord.data = {"items": [_item(url, f"rec{i}") for i, url in enumerate(urls)]}
    refreshes = []

    async def refresh():
        refreshes.append(True)
        await asyncio.sleep(0)
        fresh = renewed if renewed is not None else [
            url.replace("&s=", "&s=new") for url in urls
        ]
        coord.data = {"items": [_item(url, f"rec{i}") for i, url in enumerate(fresh)]}

    coord.async_refresh = refresh
    return coord, refreshes


def _camera(coord, responses):
    """Camera whose downloads come from ``responses``: url -> bytes, status or None."""
    cam = camera.AlbumSlideshowCamera.__new__(camera.AlbumSlideshowCamera)
    cam.coordinator = coord
    cam._download_cache = camera._DownloadCache(1024 * 1024)
    requests = []

    async def http_get(url, *, signed=False):
        requests.append(url)
        result = responses.get(url)
        if isinstance(result, int):
            if signed:
                raise camera._ExpiredLink
            return None
        return result

    cam._http_get = http_get
    return cam, requests


# ── Link helpers ────────────────────────────────────────────────────────────


def test_link_key_ignores_the_signature():
    old, new = _link("AbC", "one"), _link("AbC", "two", expires_in=1800)
    assert old != new
    assert ic.link_key(old) == ic.link_key(new) == "/B/AbC/IMG.JPG"
    assert ic.link_key(_link("XyZ", "one")) != ic.link_key(old)


def test_link_expiry_comes_from_the_e_parameter():
    assert ic.link_expires_at("https://x.icloud-content.com/a.jpg?o=1&e=1792000000") == 1792000000
    assert ic.link_expires_at("https://x.icloud-content.com/a.jpg?o=1") is None
    assert ic.link_expires_at("https://x.icloud-content.com/a.jpg?e=soon") is None


# ── Coordinator ─────────────────────────────────────────────────────────────


def test_download_key_is_stable_only_for_icloud():
    old, new = _link("AbC", "one"), _link("AbC", "two")
    coord, _ = _coordinator([old])
    assert coord.download_key(old) == coord.download_key(new)
    immich, _ = _coordinator([old], provider=PROVIDER_IMMICH)
    assert immich.download_key(old) == old
    assert immich.signed_links is False


def test_link_expired_renews_a_minute_early():
    coord, _ = _coordinator([])
    assert coord.link_expired(_link("A", "s", expires_in=30))
    assert coord.link_expired(_link("A", "s", expires_in=-5))
    assert not coord.link_expired(_link("A", "s", expires_in=600))
    assert not coord.link_expired("https://cvws.icloud-content.com/B/A/IMG.JPG?o=x")
    immich, _ = _coordinator([], provider=PROVIDER_IMMICH)
    assert not immich.link_expired(_link("A", "s", expires_in=-5))


def test_expired_link_refreshes_the_album_once_for_concurrent_callers():
    first, second = _link("A", "one"), _link("B", "one")
    coord, refreshes = _coordinator([first, second])

    async def main():
        return await asyncio.gather(
            coord.async_renew_link(first), coord.async_renew_link(second)
        )

    renewed = asyncio.run(main())
    assert refreshes == [True]
    assert renewed == [first.replace("&s=", "&s=new"), second.replace("&s=", "&s=new")]


def test_link_already_renewed_by_a_refresh_is_returned_without_refreshing():
    old, current = _link("A", "one"), _link("A", "two")
    coord, refreshes = _coordinator([current])
    assert asyncio.run(coord.async_renew_link(old)) == current
    assert refreshes == []


def test_refreshes_for_new_links_run_at_most_once_a_minute():
    url = _link("A", "one")
    coord, refreshes = _coordinator([url], renewed=[url])
    assert asyncio.run(coord.async_renew_link(url)) is None
    assert asyncio.run(coord.async_renew_link(url)) is None
    assert refreshes == [True]
    coord._links_renewed_at = time.monotonic() - 61
    asyncio.run(coord.async_renew_link(url))
    assert refreshes == [True, True]


def test_removed_photo_and_other_providers_never_refresh():
    coord, refreshes = _coordinator([_link("A", "one")])
    assert asyncio.run(coord.async_renew_link(_link("Gone", "one"))) is None
    immich, immich_refreshes = _coordinator([_link("A", "one")], provider=PROVIDER_IMMICH)
    assert asyncio.run(immich.async_renew_link(_link("A", "one"))) is None
    assert refreshes == immich_refreshes == []


# ── Camera ──────────────────────────────────────────────────────────────────


def test_downloaded_photo_is_reused_after_its_link_is_signed_again():
    old, new = _link("A", "one"), _link("A", "two")
    coord, _ = _coordinator([old])
    cam, requests = _camera(coord, {old: b"photo"})
    assert asyncio.run(cam._fetch_bytes(old)) == b"photo"
    assert asyncio.run(cam._fetch_bytes(new)) == b"photo"
    assert requests == [old]


def test_link_past_its_expiry_is_renewed_before_downloading():
    stale = _link("A", "one", expires_in=-120)
    coord, refreshes = _coordinator([stale])
    fresh = stale.replace("&s=", "&s=new")
    cam, requests = _camera(coord, {fresh: b"photo"})
    assert asyncio.run(cam._fetch_bytes(stale)) == b"photo"
    assert refreshes == [True]
    assert requests == [fresh]


def test_gone_response_renews_the_link_and_retries_quietly(caplog):
    url = _link("A", "one")
    coord, refreshes = _coordinator([url])
    fresh = url.replace("&s=", "&s=new")
    cam, requests = _camera(coord, {url: 410, fresh: b"photo"})
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(cam._fetch_bytes(url)) == b"photo"
    assert refreshes == [True]
    assert requests == [url, fresh]
    assert not caplog.records


def test_expired_link_without_a_newer_one_is_skipped_quietly(caplog):
    url = _link("A", "one")
    coord, _ = _coordinator([url], renewed=[url])
    cam, requests = _camera(coord, {url: 410})
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(cam._fetch_bytes(url)) is None
    assert requests == [url]
    assert not caplog.records


def test_other_providers_keep_url_keys_and_no_renewal():
    url = "https://immich.test/api/assets/1/thumbnail?size=preview"
    cam, requests = _camera(types.SimpleNamespace(image_request_headers=None), {url: b"photo"})
    assert asyncio.run(cam._fetch_bytes(url)) == b"photo"
    assert list(cam._download_cache._cache) == [url]
    assert requests == [url]


class _Response:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self):
        info = RequestInfo(URL("https://x.test/a"), "GET", CIMultiDict(), URL("https://x.test/a"))
        raise ClientResponseError(info, (), status=self.status)


@pytest.mark.parametrize("status", [403, 410])
def test_http_get_reports_expired_signed_links(monkeypatch, caplog, status):
    session = types.SimpleNamespace(get=lambda url, **kwargs: _Response(status))
    monkeypatch.setattr(camera, "async_get_clientsession", lambda hass, verify_ssl=True: session)
    cam = camera.AlbumSlideshowCamera.__new__(camera.AlbumSlideshowCamera)
    cam.hass = object()
    cam.coordinator = types.SimpleNamespace(image_request_headers=None)
    with pytest.raises(camera._ExpiredLink):
        asyncio.run(cam._http_get("https://x.test/a", signed=True))
    assert not caplog.records
    assert asyncio.run(cam._http_get("https://x.test/a")) is None
    assert f"HTTP {status}" in caplog.text
