import unittest

from cdc_ai_guard import (
    AI_DISPLAY_TOGGLES,
    AUTO_ENFORCE_ON_CONNECT,
    CapabilityLevel,
    PRESERVED_FIRMWARE_PROPERTIES,
    SkipReason,
    ToggleState,
    build_off_property_map,
    evaluate_getprop_mapping,
    find_stale_on_toggles,
    find_stale_on_toggles_from_xml,
    parse_shared_preferences_booleans,
    plan_guard_on_connection,
    sync_ai_display_preferences_off,
)


class AIDisplayGuardTests(unittest.TestCase):
    @staticmethod
    def _all_false_preferences_xml() -> str:
        entries = "\n".join(
            f'    <boolean name="{spec.preference_key}" value="false" />'
            for spec in AI_DISPLAY_TOGGLES
        )
        return f"<?xml version='1.0' encoding='utf-8'?>\n<map>\n{entries}\n</map>\n"

    def test_all_off_is_fully_verified(self) -> None:
        raw_properties = build_off_property_map()

        result = evaluate_getprop_mapping(raw_properties)

        self.assertTrue(result.verified_off)
        self.assertEqual(result.drift_count, 0)
        self.assertEqual(len(result.toggles), 7)
        self.assertTrue(
            all(item.state is ToggleState.OFF for item in result.toggles)
        )
        for preserved in PRESERVED_FIRMWARE_PROPERTIES:
            self.assertNotIn(preserved, raw_properties)

    def test_observed_properties_missing_from_v1_are_detected_as_drift(self) -> None:
        # V1 verified its original 11 keys while these three real firmware
        # controls remained enabled.  V2 must report all three mismatches.
        raw_properties = build_off_property_map()
        raw_properties.update(
            {
                "persist.vendor.sculptor.mode": "1",
                "persist.vendor.sculptor.c2.mode": "1",
                "persist.vendor.rkpq.memc.watermark": "1",
            }
        )

        result = evaluate_getprop_mapping(raw_properties)

        self.assertFalse(result.verified_off)
        self.assertEqual(result.drift_count, 3)
        self.assertEqual(
            set(result.drifted_properties),
            {
                "persist.vendor.sculptor.mode",
                "persist.vendor.sculptor.c2.mode",
                "persist.vendor.rkpq.memc.watermark",
            },
        )
        self.assertIs(
            result.toggle_for_preference("ai_visionpq").state,
            ToggleState.ON,
        )
        self.assertIs(
            result.toggle_for_preference("ai_demonstration_switch").state,
            ToggleState.ON,
        )

    def test_firmware_specific_minus_one_and_one_on_values(self) -> None:
        raw_properties = build_off_property_map()
        for spec in AI_DISPLAY_TOGGLES:
            raw_properties[spec.property_key] = spec.on_values[0]

        result = evaluate_getprop_mapping(raw_properties)

        self.assertFalse(result.verified_off)
        self.assertEqual(result.drift_count, 7)
        self.assertTrue(
            all(item.state is ToggleState.ON for item in result.toggles)
        )
        self.assertEqual(
            result.toggle_for_preference("ai_dc").raw_value,
            "-1",
        )
        self.assertEqual(
            result.toggle_for_preference("ai_sr").raw_value,
            "-1",
        )
        self.assertEqual(
            result.toggle_for_preference("ai_memc").raw_value,
            "-1",
        )
        self.assertEqual(
            result.toggle_for_preference("ai_visionpq").raw_value,
            "1",
        )
        self.assertEqual(
            result.toggle_for_preference("ai_pq").raw_value,
            "1",
        )
        self.assertEqual(
            result.toggle_for_preference("ai_face").raw_value,
            "1",
        )
        self.assertEqual(
            result.toggle_for_preference("ai_demonstration_switch").raw_value,
            "1",
        )

    def test_noncanonical_nonzero_value_is_unknown_but_still_drift(self) -> None:
        raw_properties = build_off_property_map()
        raw_properties["persist.vendor.rkpq.dc.enable"] = "1"

        result = evaluate_getprop_mapping(raw_properties)
        ai_dc = result.toggle_for_preference("ai_dc")

        self.assertIs(ai_dc.state, ToggleState.UNKNOWN)
        self.assertTrue(ai_dc.drift)
        self.assertFalse(result.verified_off)

    def test_shared_preferences_parser_returns_seven_keys_in_ui_order(self) -> None:
        xml_data = b"""<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
        <map>
            <boolean name="ai_face" value="false" />
            <string name="unrelated_text">keep</string>
            <boolean name="ai_memc" value="true" />
            <boolean name="unrelated_switch" value="true" />
            <boolean name="ai_dc" value="false" />
            <boolean name="ai_demonstration_switch" value="TRUE" />
            <boolean name="ai_sr" value="false" />
            <boolean name="ai_pq" value="true" />
            <boolean name="ai_visionpq" value="false" />
        </map>
        """

        parsed = parse_shared_preferences_booleans(xml_data)

        self.assertEqual(
            list(parsed),
            [spec.preference_key for spec in AI_DISPLAY_TOGGLES],
        )
        self.assertEqual(
            parsed,
            {
                "ai_visionpq": False,
                "ai_dc": False,
                "ai_pq": True,
                "ai_sr": False,
                "ai_memc": True,
                "ai_face": False,
                "ai_demonstration_switch": True,
            },
        )

    def test_stale_on_report_compares_cache_to_verified_runtime_off(self) -> None:
        preference_booleans = {
            "ai_visionpq": True,
            "ai_dc": True,
            "ai_pq": False,
            "ai_sr": True,
            "ai_memc": True,
            "ai_face": False,
            "ai_demonstration_switch": True,
        }
        raw_properties = build_off_property_map()
        # AI-DC is genuinely on, so it is runtime drift, not a stale cache.
        raw_properties["persist.vendor.rkpq.dc.enable"] = "-1"
        # Missing runtime state is unverifiable and must not be called stale.
        del raw_properties["persist.vendor.rkpq.memc.enable"]

        stale = find_stale_on_toggles(
            preference_booleans,
            raw_properties,
        )

        self.assertEqual(
            [item.name for item in stale],
            ["AI VisionPQ", "AI-SR", "AI Test Mode"],
        )
        self.assertTrue(all(item.preference_value for item in stale))
        self.assertTrue(all(item.runtime_value == "0" for item in stale))

    def test_xml_convenience_reports_all_cached_on_toggles_in_ui_order(self) -> None:
        xml_data = "<map>" + "".join(
            f'<boolean name="{spec.preference_key}" value="true" />'
            for spec in reversed(AI_DISPLAY_TOGGLES)
        ) + "</map>"

        stale = find_stale_on_toggles_from_xml(
            xml_data,
            build_off_property_map(),
        )

        self.assertEqual(
            [item.preference_key for item in stale],
            [spec.preference_key for spec in AI_DISPLAY_TOGGLES],
        )

    def test_shared_preferences_parser_rejects_ambiguous_known_boolean(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid SharedPreferences boolean"):
            parse_shared_preferences_booleans(
                '<map><boolean name="ai_dc" value="1" /></map>'
            )

    def test_full_rk_profile_auto_enforces_all_seven_for_every_connection(self) -> None:
        self.assertTrue(AUTO_ENFORCE_ON_CONNECT)
        raw_properties = build_off_property_map()
        raw_properties.update(
            {
                spec.property_key: spec.on_values[0]
                for spec in AI_DISPLAY_TOGGLES
            }
        )
        # Identifiers are opaque: USB, hostname, and non-default network
        # transports receive the same capability-driven policy.
        for device_id in (
            "RK3576-USB-002",
            "192.168.40.21:5555",
            "display-lab-a.local:17000",
        ):
            with self.subTest(device_id=device_id):
                plan = plan_guard_on_connection(device_id, raw_properties)
                self.assertEqual(plan.device_id, device_id)
                self.assertIs(plan.capability, CapabilityLevel.FULL)
                self.assertTrue(plan.compatible)
                self.assertTrue(plan.automatic)
                self.assertTrue(plan.should_apply)
                self.assertTrue(plan.enforces_all_toggles)
                self.assertEqual(len(plan.supported_toggle_specs), 7)
                self.assertEqual(len(plan.application_map), 14)
                self.assertEqual(len(plan.correction_map), 7)

    def test_missing_vendor_properties_are_skipped_not_scheduled_or_fixed(self) -> None:
        raw_properties = {
            "persist.vendor.sculptor.mode": "1",
            "persist.vendor.rkpq.dc.enable": "",
            "persist.vendor.rkpq.hwpq_aisd_enable": None,
            "vendor.hwc.hwpq_force_enable": "1",
        }

        plan = plan_guard_on_connection("device-with-partial-rk-pq", raw_properties)

        self.assertIs(plan.capability, CapabilityLevel.PARTIAL)
        self.assertTrue(plan.compatible)
        self.assertFalse(plan.enforces_all_toggles)
        self.assertEqual(
            plan.application_map,
            {
                "persist.vendor.sculptor.mode": "0",
                "vendor.hwc.hwpq_force_enable": "0",
            },
        )
        self.assertEqual(plan.correction_map, plan.application_map)
        self.assertEqual(
            [spec.name for spec in plan.skipped_toggle_specs],
            [
                "AI-DC",
                "AI-SD",
                "AI-SR",
                "AI-MEMC",
                "AI-FACE",
                "AI Test Mode",
            ],
        )
        skipped = {item.property_key: item for item in plan.skipped_properties}
        self.assertIs(
            skipped["persist.vendor.rkpq.dc.enable"].reason,
            SkipReason.UNSUPPORTED_OR_MISSING,
        )
        self.assertNotIn("persist.vendor.rkpq.dc.enable", plan.application_map)
        self.assertNotIn(
            "persist.vendor.rkpq.hwpq_aisd_enable",
            plan.application_map,
        )

    def test_device_without_known_toggle_capability_is_unsupported(self) -> None:
        plan = plan_guard_on_connection(
            "generic-android-device",
            {
                "ro.board.platform": "generic",
                # An isolated secondary gate does not prove that the audited
                # seven-switch firmware interface exists.
                "vendor.hwc.hwpq_force_enable": "1",
            },
        )

        self.assertIs(plan.capability, CapabilityLevel.UNSUPPORTED)
        self.assertFalse(plan.compatible)
        self.assertFalse(plan.should_apply)
        self.assertEqual(plan.application_map, {})
        self.assertEqual(plan.correction_map, {})
        self.assertEqual(len(plan.skipped_properties), 14)
        force_gate = next(
            item
            for item in plan.skipped_properties
            if item.property_key == "vendor.hwc.hwpq_force_enable"
        )
        self.assertIs(force_gate.reason, SkipReason.INCOMPATIBLE_FIRMWARE)

    def test_connection_plan_never_touches_preserved_face_capability_gate(self) -> None:
        raw_properties = build_off_property_map()
        preserved = "persist.vendor.sculptor.enable.fe"
        raw_properties[preserved] = "1"

        plan = plan_guard_on_connection("rk3576-preserve-face-gate", raw_properties)

        self.assertNotIn(preserved, plan.application_map)
        self.assertNotIn(preserved, plan.correction_map)
        self.assertNotIn(
            preserved,
            [item.property_key for item in plan.skipped_properties],
        )

    def test_already_off_full_profile_is_still_applied_on_new_connection(self) -> None:
        plan = plan_guard_on_connection(
            "newly-connected-rk3576",
            build_off_property_map(),
        )

        self.assertIs(plan.capability, CapabilityLevel.FULL)
        self.assertEqual(plan.correction_map, {})
        self.assertEqual(plan.application_map, build_off_property_map())
        self.assertTrue(plan.enforces_all_toggles)

    def test_cache_sync_changes_only_known_true_boolean_value_spans(self) -> None:
        source = """<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <!-- unrelated formatting and entries must remain byte-for-byte intact -->
    <string name="theme">dark &amp; blue</string>
    <boolean name="unrelated_switch" value="true" />
    <boolean name="ai_visionpq" value="true" custom="preserve" />
    <boolean value='true' name='ai_dc' />
    <boolean name="ai_pq" value="false" />
    <boolean name="ai_sr" value="TRUE" />
    <boolean name="ai_memc" value="false" />
    <boolean name="ai_face" value="true" />
    <boolean name="ai_demonstration_switch" value="false" />
    <int name="unrelated_number" value="7" />
</map>
"""
        expected = (
            source
            .replace(
                'name="ai_visionpq" value="true"',
                'name="ai_visionpq" value="false"',
            )
            .replace("value='true' name='ai_dc'", "value='false' name='ai_dc'")
            .replace(
                'name="ai_sr" value="TRUE"',
                'name="ai_sr" value="false"',
            )
            .replace(
                'name="ai_face" value="true"',
                'name="ai_face" value="false"',
            )
        )

        result = sync_ai_display_preferences_off(source)

        self.assertTrue(result.changed)
        self.assertEqual(
            result.changed_keys,
            ("ai_visionpq", "ai_dc", "ai_sr", "ai_face"),
        )
        self.assertEqual(result.xml_data, expected)
        self.assertIn(
            '<boolean name="unrelated_switch" value="true" />',
            result.xml_data,
        )
        self.assertTrue(
            all(
                value is False
                for value in parse_shared_preferences_booleans(
                    result.xml_data
                ).values()
            )
        )

    def test_cache_sync_returns_original_bytes_when_already_off(self) -> None:
        source = self._all_false_preferences_xml().encode("utf-8")

        result = sync_ai_display_preferences_off(source)

        self.assertFalse(result.changed)
        self.assertEqual(result.changed_keys, ())
        self.assertIs(result.xml_data, source)

    def test_cache_sync_rejects_missing_duplicate_and_malformed_known_entries(self) -> None:
        valid = self._all_false_preferences_xml()
        missing = valid.replace(
            '    <boolean name="ai_dc" value="false" />\n',
            "",
        )
        duplicate = valid.replace(
            "</map>",
            '    <boolean name="ai_dc" value="true" />\n</map>',
        )
        wrong_type = valid.replace(
            '<boolean name="ai_dc" value="false" />',
            '<string name="ai_dc">false</string>',
        )
        invalid_value = valid.replace(
            '<boolean name="ai_dc" value="false" />',
            '<boolean name="ai_dc" value="1" />',
        )
        malformed = valid.replace("</map>", "")

        cases = (
            ("missing", missing, "missing SharedPreferences booleans"),
            ("duplicate", duplicate, "duplicate SharedPreferences preference"),
            ("wrong type", wrong_type, "must be a direct boolean"),
            ("invalid value", invalid_value, "invalid SharedPreferences boolean"),
            ("malformed", malformed, "invalid Android SharedPreferences XML"),
        )
        for name, xml_data, message in cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, message):
                    sync_ai_display_preferences_off(xml_data)

    def test_cache_sync_changed_bytes_remain_bytes(self) -> None:
        source = self._all_false_preferences_xml().replace(
            'name="ai_memc" value="false"',
            'name="ai_memc" value="true"',
        ).encode("utf-8")

        result = sync_ai_display_preferences_off(source)

        self.assertTrue(result.changed)
        self.assertEqual(result.changed_keys, ("ai_memc",))
        self.assertIsInstance(result.xml_data, bytes)
        self.assertIn(b'name="ai_memc" value="false"', result.xml_data)


if __name__ == "__main__":
    unittest.main()
