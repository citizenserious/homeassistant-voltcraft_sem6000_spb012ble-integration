from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_MAC, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator_extended import VoltcraftDataUpdateCoordinator
from .security import install_sensitive_log_filter

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.TIME,
    Platform.TEXT,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up integration-wide privacy filtering.

    Version 2.0.0 registers no Voltcraft-specific Home Assistant services.
    User-facing operations are provided by normal entities and the integration's
    Configure GUI. Time synchronization and complete state refresh run internally.
    """
    install_sensitive_log_filter()
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry after a stored login PIN was corrected."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one Voltcraft config entry."""
    install_sensitive_log_filter()

    mac_address = entry.data[CONF_MAC]
    ble_device = bluetooth.async_ble_device_from_address(
        hass, mac_address, connectable=True
    )
    coordinator = VoltcraftDataUpdateCoordinator(
        hass,
        entry,
        mac_address,
        ble_device.name if ble_device is not None else None,
    )

    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady as err:
        _LOGGER.warning(
            "Initial Voltcraft connection unavailable; loading entities and "
            "retrying: %s",
            err,
        )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Voltcraft config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        if isinstance(coordinator, VoltcraftDataUpdateCoordinator):
            await coordinator.async_shutdown()
    return unload_ok
