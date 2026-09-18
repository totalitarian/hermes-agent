"""Hermes <-> iPhone Shortcuts bridge for Apple Notes/Reminders (Mac-free).

Registers ``apple_bridge``. Calls ``script.hermes_apple_bridge`` in Home
Assistant with ``return_response=true`` and returns its structured result.
A normal request needs one iPhone callback; Notes append needs a second,
independent read callback before Home Assistant reports success.

The tool is enabled through ``platform_toolsets`` in config.yaml. See
skills/smart-home/homeassistant-apple-bridge/SKILL.md for the authoritative
operation and verification contract.
"""

import json
import logging
import re
from typing import Any, Dict, Optional
from urllib.parse import quote

from agent.secret_scope import get_secret
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_HA_CALLBACK_TIMEOUT_SECONDS = 120
_HTTP_TIMEOUT_SECONDS = 135
_FUTURE_TIMEOUT_SECONDS = 140


class AppleBridgeTimeout(TimeoutError):
    def __init__(self, seconds: float, stage: str = ""):
        self.seconds = seconds
        self.stage = stage
        super().__init__(f"{stage or 'request'} timeout after {seconds} seconds")

_WEBUI_HOME_LINK = "webui.fixterslab.uk"
_WEBUI_BASE_URL = "https://webui.fixterslab.uk"

_VALID_APPS = {"notes", "reminders"}
_VALID_ACTIONS = {"read", "append", "complete", "create"}
_VALID_ACTIONS_BY_APP = {
    "notes": {"read", "append"},
    "reminders": {"read", "complete", "create"},
}


def _get_config():
    return (
        (get_secret("HASS_URL", "http://homeassistant.local:8123") or "").rstrip("/"),
        get_secret("HASS_TOKEN", "") or "")


def _check_ha_available() -> bool:
    return bool(get_secret("HASS_TOKEN"))


async def _async_apple_bridge(app: str, action: str, title: str, payload: Optional[str]) -> Dict[str, Any]:
    import aiohttp
    hass_url, hass_token = _get_config()
    headers = {"Authorization": f"Bearer {hass_token}", "Content-Type": "application/json"}
    body: Dict[str, Any] = {"app": app, "action": action, "title": title}
    if payload:
        body["payload"] = payload
    url = f"{hass_url}/api/services/script/hermes_apple_bridge?return_response=true"
    # Keep the HTTP client outside Home Assistant's callback window. Notes
    # append can use two callback windows because HA verifies with a read.
    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=body, timeout=timeout) as resp:
                resp.raise_for_status()
                data = await resp.json()
    except TimeoutError as exc:
        raise AppleBridgeTimeout(_HTTP_TIMEOUT_SECONDS, "HTTP") from exc
    result = data.get("service_response") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        return {"success": False, "error": "No service_response from script.hermes_apple_bridge", "raw": data}
    return result


def _run_async(coro):
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(asyncio.run, coro)
        timed_out = False
        try:
            return future.result(timeout=_FUTURE_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError as exc:
            timed_out = True
            future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
            raise AppleBridgeTimeout(_FUTURE_TIMEOUT_SECONDS) from exc
        finally:
            if not timed_out:
                pool.shutdown(wait=True)
    return asyncio.run(coro)


def _expand_cron_session_link(app: str, action: str, title: str, session_id: Any) -> str:
    """Turn the cron reminder's home-link line into a per-run WebUI deep link."""
    if (
        app != "reminders"
        or action != "create"
        or not isinstance(session_id, str)
        or not session_id.startswith("cron_")
    ):
        return title
    session_url = f"{_WEBUI_BASE_URL}/session/{quote(session_id, safe='')}"
    return re.sub(rf"(?m)^{re.escape(_WEBUI_HOME_LINK)}$", session_url, title)


def _handle_apple_bridge(args: dict, **kw) -> str:
    app = (args.get("app") or "").strip().lower()
    action = (args.get("action") or "").strip().lower()
    title = args.get("title") or ""
    payload = args.get("payload")
    if app not in _VALID_APPS:
        return tool_error(f"Invalid app {app!r}; must be one of {sorted(_VALID_APPS)}")
    if action not in _VALID_ACTIONS:
        return tool_error(f"Invalid action {action!r}; must be one of {sorted(_VALID_ACTIONS)}")
    if action not in _VALID_ACTIONS_BY_APP[app]:
        return tool_error(
            f"Invalid action {action!r} for app {app!r}; "
            f"must be one of {sorted(_VALID_ACTIONS_BY_APP[app])}"
        )
    title = _expand_cron_session_link(app, action, title, kw.get("session_id"))
    if not title:
        return tool_error("Missing required parameter: title")
    if app == "notes" and action == "append" and not (payload or "").strip():
        return tool_error("Missing required parameter: payload for notes append")
    try:
        hass_url, hass_token = _get_config()
    except Exception as e:
        logger.error("apple_bridge credential lookup failed: %s", e)
        return tool_error("Home Assistant credentials are unavailable in this execution context")
    if not hass_url or not hass_token:
        return tool_error("Home Assistant credentials are unavailable in this execution context")
    try:
        result = _run_async(_async_apple_bridge(app, action, title, payload))
        return json.dumps({"result": result})
    except AppleBridgeTimeout as exc:
        stage = f"{exc.stage} " if exc.stage else ""
        logger.error(
            "apple_bridge %stimed out after %s seconds (%s/%s)",
            stage,
            exc.seconds,
            app,
            action,
        )
        return tool_error(
            f"Apple Bridge {stage}request timed out after {exc.seconds} seconds "
            f"({app}/{action})"
        )
    except TimeoutError:
        logger.error(
            "apple_bridge timed out after %s seconds (%s/%s)",
            _FUTURE_TIMEOUT_SECONDS,
            app,
            action,
        )
        return tool_error(
            f"Apple Bridge request timed out after {_FUTURE_TIMEOUT_SECONDS} seconds "
            f"({app}/{action})"
        )
    except Exception as e:
        logger.error("apple_bridge error: %s", e)
        return tool_error(f"Failed to call Apple bridge ({app}/{action}): {e}")


APPLE_BRIDGE_SCHEMA = {
    "name": "apple_bridge",
    "description": (
        "Read or append Apple Notes, or read/complete/create Apple Reminders, on Lee's iPhone via the "
        "'Hermes Apple Bridge' Home Assistant script + Shortcut (Mac-free). "
        "Sends a silent notification the phone-side Shortcut acts on, then "
        "waits for its callback; Notes append also performs a separate read "
        "before success is returned. Requires the 'Hermes Apple "
        "Bridge' Shortcut to be installed and its personal automation "
        "enabled on the phone. No due dates for reminders (accepted "
        "platform limit)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "app": {
                "type": "string",
                "enum": ["notes", "reminders"],
                "description": "Target Apple app.",
            },
            "action": {
                "type": "string",
                "enum": ["read", "append", "complete", "create"],
                "description": "Notes: read or append. Reminders: read, complete, or create.",
            },
            "title": {
                "type": "string",
                "description": (
                    "Notes: exact note title. Reminders: existing reminder list/title contract."
                ),
            },
            "payload": {
                "type": "string",
                "description": (
                    "Notes append: Markdown content. Reminders complete: title is the list name and payload is the exact reminder title. "
                    "Reminders create: leave empty. Omit for read."
                ),
            },
        },
        "required": ["app", "action", "title"],
    },
}

registry.register(
    name=APPLE_BRIDGE_SCHEMA["name"], toolset="apple_bridge", schema=APPLE_BRIDGE_SCHEMA,
    handler=_handle_apple_bridge, check_fn=_check_ha_available, emoji="📱")
