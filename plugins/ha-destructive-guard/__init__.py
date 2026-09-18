"""ha-destructive-guard plugin — human approval gate for destructive Home
Assistant tool calls.

Added 2026-09-04. Hermes exposes broad Home Assistant config-management
tools via the ``homeassistant_full`` MCP server (ha-mcp v8.4.1, 84 tools,
verified live against http://192.168.0.245:9584/... on 2026-09-04) plus a
built-in ``tools/homeassistant_tool.py`` toolset. Neither had a confirmation
gate for destructive actions. This plugin returns
``{"action": "approve", ...}`` from a ``pre_tool_call`` hook, which routes
through the SAME human [o]nce/[s]ession/[a]lways/[d]eny approval gate
(``tools/approval.py::request_tool_approval``) the terminal tool's
dangerous-command guard uses — fails closed on gate error/timeout/
non-interactive context (``hermes_cli/plugins.py::_resolve_block_from_details``).

Scope, deliberately narrow (destructive-only, not a blanket HA wall):

- Always gated: ``ha_write_file``, ``ha_delete_file``, ``ha_restart`` on the
  ``homeassistant_full`` MCP server, PLUS the bare ``ha_call_service`` name
  from the built-in ``tools/homeassistant_tool.py`` (see below — no
  write/delete/restart tool exists there, only call_service).
- ``ha_call_service`` (MCP-prefixed AND the built-in bare name): gated ONLY
  for a fixed allowlist of destructive (domain, service) pairs — host
  restart/shutdown, add-on lifecycle, recorder data purge. Everything else
  (state reads, light.turn_on, climate.set_temperature, automation.trigger,
  pyscript.reload, todo.add_item, ...) passes through completely ungated.

Why ``ha_restart`` needs its own entry: this particular MCP server
(ha-mcp v8.4.1) ships restart as a DEDICATED tool, not routed through
``ha_call_service`` at all — gating only call_service domain/service pairs
would leave the exact action this plugin exists to gate completely open.

Why the built-in bare ``ha_call_service`` is included: ``tools/
homeassistant_tool.py::_BLOCKED_DOMAINS`` blocks ``hassio``,
``shell_command``, etc. but NOT the ``homeassistant`` domain, so
``ha_call_service(domain="homeassistant", service="restart")`` through the
built-in tool is not inherently blocked at that layer. As of 2026-09-04 the
built-in ``homeassistant`` toolset is verified DISABLED for both the root/
default and assistant profiles (``hermes tools list`` shows
``✗ disabled  homeassistant``), so this path is not currently reachable —
gated anyway, defensively, in case that toolset is ever re-enabled.

KNOWN GAP (documented, not silently guessed at — see the 2026-09-04 task
report for the full list): the homeassistant_full MCP server exposes ~15
additional dedicated remove/delete tools across other domains
(``ha_remove_entity``, ``ha_remove_device``, ``ha_config_remove_automation``,
``ha_config_delete_dashboard``, ``ha_remove_zone``, ``ha_config_remove_scene``,
``ha_config_remove_script``, etc.), plus ``ha_config_set_yaml`` (raw YAML
config edit — the tool's own description calls it "Destructive, disabled by
default") and destructive ``ha_manage_backup`` actions (snapshot restore/
delete). These were out of the explicit scope handed to this plugin's
author and are deliberately NOT gated here. Extend ``_ALWAYS_APPROVE`` (and
consider a per-tool-name allowlist rather than blanket coverage) if broader
protection is wanted later.

approvals.mode: verified 'manual' (the unset default) for both the root/
default and assistant profiles as of 2026-09-04 — see
``tools/approval.py::_get_approval_mode()`` / ``_normalize_approval_mode()``.
Neither profile's config.yaml sets an ``approvals: mode:`` key, and neither
runs inside a "hosted room" execution-policy context (that override path —
``gateway/hosted_room_execution_policy.py`` — is only bound by
``gateway/platforms/api_server_runs.py`` for a separate hosted-rooms
feature). A plugin ``approve`` directive here therefore reaches a REAL
human [o/s/a/d] prompt, not an LLM auto-approval (``approvals.mode: smart``
would ask an auxiliary LLM first via ``_smart_approve()``; that mode is not
configured for either profile). Re-verify this if either profile's config
ever changes.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Tool names that ALWAYS require human approval, regardless of args.
_ALWAYS_APPROVE = {
    # homeassistant_full MCP server (ha-mcp v8.4.1). Verified live against
    # the running server on 2026-09-04 (`hermes mcp test homeassistant_full`).
    "mcp__homeassistant_full__ha_write_file",
    "mcp__homeassistant_full__ha_delete_file",
    "mcp__homeassistant_full__ha_restart",
}

# ha_call_service tool names to gate by (domain, service) pair — both the
# homeassistant_full MCP tool and the built-in tools/homeassistant_tool.py
# tool (registered under the bare name "ha_call_service", no mcp__ prefix).
_CALL_SERVICE_TOOLS = {
    "mcp__homeassistant_full__ha_call_service",
    "ha_call_service",
}

# (domain, service) pairs considered destructive: host-level, data-loss, or
# availability-impacting. Deliberately NOT exhaustive of every HA service —
# normal entity control (light.turn_on, climate.set_temperature,
# automation.trigger, todo.add_item, ...) and non-destructive reloads
# (pyscript.reload, *.reload) are intentionally left ungated so this stays
# destructive-only rather than a blanket approval wall.
_DESTRUCTIVE_SERVICE_PAIRS = {
    ("homeassistant", "restart"),
    ("homeassistant", "stop"),
    ("hassio", "restart"),
    ("hassio", "reboot"),
    ("hassio", "addon_restart"),
    ("hassio", "addon_stop"),
    ("hassio", "addon_uninstall"),
    ("hassio", "host_reboot"),
    ("hassio", "host_shutdown"),
    ("recorder", "purge"),
    ("recorder", "purge_entities"),
}


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _on_pre_tool_call(
    tool_name: str = "",
    args: Any = None,
    **_: Any,
) -> Optional[Dict[str, str]]:
    """Escalate destructive Home Assistant tool calls to human approval.

    Returns ``{"action": "approve", ...}`` for gated calls (resolved by the
    caller via ``tools.approval.request_tool_approval`` — see module
    docstring). Returns ``None`` for everything else so routine HA tool use
    is completely unaffected.
    """
    if not isinstance(args, dict):
        args = {}

    if tool_name in _ALWAYS_APPROVE:
        logger.info("ha-destructive-guard: escalating %s for human approval", tool_name)
        return {
            "action": "approve",
            "message": (
                f"Destructive Home Assistant tool call: {tool_name}. "
                "This writes/deletes a file in the Home Assistant config "
                "directory, or restarts Home Assistant. Approve only if "
                "you intended this."
            ),
            "rule_key": f"ha-destructive-guard:{tool_name}",
        }

    if tool_name in _CALL_SERVICE_TOOLS:
        domain = _norm(args.get("domain"))
        service = _norm(args.get("service"))
        if (domain, service) in _DESTRUCTIVE_SERVICE_PAIRS:
            logger.info(
                "ha-destructive-guard: escalating %s (%s.%s) for human approval",
                tool_name, domain, service,
            )
            return {
                "action": "approve",
                "message": (
                    f"Destructive Home Assistant service call: {domain}.{service} "
                    f"(via {tool_name}). This is host-level, data-loss, or "
                    "availability-impacting. Approve only if you intended this."
                ),
                "rule_key": f"ha-destructive-guard:{tool_name}:{domain}.{service}",
            }

    return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
