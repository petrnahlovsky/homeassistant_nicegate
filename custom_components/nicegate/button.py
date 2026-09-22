"""Buttons for Nice gate T4 commands."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .nice_api import NiceGateApi

# (code, bit position in T4_allowed, name, enabled by default)
T4_BUTTONS = [
    ("MDAx", 1, "Step by step", True),
    ("MDA1", 5, "Partial opening 1", True),
    ("MDA2", 6, "Partial opening 2", False),
    ("MDA3", 7, "Partial opening 3", False),
    ("MDEx", 17, "Courtesy light timer", False),
    ("MDEy", 18, "Courtesy light on/off", False),
]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Nice gate T4 buttons."""
    api: NiceGateApi = hass.data[DOMAIN][entry.entry_id]
    mac = entry.data["mac"]
    # Ask the module for device info incl. list of supported T4 commands
    hass.async_create_task(api.info())
    async_add_entities(
        NiceT4Button(api, mac, code, bit, name, enabled)
        for code, bit, name, enabled in T4_BUTTONS
    )


class NiceT4Button(ButtonEntity):
    """Button sending one T4 command to the gate."""

    _attr_has_entity_name = True

    def __init__(self, api: NiceGateApi, mac: str, code: str, bit: int, name: str, enabled: bool) -> None:
        self._api = api
        self._code = code
        self._bit = bit
        self._attr_name = name
        self._attr_unique_id = f"{mac}_t4_{code}"
        self._attr_entity_registry_enabled_default = enabled
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, mac)}, name="Nice gate", manufacturer="Nice")

    @property
    def available(self) -> bool:
        """Hide commands the gate reports as unsupported."""
        return self._api.t4_supported(self._bit)

    async def async_press(self) -> None:
        """Send the command."""
        await self._api.t4(self._code)
