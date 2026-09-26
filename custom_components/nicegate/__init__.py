"""The Nice gate integration."""
from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import CONF_RELOAD_INTERVAL, DEFAULT_RELOAD_INTERVAL, DOMAIN
from .nice_api import NiceGateApi

_LOGGER = logging.getLogger("nicegate")

# List the platforms that you want to support.
PLATFORMS: list[Platform] = [Platform.COVER, Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Nice gate from a config entry."""

    hass.data.setdefault(DOMAIN, {})
    api = NiceGateApi(
        entry.data["host"],
        entry.data["mac"],
        entry.data["username"],
        entry.data["password"],
        entry.data.get("source"),
    )
    hass.data[DOMAIN][entry.entry_id] = api

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.warning("Integrace brány načtena, stav: %s", api.gate_status)

    interval = entry.options.get(CONF_RELOAD_INTERVAL, DEFAULT_RELOAD_INTERVAL)
    if interval and interval > 0:

        @callback
        def _periodic_reload(_now) -> None:
            """Reload the entry, the same thing as the Reload button in the UI."""
            _LOGGER.debug("Periodic reload of Nice gate integration")
            hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))

        entry.async_on_unload(
            async_track_time_interval(
                hass, _periodic_reload, timedelta(minutes=interval)
            )
        )

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        api = hass.data[DOMAIN].pop(entry.entry_id, None)
        if api is not None:
            await api.shutdown()

    return unload_ok
