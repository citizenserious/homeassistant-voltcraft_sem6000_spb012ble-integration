"""Transactional credential, history, and BLE startup compatibility fixes."""

from __future__ import annotations

import logging

from bleak.exc import BleakError
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import DEFAULT_PIN
from .coordinator import (
    _COMMAND_RESPONSE_TIMEOUT,
    _HISTORY_RESPONSE_TIMEOUT,
    _PIN_CHANGE_SETTLE_DELAY,
    VoltcraftData,
    VoltcraftDataUpdateCoordinator as BaseVoltcraftDataUpdateCoordinator,
)
from .protocol import (
    Command,
    Commands,
    HistoryKind,
    LoginNotifyPayload,
    ParsedNotifyPayload,
    PinOperation,
    normalize_pin,
)

_LOGGER = logging.getLogger(__name__)


class VoltcraftDataUpdateCoordinator(BaseVoltcraftDataUpdateCoordinator):
    """Apply credential changes transactionally and verify the resulting PIN.

    Some SEM6000 firmware changes the PIN but does not deliver the expected
    acknowledgement before the command timeout. The resulting device state is
    therefore authoritative: reconnect with the candidate PIN before reporting
    success or failure.
    """

    @staticmethod
    def _is_transient_notification_subscription_error(err: Exception) -> bool:
        """Return whether startup notification setup hit the observed proxy race.

        Depending on the Bluetooth backend, the same transient startup failure is
        reported either as GATT ``UNLIKELY_ERROR`` or as a bare ``TimeoutError``
        with an empty message.  Only failures explicitly raised during the
        notification-subscription stage are eligible for the one-time deferral.
        """
        detail = str(err)
        if "notification subscription" not in detail:
            return False
        if "Unlikely Error" in detail or "UNLIKELY_ERROR" in detail:
            return True
        return isinstance(err.__cause__, TimeoutError)

    async def _async_update_data(self) -> VoltcraftData | None:
        """Treat one startup notification error as a transient proxy condition.

        ESPHome Bluetooth proxies can briefly reject notification subscription
        while their scanner or another BLE connection is still settling after a
        Home Assistant restart.  Load the entities with their existing state and
        schedule a near-term retry instead of emitting a red first-refresh error.
        Repeated failures are still reported normally.
        """
        try:
            data = await super()._async_update_data()
        except UpdateFailed as err:
            transient_error = self._is_transient_notification_subscription_error(err)
            already_suppressed = getattr(
                self, "_startup_notify_error_suppressed", False
            )
            if not transient_error or already_suppressed:
                raise

            self._startup_notify_error_suppressed = True
            self._next_connect_at = 0.0
            self._schedule_reconnect(5.0)
            _LOGGER.debug(
                "Deferring Voltcraft reconnect after a transient startup "
                "notification-subscription error"
            )
            return self._latest_data

        self._startup_notify_error_suppressed = False
        return data

    async def async_refresh_history(self) -> None:
        """Refresh every history range even if another range times out.

        The plug answers each range independently. A timeout for one range must
        not prevent Home Assistant from receiving the other two ranges.
        """
        failures: list[tuple[HistoryKind, Exception]] = []
        command_by_kind = {
            HistoryKind.DAY: Command.CONSUMPTION_DAY,
            HistoryKind.MONTH: Command.CONSUMPTION_MONTH,
            HistoryKind.YEAR: Command.CONSUMPTION_YEAR,
        }

        for kind in (HistoryKind.DAY, HistoryKind.MONTH, HistoryKind.YEAR):
            try:
                await self._send_and_wait(
                    Commands.request_history(kind),
                    (command_by_kind[kind], 0),
                    _HISTORY_RESPONSE_TIMEOUT,
                )
            except (TimeoutError, BleakError, UpdateFailed, HomeAssistantError) as err:
                failures.append((kind, err))
                _LOGGER.debug(
                    "SEM6000 %s history refresh failed: %s",
                    kind.value,
                    str(err).strip() or type(err).__name__,
                )

        if len(failures) == 3:
            details = ", ".join(
                f"{kind.value}: {str(err).strip() or type(err).__name__}"
                for kind, err in failures
            )
            raise UpdateFailed(
                f"No SEM6000 history range could be refreshed ({details})"
            )

    async def async_refresh_all(self, *, include_initialization: bool = True) -> None:
        """Refresh optional state after the authenticated session is ready."""
        client = await self._async_ensure_connected()
        if include_initialization:
            command_char = self._command_characteristic(client)
            initialized, finalized = await self._async_core_initialization(
                client, command_char
            )
            self._update_data(
                app_initialization_complete=initialized,
                app_finalize_succeeded=finalized,
            )

        operations = (
            self.async_refresh_device_identity,
            self.async_refresh_history,
            self.async_refresh_timer,
            self.async_refresh_schedules,
        )
        for operation in operations:
            try:
                await operation()
            except (TimeoutError, BleakError, UpdateFailed, HomeAssistantError) as err:
                _LOGGER.debug(
                    "Optional state refresh %s failed: %s",
                    operation.__name__,
                    str(err).strip() or type(err).__name__,
                )

    async def _async_verify_and_store_pin(
        self,
        *,
        new_pin: str,
        previous_pin: str,
        operation_name: str,
    ) -> None:
        """Persist a PIN only after a fresh authenticated connection succeeds."""
        if await self._async_verify_pin(
            new_pin, settle_delay=_PIN_CHANGE_SETTLE_DELAY
        ):
            self._store_verified_pin(new_pin)
            return

        if await self._async_verify_pin(previous_pin):
            raise HomeAssistantError(
                f"{operation_name} did not take effect. The previous PIN remains "
                "stored and continues to work."
            )

        await self._async_teardown()
        raise HomeAssistantError(
            f"{operation_name} could not be verified with either the new or the "
            "previous PIN. The stored PIN was not changed. Enter the working PIN "
            "under Device access in the integration configuration."
        )

    async def _async_credential_command(
        self,
        frame: bytes | bytearray,
        key: tuple[int, int],
        *,
        new_pin: str,
        previous_pin: str,
        operation_name: str,
        expected_operation: PinOperation | None = None,
        timeout: float = _COMMAND_RESPONSE_TIMEOUT,
    ) -> None:
        """Send a PIN-changing command and verify the actual resulting state."""
        response: ParsedNotifyPayload | None = None
        try:
            response = await self._send_and_wait(frame, key, timeout)
        except (TimeoutError, BleakError, UpdateFailed) as err:
            # A missing acknowledgement is not proof of failure. The tested
            # SEM6000 applies the new PIN before the response wait times out.
            _LOGGER.debug(
                "%s response unavailable (%s); verifying the resulting PIN",
                operation_name,
                type(err).__name__,
            )
        else:
            if expected_operation is not None and (
                not isinstance(response, LoginNotifyPayload)
                or response.operation != expected_operation
                or not response.was_successful
            ):
                _LOGGER.debug(
                    "%s returned no positive matching acknowledgement; verifying "
                    "the resulting PIN",
                    operation_name,
                )

        await self._async_verify_and_store_pin(
            new_pin=new_pin,
            previous_pin=previous_pin,
            operation_name=operation_name,
        )

    async def async_change_pin(self, new_pin: str) -> None:
        """Change the device PIN and store it only after a successful fresh login."""
        new_pin = normalize_pin(new_pin)
        async with self._credential_lock:
            previous_pin = self._pin
            if new_pin == previous_pin:
                return
            await self._async_credential_command(
                Commands.change_pin(previous_pin, new_pin),
                (Command.LOGIN, PinOperation.CHANGE),
                new_pin=new_pin,
                previous_pin=previous_pin,
                operation_name="PIN change",
                expected_operation=PinOperation.CHANGE,
            )

    async def async_reset_pin(self) -> None:
        """Reset the device PIN to the factory default and verify it."""
        async with self._credential_lock:
            previous_pin = self._pin
            await self._async_credential_command(
                Commands.reset_pin(),
                (Command.LOGIN, PinOperation.RESET),
                new_pin=DEFAULT_PIN,
                previous_pin=previous_pin,
                operation_name="PIN reset",
                expected_operation=PinOperation.RESET,
            )

    async def async_factory_reset(self) -> None:
        """Factory-reset the plug and verify that the default PIN works."""
        async with self._credential_lock:
            previous_pin = self._pin
            await self._async_credential_command(
                Commands.factory_reset(),
                (Command.SETTINGS_CONTROL, 0),
                new_pin=DEFAULT_PIN,
                previous_pin=previous_pin,
                operation_name="Factory reset",
                timeout=5.0,
            )
