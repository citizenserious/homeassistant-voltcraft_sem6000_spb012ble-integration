from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "voltcraft_sem6000_spb012ble"


class PackageConsistencyTests(unittest.TestCase):
    def test_const_defines_all_required_runtime_names(self) -> None:
        tree = ast.parse((COMPONENT / "const.py").read_text(encoding="utf-8"))
        defined = {
            target.id
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            if isinstance(target, ast.Name)
        }
        required = {
            "COMMAND_UUID",
            "CONF_PIN",
            "DEFAULT_PIN",
            "DEVICE_INFO_UUID",
            "DEVICE_NAME",
            "DEVICE_NAME_UUID",
            "DOMAIN",
            "NOTIFY_UUID",
            "SCAN_INTERVAL",
            "SERVICE_UUID",
        }
        self.assertEqual(required - defined, set())

    def test_manifest_is_stable_2_0_0_and_uses_core_bluetooth_packages(self) -> None:
        manifest = json.loads(
            (COMPONENT / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["version"], "2.0.0")
        self.assertEqual(manifest["codeowners"], ["@citizenserious"])
        self.assertNotIn("requirements", manifest)
        self.assertIn("bluetooth", manifest["dependencies"])

    def test_config_flow_masks_all_pin_fields(self) -> None:
        source = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")
        self.assertIn("TextSelectorType.PASSWORD", source)
        self.assertIn("vol.Required(CONF_NEW_PIN): _PIN_SELECTOR", source)
        self.assertIn("vol.Required(CONF_REPEAT_PIN): _PIN_SELECTOR", source)
        self.assertNotIn("default=current_pin", source)

    def test_integration_is_config_entry_only(self) -> None:
        source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
        self.assertIn(
            "CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)",
            source,
        )

    def test_v2_runtime_uses_transactional_coordinator(self) -> None:
        source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
        self.assertIn(
            "from .coordinator_extended import VoltcraftDataUpdateCoordinator",
            source,
        )

    def test_no_voltcraft_specific_services_or_button_platform(self) -> None:
        source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("services.async_register", source)
        self.assertNotIn("async_register_admin_service", source)
        self.assertNotIn("Platform.BUTTON", source)
        self.assertFalse((COMPONENT / "services.yaml").exists())
        self.assertFalse((COMPONENT / "button.py").exists())

    def test_history_protocol_fix_is_included(self) -> None:
        source = (COMPONENT / "protocol.py").read_text(encoding="utf-8")
        self.assertIn("is_observed_history_frame", source)
        self.assertIn("int(Command.CONSUMPTION_DAY): (0x33, 55, 52)", source)
        self.assertIn("int(Command.CONSUMPTION_MONTH): (0x7B, 127, 124)", source)
        self.assertIn("int(Command.CONSUMPTION_YEAR): (0x33, 55, 52)", source)


    def test_datetime_forms_use_separate_date_and_time_selectors(self) -> None:
        source = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")
        self.assertIn("DateSelector", source)
        self.assertIn("TimeSelector", source)
        self.assertIn("_DATE_SELECTOR = DateSelector()", source)
        self.assertIn("_TIME_SELECTOR = TimeSelector()", source)
        self.assertIn("datetime.combine(cv.date(date_value), cv.time(time_value))", source)
        self.assertNotIn("DateTimeSelector", source)

    def test_serial_number_is_not_exposed_or_polled(self) -> None:
        coordinator_source = (COMPONENT / "coordinator_extended.py").read_text(
            encoding="utf-8"
        )
        sensor_source = (COMPONENT / "sensor.py").read_text(encoding="utf-8")
        self.assertNotIn("async_refresh_serial", coordinator_source)
        self.assertNotIn("Commands.request_serial()", coordinator_source)
        self.assertNotIn('key="serial"', sensor_source)
        self.assertIn("entity_registry.async_remove", sensor_source)

    def test_transient_notification_subscription_error_is_deferred_once(self) -> None:
        source = (COMPONENT / "coordinator_extended.py").read_text(encoding="utf-8")
        self.assertIn("_is_transient_notification_subscription_error", source)
        self.assertIn("_startup_notify_error_suppressed", source)
        self.assertIn("self._schedule_reconnect(5.0)", source)
        self.assertIn("return self._latest_data", source)

    def test_github_actions_are_sha_pinned_and_read_only(self) -> None:
        action_ref = re.compile(r"uses:\s+[^@\s]+@([0-9a-f]{40})(?:\s|$)")
        workflow_dir = ROOT / ".github" / "workflows"
        workflows = sorted(
            [*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")]
        )
        self.assertTrue(workflows)
        for workflow in workflows:
            source = workflow.read_text(encoding="utf-8")
            with self.subTest(workflow=workflow.name):
                uses_lines = [
                    line.strip() for line in source.splitlines() if "uses:" in line
                ]
                self.assertTrue(uses_lines)
                self.assertTrue(all(action_ref.search(line) for line in uses_lines))
                self.assertIn("permissions:\n  contents: read", source)


if __name__ == "__main__":
    unittest.main()
