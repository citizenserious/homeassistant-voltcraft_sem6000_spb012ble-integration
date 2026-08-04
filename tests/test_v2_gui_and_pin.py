from __future__ import annotations

import ast
import importlib.util
import json
import sys
import textwrap
import unittest
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "voltcraft_sem6000_spb012ble"
EXTENDED_COORDINATOR = COMPONENT / "coordinator_extended.py"
CONFIG_FLOW = COMPONENT / "config_flow.py"
PROTOCOL = COMPONENT / "protocol.py"


def _class_method_source(path: Path, class_name: str, method_name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == method_name
                ):
                    segment = ast.get_source_segment(source, item)
                    assert segment is not None
                    return textwrap.dedent(segment)
    raise AssertionError(f"{class_name}.{method_name} was not found")


def _load_method(
    path: Path,
    class_name: str,
    method_name: str,
    globals_: dict[str, Any],
) -> Any:
    source = _class_method_source(path, class_name, method_name)
    namespace = dict(globals_)
    exec("from __future__ import annotations\n" + source, namespace)
    return namespace[method_name]


def _load_protocol() -> Any:
    spec = importlib.util.spec_from_file_location("beta8_protocol", PROTOCOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Logger:
    def debug(self, *args: Any, **kwargs: Any) -> None:
        return None


class _TimeoutHarness:
    _async_credential_command = _load_method(
        EXTENDED_COORDINATOR,
        "VoltcraftDataUpdateCoordinator",
        "_async_credential_command",
        {
            "_LOGGER": _Logger(),
            "BleakError": type("BleakError", (Exception,), {}),
            "UpdateFailed": type("UpdateFailed", (Exception,), {}),
            "LoginNotifyPayload": type("LoginNotifyPayload", (), {}),
            "_COMMAND_RESPONSE_TIMEOUT": 4.0,
        },
    )

    def __init__(self) -> None:
        self.verifications: list[dict[str, str]] = []

    async def _send_and_wait(
        self, frame: bytes, key: tuple[int, int], timeout: float
    ) -> None:
        raise TimeoutError

    async def _async_verify_and_store_pin(self, **kwargs: str) -> None:
        self.verifications.append(kwargs)


class FakeHomeAssistantError(Exception):
    """Stand-in for a user-facing Home Assistant error."""


class _PinVerificationHarness:
    _async_verify_and_store_pin = _load_method(
        EXTENDED_COORDINATOR,
        "VoltcraftDataUpdateCoordinator",
        "_async_verify_and_store_pin",
        {
            "HomeAssistantError": FakeHomeAssistantError,
            "_PIN_CHANGE_SETTLE_DELAY": 3.0,
        },
    )

    def __init__(self, outcomes: dict[str, list[bool]]) -> None:
        self.outcomes = {pin: list(values) for pin, values in outcomes.items()}
        self.verification_calls: list[tuple[str, float]] = []
        self.stored: list[str] = []
        self.teardown_calls = 0

    async def _async_verify_pin(
        self, pin: str, *, settle_delay: float = 0.0
    ) -> bool:
        self.verification_calls.append((pin, settle_delay))
        values = self.outcomes.get(pin, [])
        return values.pop(0) if values else False

    def _store_verified_pin(self, pin: str) -> None:
        self.stored.append(pin)

    async def _async_teardown(self) -> None:
        self.teardown_calls += 1


class PinVerificationTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_pin_is_stored_only_after_successful_login(self) -> None:
        harness = _PinVerificationHarness({"2468": [True]})
        await harness._async_verify_and_store_pin(
            new_pin="2468",
            previous_pin="0000",
            operation_name="PIN change",
        )
        self.assertEqual(harness.stored, ["2468"])
        self.assertEqual(harness.verification_calls, [("2468", 3.0)])

    async def test_previous_pin_is_not_replaced_when_change_did_not_take_effect(
        self,
    ) -> None:
        harness = _PinVerificationHarness({"2468": [False], "0000": [True]})
        with self.assertRaisesRegex(
            FakeHomeAssistantError, "previous PIN remains stored"
        ):
            await harness._async_verify_and_store_pin(
                new_pin="2468",
                previous_pin="0000",
                operation_name="PIN change",
            )
        self.assertEqual(harness.stored, [])
        self.assertEqual(
            harness.verification_calls, [("2468", 3.0), ("0000", 0.0)]
        )

    async def test_unverifiable_result_does_not_store_either_pin(self) -> None:
        harness = _PinVerificationHarness({"2468": [False], "0000": [False]})
        with self.assertRaisesRegex(
            FakeHomeAssistantError, "stored PIN was not changed"
        ):
            await harness._async_verify_and_store_pin(
                new_pin="2468",
                previous_pin="0000",
                operation_name="PIN change",
            )
        self.assertEqual(harness.stored, [])
        self.assertEqual(harness.teardown_calls, 1)


class PinTimeoutRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_still_runs_fresh_login_verification(self) -> None:
        harness = _TimeoutHarness()
        await harness._async_credential_command(
            b"frame",
            (0x17, 0x01),
            new_pin="2468",
            previous_pin="0000",
            operation_name="PIN change",
        )
        self.assertEqual(
            harness.verifications,
            [
                {
                    "new_pin": "2468",
                    "previous_pin": "0000",
                    "operation_name": "PIN change",
                }
            ],
        )


class _StartupUpdateFailed(Exception):
    """Stand-in for Home Assistant's UpdateFailed with chained causes."""


class StartupNotificationRecoveryTests(unittest.TestCase):
    _is_transient = staticmethod(
        _load_method(
            EXTENDED_COORDINATOR,
            "VoltcraftDataUpdateCoordinator",
            "_is_transient_notification_subscription_error",
            {},
        )
    )

    def test_unlikely_error_is_treated_as_transient(self) -> None:
        error = _StartupUpdateFailed(
            "Failed to connect during notification subscription: "
            "GATT Protocol Error: Unlikely Error"
        )
        self.assertTrue(self._is_transient(error))

    def test_empty_timeout_during_notification_subscription_is_transient(self) -> None:
        try:
            raise TimeoutError
        except TimeoutError as cause:
            try:
                raise _StartupUpdateFailed(
                    "Failed to connect during notification subscription: "
                ) from cause
            except _StartupUpdateFailed as error:
                self.assertTrue(self._is_transient(error))

    def test_timeout_in_other_connection_stage_is_not_suppressed(self) -> None:
        try:
            raise TimeoutError
        except TimeoutError as cause:
            try:
                raise _StartupUpdateFailed(
                    "Failed to connect during transport and service discovery: "
                ) from cause
            except _StartupUpdateFailed as error:
                self.assertFalse(self._is_transient(error))


class _HistoryKind(Enum):
    DAY = "day"
    MONTH = "month"
    YEAR = "year"


class _HistoryCommand(IntEnum):
    CONSUMPTION_DAY = 0x0A
    CONSUMPTION_MONTH = 0x0B
    CONSUMPTION_YEAR = 0x0C


class _HistoryCommands:
    @staticmethod
    def request_history(kind: _HistoryKind) -> bytes:
        return kind.value.encode()


class FakeUpdateFailed(Exception):
    """Stand-in for Home Assistant's UpdateFailed."""


class _HistoryHarness:
    async_refresh_history = _load_method(
        EXTENDED_COORDINATOR,
        "VoltcraftDataUpdateCoordinator",
        "async_refresh_history",
        {
            "HistoryKind": _HistoryKind,
            "Command": _HistoryCommand,
            "Commands": _HistoryCommands,
            "_HISTORY_RESPONSE_TIMEOUT": 15.0,
            "BleakError": type("BleakError", (Exception,), {}),
            "UpdateFailed": FakeUpdateFailed,
            "HomeAssistantError": type("HomeAssistantError", (Exception,), {}),
            "_LOGGER": _Logger(),
        },
    )

    def __init__(self, failing: set[_HistoryKind]) -> None:
        self.failing = set(failing)
        self.calls: list[_HistoryKind] = []

    async def _send_and_wait(
        self,
        frame: bytes,
        key: tuple[int, int],
        timeout: float,
    ) -> None:
        kind = _HistoryKind(frame.decode())
        self.calls.append(kind)
        if kind in self.failing:
            raise TimeoutError


class HistoryRefreshRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_timeout_does_not_skip_other_history_ranges(self) -> None:
        harness = _HistoryHarness({_HistoryKind.DAY})
        await harness.async_refresh_history()
        self.assertEqual(
            harness.calls,
            [_HistoryKind.DAY, _HistoryKind.MONTH, _HistoryKind.YEAR],
        )

    async def test_all_three_failures_are_reported(self) -> None:
        harness = _HistoryHarness(set(_HistoryKind))
        with self.assertRaisesRegex(
            FakeUpdateFailed, "No SEM6000 history range could be refreshed"
        ):
            await harness.async_refresh_history()
        self.assertEqual(harness.calls, list(_HistoryKind))


class HistoryChecksumRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.protocol = _load_protocol()

    def _history_frame(self, command: int) -> bytearray:
        data_length = 120 if command == self.protocol.Command.CONSUMPTION_MONTH else 48
        data = bytes(index & 0xFF for index in range(data_length))
        length = data_length + 3
        frame = bytearray(
            b"\x0f" + bytes([length, command, 0]) + data + b"\0\xff\xff"
        )
        checksum_index = length + 1
        expected = (1 + sum(frame[2:checksum_index])) & 0xFF
        frame[checksum_index] = (expected + 1) & 0xFF
        return frame

    def test_observed_history_checksum_is_tolerated_for_all_ranges(self) -> None:
        expected_values = {
            self.protocol.Command.CONSUMPTION_DAY: 24,
            self.protocol.Command.CONSUMPTION_MONTH: 31,
            self.protocol.Command.CONSUMPTION_YEAR: 13,
        }
        for command, value_count in expected_values.items():
            with self.subTest(command=command):
                frame = self._history_frame(command)
                self.assertEqual(
                    self.protocol.validate_notify_frame(frame), bytes(frame)
                )
                parsed = self.protocol.parse_notify_payload(frame)
                self.assertIsNotNone(parsed)
                self.assertEqual(len(parsed.values_wh), value_count)

    def test_wrong_checksum_remains_rejected_for_non_history_commands(self) -> None:
        frame = self._history_frame(self.protocol.Command.SET_NAME)
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.protocol.validate_notify_frame(frame)

    def test_history_frame_with_invalid_suffix_remains_rejected(self) -> None:
        frame = self._history_frame(self.protocol.Command.CONSUMPTION_YEAR)
        frame[-1] = 0
        with self.assertRaisesRegex(ValueError, "suffix"):
            self.protocol.validate_notify_frame(frame)



class SerialRemovalRegressionTests(unittest.TestCase):
    def test_serial_refresh_override_is_removed(self) -> None:
        source = EXTENDED_COORDINATOR.read_text(encoding="utf-8")
        self.assertNotIn("async def async_refresh_serial", source)
        self.assertNotIn("self.async_refresh_serial,", source)
        self.assertNotIn("Commands.request_serial()", source)

    def test_serial_sensor_is_removed_and_legacy_entry_is_cleaned_up(self) -> None:
        source = (COMPONENT / "sensor.py").read_text(encoding="utf-8")
        self.assertNotIn('key="serial"', source)
        self.assertIn('f"{coordinator.mac}_serial"', source)
        self.assertIn("entity_registry.async_remove", source)


class _MenuHarness:
    async_step_init = _load_method(
        CONFIG_FLOW,
        "VoltcraftOptionsFlow",
        "async_step_init",
        {},
    )

    def async_show_menu(self, **kwargs: Any) -> dict[str, Any]:
        return kwargs


class OptionsFlowRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_root_configure_menu_contains_exactly_four_areas(self) -> None:
        result = await _MenuHarness().async_step_init()
        self.assertEqual(result["step_id"], "init")
        self.assertEqual(
            result["menu_options"],
            ["access", "timer", "schedule", "maintenance"],
        )


class V2StructureTests(unittest.TestCase):
    def test_options_flow_factory_is_callback_decorated(self) -> None:
        source = CONFIG_FLOW.read_text(encoding="utf-8")
        tree = ast.parse(source)
        flow_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "VoltcraftConfigFlow"
        )
        method = next(
            node
            for node in flow_class.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "async_get_options_flow"
        )
        decorators = {
            decorator.id
            for decorator in method.decorator_list
            if isinstance(decorator, ast.Name)
        }
        self.assertEqual(decorators, {"staticmethod", "callback"})

    def test_change_pin_uses_verified_credential_transaction(self) -> None:
        source = _class_method_source(
            EXTENDED_COORDINATOR,
            "VoltcraftDataUpdateCoordinator",
            "async_change_pin",
        )
        self.assertIn("Commands.change_pin(previous_pin, new_pin)", source)
        self.assertIn("await self._async_credential_command(", source)
        self.assertNotIn("await self._user_command(", source)

    def test_pin_reset_and_factory_reset_use_same_verification(self) -> None:
        for method in ("async_reset_pin", "async_factory_reset"):
            with self.subTest(method=method):
                source = _class_method_source(
                    EXTENDED_COORDINATOR,
                    "VoltcraftDataUpdateCoordinator",
                    method,
                )
                self.assertIn("await self._async_credential_command(", source)
                self.assertIn("new_pin=DEFAULT_PIN", source)

    def test_custom_services_and_button_platform_are_removed(self) -> None:
        init_source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("hass.services.async_register", init_source)
        self.assertNotIn("async_register_admin_service", init_source)
        self.assertNotIn("Platform.BUTTON", init_source)
        self.assertFalse((COMPONENT / "services.yaml").exists())
        self.assertFalse((COMPONENT / "button.py").exists())

    def test_all_advanced_operations_have_gui_steps(self) -> None:
        source = CONFIG_FLOW.read_text(encoding="utf-8")
        required_steps = {
            "access",
            "login_pin",
            "change_pin",
            "reset_pin",
            "timer",
            "timer_delay",
            "timer_at",
            "timer_stop",
            "schedule",
            "schedule_add",
            "schedule_edit_select",
            "schedule_edit",
            "schedule_remove",
            "maintenance",
            "reset_consumption",
            "factory_reset",
        }
        for step in required_steps:
            self.assertIn(f"async def async_step_{step}(", source)
        self.assertIn("self.async_show_menu(", source)
        self.assertIn("TextSelectorType.PASSWORD", source)

    def test_translation_trees_are_consistent(self) -> None:
        strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
        english = json.loads(
            (COMPONENT / "translations" / "en.json").read_text(encoding="utf-8")
        )
        german = json.loads(
            (COMPONENT / "translations" / "de.json").read_text(encoding="utf-8")
        )
        self.assertEqual(strings, english)

        def key_tree(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: key_tree(item) for key, item in value.items()}
            return None

        self.assertEqual(key_tree(english), key_tree(german))

    def test_manifest_is_stable_2_0_0(self) -> None:
        manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "2.0.0")


if __name__ == "__main__":
    unittest.main()
