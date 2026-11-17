"""VicoHome cloud cameras and doorbells, with native WebRTC live view."""

from __future__ import annotations

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

import logging

from .api import VicoApi
from .archive import VicoArchive
from .clip_view import VicoArchiveView, VicoClipView
from .const import (
    CONF_ARCHIVE_EVENTS,
    CONF_COUNTRY,
    CONF_LANGUAGE,
    CONF_REGION,
    CONF_RETENTION_DAYS,
    DEFAULT_ARCHIVE_EVENTS,
    DEFAULT_COUNTRY,
    DEFAULT_LANGUAGE,
    DEFAULT_REGION,
    DEFAULT_RETENTION_DAYS,
    DOMAIN,
)
from .coordinator import VicoConfigEntry, VicoCoordinator
from .push import VicoPush
from .relay import RELAY_KEY, VicoRelay, VicoWhepView

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.CAMERA, Platform.EVENT, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: VicoConfigEntry) -> bool:
    """Set up VicoHome from a config entry."""
    api = VicoApi(
        async_get_clientsession(hass),
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        entry.data.get(CONF_REGION, DEFAULT_REGION),
        entry.data.get(CONF_COUNTRY, DEFAULT_COUNTRY),
        entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
    )
    if not hass.data.get(f"{DOMAIN}_view"):
        hass.http.register_view(VicoClipView(hass))
        hass.http.register_view(VicoArchiveView(hass))
        relay = VicoRelay(hass)
        hass.data[RELAY_KEY] = relay
        hass.http.register_view(VicoWhepView(hass, relay))

        async def _stop_relay(_event) -> None:  # noqa: ANN001
            await relay.async_stop()

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop_relay)
        hass.data[f"{DOMAIN}_view"] = True
    coordinator = VicoCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    # Local copy of the recordings, kept as many days as the options say.
    archive = VicoArchive(
        hass, entry.entry_id, api.session,
        days=int(entry.options.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS)),
        archive_events=entry.options.get(CONF_ARCHIVE_EVENTS, DEFAULT_ARCHIVE_EVENTS),
    )
    await archive.async_load()
    coordinator.archive = archive
    archive.start(lambda coro, name: entry.async_create_background_task(hass, coro, name))

    @callback
    def _sync_archive() -> None:
        if coordinator.data:
            archive.async_sync([ev for cam in coordinator.data.values() for ev in cam.events])

    entry.async_on_unload(coordinator.async_add_listener(_sync_archive))
    _sync_archive()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Instant updates via the app's push channel; polling keeps working if this fails.
    push = VicoPush(hass, coordinator, entry.entry_id)
    coordinator.push = push
    entry.async_create_background_task(hass, push.async_run(), f"{DOMAIN}_push_start")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: VicoConfigEntry) -> bool:
    """Unload a config entry."""
    if push := getattr(entry.runtime_data, "push", None):
        await push.async_stop()
    if archive := getattr(entry.runtime_data, "archive", None):
        await archive.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
