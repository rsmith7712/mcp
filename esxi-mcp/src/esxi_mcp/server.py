"""
==============================================================================
Script Name:    server.py
Synopsis:       MCP server entry point for the ESXi management plugin.
Description:    Implements the Model Context Protocol (MCP) stdio server that
                exposes ESXi host management capabilities to Claude Desktop
                and Claude Code.  The server registers 15 tools covering:
                  - Host inventory, capacity, licensing, network, and events
                  - VM inventory, detail, power management, and migration
                  - Datastore capacity and file browsing
                  - VM snapshot lifecycle (list, create, revert, delete)

                Transport: MCP stdio (stdin/stdout).  Claude launches this
                process and communicates via JSON-RPC over the standard
                streams.  All log output goes to stderr so it does not
                interfere with the stdio protocol.

                Architecture: the server layer is intentionally thin —
                it handles tool registration, JSON serialisation, and error
                formatting only.  All ESXi API logic lives in the tools/
                submodules.  This separation means the MCP wiring can be
                tested independently of live ESXi connectivity.

Parameters /
Setup:          Requires config/hosts.yaml and a .env file (or shell
                environment variables) with host credentials.
                See connection.py and INSTALL.md for full setup details.
                Entry point: `python -m esxi_mcp.server`
                or via the installed console script: `esxi-mcp`

Change Log:
  2026-06-23  Richard Smith   Initial version — 15 tools registered, stdio
                              transport, singleton ConnectionManager init at
                              startup, JSON error wrapping for all tool calls.
==============================================================================
"""

from __future__ import annotations

# ==============================================================================
# IMPORTS
# ==============================================================================
import asyncio
import json
import logging
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Connection manager — validates config at startup before serving any requests.
from .connection import ConnectionManager

# Tool implementations, grouped by domain.
from .tools.hosts import (
    esxi_list_hosts,
    esxi_host_summary,
    esxi_host_licensing,
    esxi_host_network,
    esxi_host_events,
)
from .tools.vms import (
    esxi_list_vms,
    esxi_vm_detail,
    esxi_vm_power,
    esxi_vm_migrate,
)
from .tools.datastores import (
    esxi_list_datastores,
    esxi_datastore_files,
)
from .tools.snapshots import (
    esxi_vm_snapshot_list,
    esxi_vm_snapshot_create,
    esxi_vm_snapshot_revert,
    esxi_vm_snapshot_delete,
)


# ==============================================================================
# VARIABLES — server configuration and logging setup
# ==============================================================================

# Server name shown to Claude in the MCP capabilities handshake.
# Changing this requires updating the name in claude_desktop_config.json.
SERVER_NAME: str = "esxi-mcp"

# Route all log output to stderr so it stays out of the stdio JSON-RPC channel.
# Claude Desktop surfaces MCP server stderr in its log viewer for debugging.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

# MCP Server instance.  Decorators on this object register handlers for
# list_tools and call_tool protocol messages.
app = Server(SERVER_NAME)


# ==============================================================================
# VARIABLES — tool registry (TOOLS list)
# ==============================================================================
# Each Tool entry defines what Claude sees: the tool name, a description that
# Claude uses to decide when to call the tool, and a JSON Schema that validates
# the arguments Claude provides.  The description is critical — it must be
# specific enough that Claude calls the right tool but concise enough to fit
# in the model's context budget.
#
# Required fields on each tool are kept to the true minimum.  Optional fields
# use "default" in the schema as documentation; actual defaults are applied
# in the _dispatch function rather than in schema validation.

TOOLS: list[Tool] = [

    # --- Host tools ---

    Tool(
        name="esxi_list_hosts",
        description=(
            "List all ESXi hosts configured in this MCP server and their "
            "current connection status. Use this first to confirm which hosts "
            "are available before calling other tools."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),

    Tool(
        name="esxi_host_summary",
        description=(
            "Return a full status snapshot for an ESXi host: hardware model, "
            "CPU (total MHz, used MHz, free MHz, % used), memory (total MB, "
            "used MB, free MB, % used), uptime, power state, maintenance mode, "
            "and ESXi product version and build number."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "host_name": {
                    "type": "string",
                    "description": (
                        "Named host from config (e.g. 'ESX-01'). "
                        "Omit to use the default host."
                    ),
                },
            },
            "required": [],
        },
    ),

    Tool(
        name="esxi_host_licensing",
        description=(
            "Return license details for an ESXi host: edition name, license key, "
            "feature list, total and used units, and expiration date. "
            "Use this to verify what VMware features are enabled and when "
            "licenses expire."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "host_name": {
                    "type": "string",
                    "description": "Named host. Omit for default.",
                },
            },
            "required": [],
        },
    ),

    Tool(
        name="esxi_host_network",
        description=(
            "Return the full network configuration for an ESXi host: "
            "physical NICs (device name, MAC, driver, link speed), "
            "standard vSwitches (port counts, MTU, uplink pNICs), "
            "port groups (name, VLAN ID, associated vSwitch), and "
            "VMkernel adapters (vmk0, etc.) with IP, subnet, DHCP status, and MTU. "
            "Note: Distributed vSwitches require vCenter and are not available "
            "on standalone ESXi."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "host_name": {
                    "type": "string",
                    "description": "Named host. Omit for default.",
                },
            },
            "required": [],
        },
    ),

    Tool(
        name="esxi_host_events",
        description=(
            "Return recent entries from the ESXi host event log, newest first. "
            "Use this to investigate recent configuration changes, errors, "
            "or VM lifecycle events on the host."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "host_name": {
                    "type": "string",
                    "description": "Named host. Omit for default.",
                },
                "max_events": {
                    "type": "integer",
                    "description": "Maximum number of events to return. Default: 50.",
                    "default": 50,
                },
            },
            "required": [],
        },
    ),

    # --- VM tools ---

    Tool(
        name="esxi_list_vms",
        description=(
            "List all virtual machines on an ESXi host with name, power state, "
            "guest OS, CPU count, memory (MB), IP address, hostname, and "
            "VMware Tools status. Use this as the starting point before calling "
            "esxi_vm_detail or esxi_vm_power on a specific VM."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "host_name": {
                    "type": "string",
                    "description": "Named host. Omit for default.",
                },
            },
            "required": [],
        },
    ),

    Tool(
        name="esxi_vm_detail",
        description=(
            "Return full detail for a single VM: disk layout (capacity, thin/thick "
            "provisioning, datastore path per disk), network adapters (type, MAC, "
            "port group, connected state), live CPU usage (MHz), memory active and "
            "ballooned (MB), uptime in seconds, and total snapshot count."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vm_name": {
                    "type": "string",
                    "description": (
                        "Exact VM name as shown in the ESXi inventory. "
                        "Names are case-sensitive."
                    ),
                },
                "host_name": {
                    "type": "string",
                    "description": "Named host. Omit for default.",
                },
            },
            "required": ["vm_name"],
        },
    ),

    Tool(
        name="esxi_vm_power",
        description=(
            "Get or change the power state of a VM.\n\n"
            "action options:\n"
            "  status    — read current power state (no change made)\n"
            "  power_on  — hard power on the VM\n"
            "  power_off — hard power off (no OS shutdown; like pulling the cord)\n"
            "  reset     — hard reset (no OS reboot sequence)\n"
            "  suspend   — suspend to memory (VM must be powered on)\n"
            "  shutdown  — graceful guest OS shutdown via VMware Tools\n"
            "  reboot    — graceful guest OS reboot via VMware Tools\n\n"
            "Prefer 'shutdown' or 'reboot' over 'power_off'/'reset' when the "
            "VM is running and VMware Tools is installed — graceful operations "
            "flush disk buffers and allow services to stop cleanly."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vm_name": {
                    "type": "string",
                    "description": "Exact VM name (case-sensitive).",
                },
                "action": {
                    "type": "string",
                    "enum": [
                        "status", "power_on", "power_off",
                        "reset", "suspend", "shutdown", "reboot",
                    ],
                    "description": "Power action to perform or 'status' to query only.",
                },
                "host_name": {
                    "type": "string",
                    "description": "Named host. Omit for default.",
                },
            },
            "required": ["vm_name", "action"],
        },
    ),

    Tool(
        name="esxi_vm_migrate",
        description=(
            "Cold-migrate a VM's storage to a different datastore on the same "
            "ESXi host. The VM MUST be powered off before migration.\n\n"
            "IMPORTANT: Live vMotion between ESXi hosts requires vCenter Server, "
            "which is not configured. This tool only supports intra-host "
            "datastore relocation. Use esxi_list_datastores to find the target "
            "datastore name before calling this tool."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vm_name": {
                    "type": "string",
                    "description": "Exact VM name (case-sensitive). VM must be powered off.",
                },
                "target_datastore": {
                    "type": "string",
                    "description": (
                        "Name of the destination datastore "
                        "(e.g. 'datastore2'). Use esxi_list_datastores to confirm."
                    ),
                },
                "host_name": {
                    "type": "string",
                    "description": "Named host. Omit for default.",
                },
            },
            "required": ["vm_name", "target_datastore"],
        },
    ),

    # --- Datastore tools ---

    Tool(
        name="esxi_list_datastores",
        description=(
            "List all datastores visible to an ESXi host with capacity (GB), "
            "free space (GB), used space (GB), usage percentage, datastore type "
            "(VMFS, NFS), accessibility state, and VM count. "
            "Results are sorted by usage (highest first) to surface capacity "
            "pressure immediately."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "host_name": {
                    "type": "string",
                    "description": "Named host. Omit for default.",
                },
            },
            "required": [],
        },
    ),

    Tool(
        name="esxi_datastore_files",
        description=(
            "Browse files in a datastore directory. Returns file names, sizes "
            "(MB), modification timestamps, and file types (e.g. VmDiskFileInfo, "
            "VmConfigFileInfo, LogFileInfo). Results sorted by size (largest first). "
            "Use this to investigate datastore space usage, locate VM disk files, "
            "or find orphaned files."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "datastore_name": {
                    "type": "string",
                    "description": "Datastore name (e.g. 'datastore1'). Use esxi_list_datastores to confirm.",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Subdirectory path within the datastore "
                        "(e.g. 'SymUtility' to browse a VM folder). "
                        "Omit or pass empty string for the datastore root."
                    ),
                    "default": "",
                },
                "host_name": {
                    "type": "string",
                    "description": "Named host. Omit for default.",
                },
            },
            "required": ["datastore_name"],
        },
    ),

    # --- Snapshot tools ---

    Tool(
        name="esxi_vm_snapshot_list",
        description=(
            "List all snapshots for a VM in flattened tree order. "
            "Each entry includes the snapshot name, description, creation time, "
            "VM state at snapshot time, depth in the tree, and child count. "
            "Returns an empty list if the VM has no snapshots."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vm_name": {
                    "type": "string",
                    "description": "Exact VM name (case-sensitive).",
                },
                "host_name": {
                    "type": "string",
                    "description": "Named host. Omit for default.",
                },
            },
            "required": ["vm_name"],
        },
    ),

    Tool(
        name="esxi_vm_snapshot_create",
        description=(
            "Create a snapshot of a VM.\n\n"
            "memory=true  — include guest memory state (VM stays running; "
            "snapshot is larger and slower to create)\n"
            "memory=false — disk-only snapshot (faster; VM must be quiesced or "
            "powered off for crash-consistency)\n\n"
            "quiesce=true — freeze the guest filesystem for application consistency "
            "(requires VMware Tools; recommended for VMs running databases or file servers)\n"
            "quiesce=false — crash-consistent only (no Tools dependency)\n\n"
            "Use unique snapshot names to avoid ambiguity in revert and delete operations."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vm_name": {
                    "type": "string",
                    "description": "Exact VM name (case-sensitive).",
                },
                "snapshot_name": {
                    "type": "string",
                    "description": "Name for the new snapshot. Should be unique within the VM.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional description of what state this snapshot captures.",
                    "default": "",
                },
                "memory": {
                    "type": "boolean",
                    "description": "Include guest memory state in the snapshot. Default: false.",
                    "default": False,
                },
                "quiesce": {
                    "type": "boolean",
                    "description": "Quiesce guest filesystem (requires VMware Tools). Default: false.",
                    "default": False,
                },
                "host_name": {
                    "type": "string",
                    "description": "Named host. Omit for default.",
                },
            },
            "required": ["vm_name", "snapshot_name"],
        },
    ),

    Tool(
        name="esxi_vm_snapshot_revert",
        description=(
            "Revert a VM to a previously taken snapshot.\n\n"
            "WARNING: All changes made to the VM after the snapshot was taken "
            "will be permanently discarded — disk writes, config changes, "
            "and files created after the snapshot point will be lost. "
            "This cannot be undone. Use esxi_vm_snapshot_list first to confirm "
            "the correct snapshot name."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vm_name": {
                    "type": "string",
                    "description": "Exact VM name (case-sensitive).",
                },
                "snapshot_name": {
                    "type": "string",
                    "description": "Exact name of the snapshot to revert to.",
                },
                "host_name": {
                    "type": "string",
                    "description": "Named host. Omit for default.",
                },
            },
            "required": ["vm_name", "snapshot_name"],
        },
    ),

    Tool(
        name="esxi_vm_snapshot_delete",
        description=(
            "Delete a named VM snapshot. The snapshot's disk delta is merged "
            "back into its parent layer — current VM disk state is NOT changed.\n\n"
            "remove_children=false (default) — delete only this snapshot; "
            "child snapshots are re-parented and kept.\n"
            "remove_children=true  — delete this snapshot AND all snapshots "
            "descended from it in the tree. Use with care."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vm_name": {
                    "type": "string",
                    "description": "Exact VM name (case-sensitive).",
                },
                "snapshot_name": {
                    "type": "string",
                    "description": "Exact name of the snapshot to delete.",
                },
                "remove_children": {
                    "type": "boolean",
                    "description": "Also delete all child snapshots. Default: false.",
                    "default": False,
                },
                "host_name": {
                    "type": "string",
                    "description": "Named host. Omit for default.",
                },
            },
            "required": ["vm_name", "snapshot_name"],
        },
    ),
]


# ==============================================================================
# FUNCTIONS — MCP protocol handlers
# ==============================================================================

@app.list_tools()
async def list_tools() -> list[Tool]:
    """
    MCP list_tools handler.  Called by Claude during the capabilities
    handshake to discover what tools this server provides.  Returns the
    full TOOLS list defined above.
    """
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """
    MCP call_tool handler.  Routes incoming tool calls to the appropriate
    implementation via _dispatch, serialises the result to JSON, and wraps
    any exception into a structured error response rather than crashing.

    Returning a TextContent with an error dict keeps Claude informed about
    what went wrong rather than leaving the tool call hanging.  Claude can
    then surface the error message to the user or retry with corrected args.

    default=str in json.dumps handles datetime objects and pyVmomi enums
    that aren't JSON-serialisable natively.
    """
    try:
        result = _dispatch(name, arguments)
    except Exception as exc:
        # Structured error response so Claude can report the failure clearly.
        result = {
            "error": type(exc).__name__,
            "message": str(exc),
        }

    return [
        TextContent(
            type="text",
            text=json.dumps(result, indent=2, default=str),
        )
    ]


def _dispatch(name: str, args: dict[str, Any]) -> Any:
    """
    Route a tool call to its implementation function.
    Using a dispatch dict rather than if/elif keeps the routing table
    visible at a glance and avoids a long elif chain that obscures structure.
    Each lambda captures the args dict in its closure and passes only the
    parameters that the target function expects.
    """
    dispatch: dict[str, Any] = {
        # Host tools
        "esxi_list_hosts": lambda: esxi_list_hosts(),
        "esxi_host_summary": lambda: esxi_host_summary(
            args.get("host_name")
        ),
        "esxi_host_licensing": lambda: esxi_host_licensing(
            args.get("host_name")
        ),
        "esxi_host_network": lambda: esxi_host_network(
            args.get("host_name")
        ),
        "esxi_host_events": lambda: esxi_host_events(
            args.get("host_name"),
            args.get("max_events", 50),
        ),
        # VM tools
        "esxi_list_vms": lambda: esxi_list_vms(
            args.get("host_name")
        ),
        "esxi_vm_detail": lambda: esxi_vm_detail(
            args["vm_name"],
            args.get("host_name"),
        ),
        "esxi_vm_power": lambda: esxi_vm_power(
            args["vm_name"],
            args["action"],
            args.get("host_name"),
        ),
        "esxi_vm_migrate": lambda: esxi_vm_migrate(
            args["vm_name"],
            args["target_datastore"],
            args.get("host_name"),
        ),
        # Datastore tools
        "esxi_list_datastores": lambda: esxi_list_datastores(
            args.get("host_name")
        ),
        "esxi_datastore_files": lambda: esxi_datastore_files(
            args["datastore_name"],
            args.get("path", ""),
            args.get("host_name"),
        ),
        # Snapshot tools
        "esxi_vm_snapshot_list": lambda: esxi_vm_snapshot_list(
            args["vm_name"],
            args.get("host_name"),
        ),
        "esxi_vm_snapshot_create": lambda: esxi_vm_snapshot_create(
            args["vm_name"],
            args["snapshot_name"],
            args.get("description", ""),
            args.get("memory", False),
            args.get("quiesce", False),
            args.get("host_name"),
        ),
        "esxi_vm_snapshot_revert": lambda: esxi_vm_snapshot_revert(
            args["vm_name"],
            args["snapshot_name"],
            args.get("host_name"),
        ),
        "esxi_vm_snapshot_delete": lambda: esxi_vm_snapshot_delete(
            args["vm_name"],
            args["snapshot_name"],
            args.get("remove_children", False),
            args.get("host_name"),
        ),
    }

    if name not in dispatch:
        raise ValueError(
            f"Unknown tool: '{name}'. "
            f"Available tools: {', '.join(sorted(dispatch.keys()))}"
        )

    return dispatch[name]()


# ==============================================================================
# MAIN — server startup and async event loop
# ==============================================================================

async def _run_server() -> None:
    """
    Start the MCP stdio server.  stdio_server() is an async context manager
    that opens stdin/stdout as MCP streams.  app.run() processes the MCP
    protocol loop until the client disconnects or the process is killed.

    The initialisation options carry the server name and version to the
    client during the MCP handshake.
    """
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


def main() -> None:
    """
    Entry point for `esxi-mcp` console script and `python -m esxi_mcp.server`.

    Performs two startup checks before entering the async event loop:
      1. Load and validate hosts.yaml — fail fast with a clear error if the
         config is missing or malformed rather than accepting connections and
         failing on the first tool call.
      2. Log the list of configured hosts — confirms the server is reading
         the correct config file and gives the operator a quick sanity check.

    sys.exit(1) on config failure prevents Claude from connecting to a server
    that can't do anything useful, which would result in confusing tool errors.
    """
    # --- Startup: validate config before accepting connections ---
    try:
        mgr = ConnectionManager.instance()
        host_names = mgr.host_names()
    except Exception as exc:
        logger.error(
            "Failed to load ESXi host configuration: %s\n"
            "Check config/hosts.yaml and your .env file.",
            exc,
        )
        sys.exit(1)

    logger.info(
        "ESXi MCP Server '%s' starting — %d host(s) configured: %s",
        SERVER_NAME,
        len(host_names),
        ", ".join(host_names),
    )

    # --- Run the async MCP event loop ---
    # asyncio.run() creates a new event loop, runs the coroutine to completion,
    # and closes the loop on exit.  This is the correct pattern for a
    # single-process stdio MCP server.
    asyncio.run(_run_server())


if __name__ == "__main__":
    main()
