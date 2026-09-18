import asyncio
import json
import time
import unittest
from unittest.mock import Mock, patch

from tools import apple_bridge_tool
from tools.registry import registry


class AppleBridgeCredentialTests(unittest.TestCase):
    def test_handle_fails_locally_when_home_assistant_credentials_are_missing(self):
        with (
            patch.object(
                apple_bridge_tool,
                "_get_config",
                return_value=("http://homeassistant.local:8123", ""),
            ),
            patch.object(apple_bridge_tool, "_run_async") as run_async,
        ):
            result = json.loads(
                apple_bridge_tool._handle_apple_bridge(
                    {"app": "reminders", "action": "read", "title": "Tasks"}
                )
            )

        run_async.assert_not_called()
        self.assertIn("Home Assistant credentials are unavailable", result["error"])


class AppleBridgeTimeoutTests(unittest.TestCase):
    def test_client_timeouts_exceed_home_assistant_callback_window(self):
        self.assertEqual(apple_bridge_tool._HA_CALLBACK_TIMEOUT_SECONDS, 120)
        self.assertEqual(apple_bridge_tool._HTTP_TIMEOUT_SECONDS, 135)
        self.assertEqual(apple_bridge_tool._FUTURE_TIMEOUT_SECONDS, 140)
        self.assertGreater(
            apple_bridge_tool._HTTP_TIMEOUT_SECONDS,
            apple_bridge_tool._HA_CALLBACK_TIMEOUT_SECONDS,
        )
        self.assertGreater(
            apple_bridge_tool._FUTURE_TIMEOUT_SECONDS,
            apple_bridge_tool._HTTP_TIMEOUT_SECONDS,
        )

    def test_timeout_error_is_reported_with_elapsed_limit(self):
        with (
            patch.object(
                apple_bridge_tool,
                "_get_config",
                return_value=("http://homeassistant.local:8123", "test-token"),
            ),
            patch.object(
                apple_bridge_tool,
                "_run_async",
                side_effect=TimeoutError(),
            ),
            patch.object(
                apple_bridge_tool,
                "_async_apple_bridge",
                new=Mock(return_value=None),
            ),
        ):
            result = json.loads(
                apple_bridge_tool._handle_apple_bridge(
                    {"app": "reminders", "action": "read", "title": "all"}
                )
            )

        self.assertEqual(
            result["error"],
            "Apple Bridge request timed out after 140 seconds (reminders/read)",
        )

    def test_http_timeout_reports_http_limit(self):
        with (
            patch.object(
                apple_bridge_tool,
                "_get_config",
                return_value=("http://homeassistant.local:8123", "test-token"),
            ),
            patch.object(
                apple_bridge_tool,
                "_run_async",
                side_effect=apple_bridge_tool.AppleBridgeTimeout(135, "HTTP"),
            ),
            patch.object(
                apple_bridge_tool,
                "_async_apple_bridge",
                new=Mock(return_value=None),
            ),
        ):
            result = json.loads(
                apple_bridge_tool._handle_apple_bridge(
                    {"app": "reminders", "action": "read", "title": "all"}
                )
            )

        self.assertEqual(
            result["error"],
            "Apple Bridge HTTP request timed out after 135 seconds (reminders/read)",
        )

    def test_outer_timeout_returns_without_waiting_for_worker_completion(self):
        async def slow_operation():
            await asyncio.sleep(0.2)

        async def invoke_from_running_loop():
            started = time.monotonic()
            with patch.object(apple_bridge_tool, "_FUTURE_TIMEOUT_SECONDS", 0.02):
                with self.assertRaises(apple_bridge_tool.AppleBridgeTimeout):
                    apple_bridge_tool._run_async(slow_operation())
            return time.monotonic() - started

        elapsed = asyncio.run(invoke_from_running_loop())
        self.assertLess(elapsed, 0.1)


class AppleBridgeCronSessionLinkTests(unittest.TestCase):
    def test_cron_reminder_home_link_becomes_exact_session_url(self):
        session_id = "cron_f4bb3d6d859e_20260915_190053"
        async_bridge_result = {"success": True}

        with (
            patch.object(
                apple_bridge_tool,
                "_get_config",
                return_value=("http://homeassistant.local:8123", "test-token"),
            ),
            patch.object(
                apple_bridge_tool,
                "_async_apple_bridge",
                new=Mock(return_value=async_bridge_result),
            ) as async_bridge,
            patch.object(
                apple_bridge_tool,
                "_run_async",
                return_value={"success": True},
            ),
        ):
            registry.dispatch(
                "apple_bridge",
                {
                    "app": "reminders",
                    "action": "create",
                    "title": "Evening report ready\nwebui.fixterslab.uk",
                    "payload": "",
                },
                session_id=session_id,
            )

        async_bridge.assert_called_once_with(
            "reminders",
            "create",
            "Evening report ready\n"
            "https://webui.fixterslab.uk/session/"
            "cron_f4bb3d6d859e_20260915_190053",
            "",
        )


if __name__ == "__main__":
    unittest.main()
