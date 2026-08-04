from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_MAC
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.selector import (
    DateSelector,
    DurationSelector,
    DurationSelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
)
from homeassistant.util import dt as dt_util

from .const import CONF_PIN, DEFAULT_PIN, DEVICE_NAME, DOMAIN, SERVICE_UUID
from .coordinator_extended import VoltcraftDataUpdateCoordinator
from .protocol import ScheduleEntry, TimerAction, normalize_pin

_LOGGER = logging.getLogger(__name__)

_PIN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_DATE_SELECTOR = DateSelector()
_TIME_SELECTOR = TimeSelector()
_DURATION_SELECTOR = DurationSelector(
    DurationSelectorConfig(enable_day=True, enable_second=True)
)
_ACTION_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=["turn_on", "turn_off"],
        translation_key="switch_action",
        mode=SelectSelectorMode.DROPDOWN,
    )
)
_STATUS_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=["enabled", "disabled"],
        translation_key="schedule_status",
        mode=SelectSelectorMode.DROPDOWN,
    )
)
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=list(_WEEKDAYS),
        translation_key="weekdays",
        multiple=True,
        mode=SelectSelectorMode.DROPDOWN,
        sort=False,
    )
)

CONF_NEW_PIN = "new_pin"
CONF_REPEAT_PIN = "repeat_pin"
CONF_CONFIRM = "confirm"
CONF_ACTION = "action"
CONF_DURATION = "duration"
CONF_DATE = "date"
CONF_TIME = "time"
CONF_STATUS = "status"
CONF_WEEKDAYS = "weekdays"
CONF_SLOT = "slot"

ACTION_TURN_ON = "turn_on"
ACTION_TURN_OFF = "turn_off"
STATUS_ENABLED = "enabled"
STATUS_DISABLED = "disabled"


def _local_naive(date_value: Any, time_value: Any) -> datetime:
    """Combine local date and time selector values without timezone duplication."""
    return datetime.combine(cv.date(date_value), cv.time(time_value))


def _duration_seconds(value: Any) -> int:
    """Convert a native Home Assistant duration selector value to seconds."""
    return int(cv.positive_time_period_dict(value).total_seconds())


def _weekday_values(user_input: dict[str, Any]) -> list[str]:
    selected = user_input.get(CONF_WEEKDAYS, [])
    return [weekday for weekday in selected if weekday in _WEEKDAYS]


class VoltcraftConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovered_devices: dict[str, str] = {}
        self._mac_address: str | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(format_mac(discovery_info.address))
        self._abort_if_unique_id_configured()
        self._mac_address = discovery_info.address
        self._name = discovery_info.name
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                pin = normalize_pin(user_input[CONF_PIN])
            except (KeyError, ValueError):
                errors["base"] = "invalid_pin"
            else:
                return self._create_entry(pin)

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_PIN, default=DEFAULT_PIN): _PIN_SELECTOR}
            ),
            errors=errors,
            description_placeholders={"name": self._name},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_MAC]
            await self.async_set_unique_id(
                format_mac(address), raise_on_progress=False
            )
            self._abort_if_unique_id_configured()
            self._mac_address = address
            self._name = self._discovered_devices[address]
            return await self.async_step_confirm()

        current_ids = self._async_current_ids()
        for discovery in async_discovered_service_info(self.hass):
            if (
                discovery.address not in current_ids
                and discovery.address not in self._discovered_devices
                and SERVICE_UUID in discovery.service_uuids
            ):
                self._discovered_devices[discovery.address] = (
                    f"{discovery.name} ({discovery.address})"
                )

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_MAC): vol.In(self._discovered_devices)}
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Create the options flow for the integration Configure dialog."""
        return VoltcraftOptionsFlow()

    @property
    def _name(self) -> str:
        return self.context.get("title_placeholders", {}).get("name") or DEVICE_NAME

    @_name.setter
    def _name(self, value: str | None) -> None:
        self.context["title_placeholders"] = {"name": value or DEVICE_NAME}

    def _create_entry(self, pin: str) -> ConfigFlowResult:
        return self.async_create_entry(
            title=self._name,
            data={CONF_MAC: self._mac_address, CONF_PIN: pin},
        )


class VoltcraftOptionsFlow(OptionsFlow):
    """Provide all advanced Voltcraft operations through normal HA forms."""

    def __init__(self) -> None:
        self._selected_schedule_slot: int | None = None

    def _coordinator(self) -> VoltcraftDataUpdateCoordinator:
        coordinator = self.hass.data.get(DOMAIN, {}).get(
            self.config_entry.entry_id
        )
        if not isinstance(coordinator, VoltcraftDataUpdateCoordinator):
            raise HomeAssistantError(
                "The Voltcraft integration is not currently loaded"
            )
        return coordinator

    @staticmethod
    def _operation_error(err: Exception) -> dict[str, str]:
        detail = str(err).strip() or type(err).__name__
        return {"error": detail}

    def _timer_status(self, coordinator: VoltcraftDataUpdateCoordinator) -> str:
        """Return a concise localized summary for the timer submenu."""
        language = str(getattr(self.hass.config, "language", "en")).lower()
        is_german = language.startswith("de")
        data = coordinator.data
        if data is None:
            return "Unbekannt" if is_german else "Unknown"
        if data.timer_action is TimerAction.INACTIVE:
            return "Kein aktiver Timer" if is_german else "No active timer"

        action = (
            "Einschalten"
            if is_german and data.timer_action is TimerAction.TURN_ON
            else "Ausschalten"
            if is_german
            else "Turn on"
            if data.timer_action is TimerAction.TURN_ON
            else "Turn off"
        )
        target = data.timer_target
        if target is None:
            return action
        formatted = (
            target.strftime("%d.%m.%Y um %H:%M:%S")
            if is_german
            else target.strftime("%Y-%m-%d at %H:%M:%S")
        )
        return f"{action}: {formatted}"

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["access", "timer", "schedule", "maintenance"],
        )

    async def async_step_back_to_overview(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Return from an options submenu to the Voltcraft overview."""
        return await self.async_step_init()

    async def async_step_access(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="access",
            menu_options=[
                "back_to_overview",
                "login_pin",
                "change_pin",
                "reset_pin",
            ],
        )

    async def async_step_timer(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        coordinator = self._coordinator()
        try:
            await coordinator.async_refresh_timer()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Timer refresh before GUI display failed: %s", err)

        return self.async_show_menu(
            step_id="timer",
            menu_options=[
                "back_to_overview",
                "timer_delay",
                "timer_at",
                "timer_stop",
            ],
            description_placeholders={
                "timer_status": self._timer_status(coordinator),
            },
        )

    async def async_step_schedule(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="schedule",
            menu_options=[
                "back_to_overview",
                "schedule_add",
                "schedule_edit_select",
                "schedule_remove",
            ],
        )

    async def async_step_maintenance(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="maintenance",
            menu_options=["back_to_overview", "reset_consumption", "factory_reset"],
        )

    async def async_step_login_pin(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                pin = normalize_pin(user_input[CONF_PIN])
            except (KeyError, ValueError):
                errors["base"] = "invalid_pin"
            else:
                options = dict(self.config_entry.options)
                options[CONF_PIN] = pin
                return self.async_create_entry(title="", data=options)

        return self.async_show_form(
            step_id="login_pin",
            data_schema=vol.Schema({vol.Required(CONF_PIN): _PIN_SELECTOR}),
            errors=errors,
        )

    async def async_step_change_pin(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            try:
                new_pin = normalize_pin(user_input[CONF_NEW_PIN])
                repeat_pin = normalize_pin(user_input[CONF_REPEAT_PIN])
            except (KeyError, ValueError):
                errors["base"] = "invalid_pin"
            else:
                if new_pin != repeat_pin:
                    errors["base"] = "pin_mismatch"
                else:
                    try:
                        await self._coordinator().async_change_pin(new_pin)
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug("GUI PIN change failed", exc_info=True)
                        errors["base"] = "operation_failed"
                        placeholders = self._operation_error(err)
                    else:
                        return self.async_abort(reason="pin_changed")

        return self.async_show_form(
            step_id="change_pin",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NEW_PIN): _PIN_SELECTOR,
                    vol.Required(CONF_REPEAT_PIN): _PIN_SELECTOR,
                }
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reset_pin(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_CONFIRM, False):
                errors["base"] = "confirmation_required"
            else:
                try:
                    await self._coordinator().async_reset_pin()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("GUI PIN reset failed", exc_info=True)
                    errors["base"] = "operation_failed"
                    placeholders = self._operation_error(err)
                else:
                    return self.async_abort(reason="pin_reset")

        return self.async_show_form(
            step_id="reset_pin",
            data_schema=vol.Schema(
                {vol.Required(CONF_CONFIRM, default=False): cv.boolean}
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_timer_delay(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            try:
                seconds = _duration_seconds(user_input[CONF_DURATION])
            except (KeyError, TypeError, ValueError, vol.Invalid):
                errors["base"] = "invalid_duration"
            else:
                if seconds < 1 or seconds > 31_536_000:
                    errors["base"] = "invalid_duration"
                else:
                    try:
                        await self._coordinator().async_set_timer_delay(
                            user_input[CONF_ACTION] == ACTION_TURN_ON,
                            seconds,
                        )
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug("GUI timer creation failed", exc_info=True)
                        errors["base"] = "operation_failed"
                        placeholders = self._operation_error(err)
                    else:
                        return self.async_abort(reason="timer_started")

        return self.async_show_form(
            step_id="timer_delay",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ACTION, default=ACTION_TURN_ON
                    ): _ACTION_SELECTOR,
                    vol.Required(CONF_DURATION): _DURATION_SELECTOR,
                }
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_timer_at(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            try:
                target = _local_naive(
                    user_input[CONF_DATE], user_input[CONF_TIME]
                )
            except (KeyError, TypeError, ValueError, vol.Invalid):
                errors["base"] = "invalid_datetime"
            else:
                try:
                    await self._coordinator().async_set_timer(
                        user_input[CONF_ACTION] == ACTION_TURN_ON,
                        target,
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug(
                        "GUI absolute timer creation failed", exc_info=True
                    )
                    errors["base"] = "operation_failed"
                    placeholders = self._operation_error(err)
                else:
                    return self.async_abort(reason="timer_started")

        return self.async_show_form(
            step_id="timer_at",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ACTION, default=ACTION_TURN_ON
                    ): _ACTION_SELECTOR,
                    vol.Required(CONF_DATE): _DATE_SELECTOR,
                    vol.Required(CONF_TIME): _TIME_SELECTOR,
                }
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_timer_stop(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._coordinator().async_stop_timer()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("GUI timer stop failed", exc_info=True)
                errors["base"] = "operation_failed"
                placeholders = self._operation_error(err)
            else:
                return self.async_abort(reason="timer_stopped")

        return self.async_show_form(
            step_id="timer_stop",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders=placeholders,
        )

    def _schedule_schema(
        self, entry: ScheduleEntry | None = None
    ) -> vol.Schema:
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_STATUS,
                default=(
                    STATUS_ENABLED
                    if entry is None or entry.active
                    else STATUS_DISABLED
                ),
            ): _STATUS_SELECTOR,
            vol.Required(
                CONF_ACTION,
                default=(
                    ACTION_TURN_ON
                    if entry is None or entry.turn_on
                    else ACTION_TURN_OFF
                ),
            ): _ACTION_SELECTOR,
        }

        if entry is None:
            schema[vol.Required(CONF_DATE)] = _DATE_SELECTOR
            schema[vol.Required(CONF_TIME)] = _TIME_SELECTOR
            selected_weekdays: list[str] = []
        else:
            now = dt_util.now().replace(second=0, microsecond=0)
            at = entry.when or now.replace(hour=entry.hour, minute=entry.minute)
            schema[
                vol.Required(CONF_DATE, default=at.date().isoformat())
            ] = _DATE_SELECTOR
            schema[
                vol.Required(
                    CONF_TIME,
                    default=at.time().isoformat(timespec="seconds"),
                )
            ] = _TIME_SELECTOR
            selected_weekdays = list(entry.weekdays)

        schema[
            vol.Required(CONF_WEEKDAYS, default=selected_weekdays)
        ] = _WEEKDAY_SELECTOR
        return vol.Schema(schema)

    async def _async_refresh_schedules(self) -> VoltcraftDataUpdateCoordinator:
        coordinator = self._coordinator()
        try:
            await coordinator.async_refresh_schedules()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Schedule refresh before GUI selection failed", exc_info=True)
            data = coordinator.data
            if data is None or not data.schedules:
                raise HomeAssistantError(str(err) or type(err).__name__) from err
        return coordinator

    def _schedule_choices(
        self, coordinator: VoltcraftDataUpdateCoordinator
    ) -> dict[str, str]:
        data = coordinator.data
        if data is None:
            return {}

        language = str(getattr(self.hass.config, "language", "en"))
        is_german = language.lower().startswith("de")
        weekday_labels = (
            {
                "mon": "Mo",
                "tue": "Di",
                "wed": "Mi",
                "thu": "Do",
                "fri": "Fr",
                "sat": "Sa",
                "sun": "So",
            }
            if is_german
            else {
                "mon": "Mon",
                "tue": "Tue",
                "wed": "Wed",
                "thu": "Thu",
                "fri": "Fri",
                "sat": "Sat",
                "sun": "Sun",
            }
        )
        on_label, off_label = (
            ("Einschalten", "Ausschalten")
            if is_german
            else ("Turn on", "Turn off")
        )
        active_label, inactive_label = (
            ("aktiv", "inaktiv") if is_german else ("active", "inactive")
        )
        once_label = "einmalig" if is_german else "once"

        choices: dict[str, str] = {}
        for entry in data.schedules:
            slot = coordinator.schedule_user_slot(entry.slot_id)
            weekdays = ", ".join(
                weekday_labels.get(weekday, weekday) for weekday in entry.weekdays
            )
            date_or_days = weekdays or (
                entry.when.date().isoformat()
                if entry.when is not None
                else once_label
            )
            action = on_label if entry.turn_on else off_label
            status = active_label if entry.active else inactive_label
            choices[str(slot)] = (
                f"{slot}: {action} {entry.at_time.strftime('%H:%M')} - "
                f"{date_or_days} - {status}"
            )
        return dict(sorted(choices.items(), key=lambda item: int(item[0])))

    @staticmethod
    def _find_schedule(
        coordinator: VoltcraftDataUpdateCoordinator, slot: int
    ) -> ScheduleEntry | None:
        data = coordinator.data
        if data is None:
            return None
        return next(
            (
                entry
                for entry in data.schedules
                if coordinator.schedule_user_slot(entry.slot_id) == slot
            ),
            None,
        )

    async def async_step_schedule_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            try:
                target = _local_naive(
                    user_input[CONF_DATE], user_input[CONF_TIME]
                )
            except (KeyError, TypeError, ValueError, vol.Invalid):
                errors["base"] = "invalid_datetime"
            else:
                try:
                    await self._coordinator().async_set_schedule(
                        slot=None,
                        active=user_input[CONF_STATUS] == STATUS_ENABLED,
                        turn_on=user_input[CONF_ACTION] == ACTION_TURN_ON,
                        weekdays=_weekday_values(user_input),
                        when=target,
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug(
                        "GUI schedule creation failed", exc_info=True
                    )
                    errors["base"] = "operation_failed"
                    placeholders = self._operation_error(err)
                else:
                    return self.async_abort(reason="schedule_saved")

        return self.async_show_form(
            step_id="schedule_add",
            data_schema=self._schedule_schema(),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_schedule_edit_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        try:
            coordinator = await self._async_refresh_schedules()
        except Exception as err:  # noqa: BLE001
            return self.async_abort(
                reason="schedule_load_failed",
                description_placeholders=self._operation_error(err),
            )

        choices = self._schedule_choices(coordinator)
        if not choices:
            return self.async_abort(reason="no_schedules")

        if user_input is not None:
            self._selected_schedule_slot = int(user_input[CONF_SLOT])
            return await self.async_step_schedule_edit()

        return self.async_show_form(
            step_id="schedule_edit_select",
            data_schema=vol.Schema(
                {vol.Required(CONF_SLOT): vol.In(choices)}
            ),
        )

    async def async_step_schedule_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._selected_schedule_slot is None:
            return await self.async_step_schedule_edit_select()

        coordinator = self._coordinator()
        entry = self._find_schedule(coordinator, self._selected_schedule_slot)
        if entry is None:
            return self.async_abort(reason="no_schedules")

        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            try:
                target = _local_naive(
                    user_input[CONF_DATE], user_input[CONF_TIME]
                )
            except (KeyError, TypeError, ValueError, vol.Invalid):
                errors["base"] = "invalid_datetime"
            else:
                try:
                    await coordinator.async_set_schedule(
                        slot=self._selected_schedule_slot,
                        active=user_input[CONF_STATUS] == STATUS_ENABLED,
                        turn_on=user_input[CONF_ACTION] == ACTION_TURN_ON,
                        weekdays=_weekday_values(user_input),
                        when=target,
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("GUI schedule edit failed", exc_info=True)
                    errors["base"] = "operation_failed"
                    placeholders = self._operation_error(err)
                else:
                    return self.async_abort(reason="schedule_saved")

        return self.async_show_form(
            step_id="schedule_edit",
            data_schema=self._schedule_schema(entry),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_schedule_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        try:
            coordinator = await self._async_refresh_schedules()
        except Exception as err:  # noqa: BLE001
            return self.async_abort(
                reason="schedule_load_failed",
                description_placeholders=self._operation_error(err),
            )

        choices = self._schedule_choices(coordinator)
        if not choices:
            return self.async_abort(reason="no_schedules")

        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_CONFIRM, False):
                errors["base"] = "confirmation_required"
            else:
                try:
                    await coordinator.async_remove_schedule(
                        int(user_input[CONF_SLOT])
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("GUI schedule removal failed", exc_info=True)
                    errors["base"] = "operation_failed"
                    placeholders = self._operation_error(err)
                else:
                    return self.async_abort(reason="schedule_removed")

        return self.async_show_form(
            step_id="schedule_remove",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SLOT): vol.In(choices),
                    vol.Required(CONF_CONFIRM, default=False): cv.boolean,
                }
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reset_consumption(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_CONFIRM, False):
                errors["base"] = "confirmation_required"
            else:
                try:
                    await self._coordinator().async_reset_consumption()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("GUI consumption reset failed", exc_info=True)
                    errors["base"] = "operation_failed"
                    placeholders = self._operation_error(err)
                else:
                    return self.async_abort(reason="consumption_reset")

        return self.async_show_form(
            step_id="reset_consumption",
            data_schema=vol.Schema(
                {vol.Required(CONF_CONFIRM, default=False): cv.boolean}
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_factory_reset(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_CONFIRM, False):
                errors["base"] = "confirmation_required"
            else:
                try:
                    await self._coordinator().async_factory_reset()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("GUI factory reset failed", exc_info=True)
                    errors["base"] = "operation_failed"
                    placeholders = self._operation_error(err)
                else:
                    return self.async_abort(reason="factory_reset")

        return self.async_show_form(
            step_id="factory_reset",
            data_schema=vol.Schema(
                {vol.Required(CONF_CONFIRM, default=False): cv.boolean}
            ),
            errors=errors,
            description_placeholders=placeholders,
        )
