"""
==============================================================================
Script Name:    tools/hosts.py
Synopsis:       ESXi host-level query tools for the MCP server.
Description:    Implements five MCP tool functions that query a standalone
                ESXi 6.7 host directly via the pyVmomi SOAP API:
                  esxi_list_hosts    — list all configured hosts + status
                  esxi_host_summary  — hardware, CPU/memory capacity, uptime
                  esxi_host_licensing — license edition, features, expiration
                  esxi_host_network  — NICs, vSwitches, port groups, vmk adapters
                  esxi_host_events   — recent host event log

                All functions return plain Python dicts that the MCP server
                serialises to JSON for Claude.  No pyVmomi types are returned
                to callers; conversion happens entirely inside these functions
                so the server layer stays clean of pyVmomi imports.

Parameters /
Setup:          No parameters at module level.  Each function accepts an
                optional host_name argument that is passed to
                ConnectionManager.get_connection(); omitting it uses the
                default host from hosts.yaml.

Change Log:
  2026-06-23  Richard Smith   Initial version — list_hosts, host_summary,
                              host_licensing, host_network, host_events.
==============================================================================
"""

from __future__ import annotations

# ==============================================================================
# IMPORTS
# ==============================================================================
import datetime
from typing import Any

from pyVmomi import vim

from ..connection import ConnectionManager


# ==============================================================================
# VARIABLES — unit conversion constants and display defaults
# ==============================================================================

# Bytes → megabytes divisor.  Used for memory values, which pyVmomi expresses
# in bytes at the hardware level but reports in MB in quickStats.
BYTES_PER_MB: int = 1024 * 1024

# Bytes → gigabytes divisor.  Used for hardware memory sizing.
BYTES_PER_GB: int = 1024 * 1024 * 1024

# Default number of events to return when max_events is not specified.
# 50 is enough for recent activity without overwhelming Claude's context window.
DEFAULT_MAX_EVENTS: int = 50


# ==============================================================================
# FUNCTIONS — unit conversion helpers
# ==============================================================================

def _mb(bytes_val: int) -> float:
    """
    Convert bytes to megabytes, rounded to one decimal place.
    Used for memory values throughout; rounding avoids floating-point noise
    in JSON output (e.g., 65535.999... → 65536.0).
    """
    return round(bytes_val / BYTES_PER_MB, 1)


def _gb(bytes_val: int) -> float:
    """
    Convert bytes to gigabytes, rounded to two decimal places.
    Two decimal places matters for datastore/memory sizing where 0.5 GB
    increments are meaningful.
    """
    return round(bytes_val / BYTES_PER_GB, 2)


def _pct(used: int, total: int) -> float:
    """
    Calculate percentage of used/total, returning 0.0 if total is zero
    to avoid ZeroDivisionError on a host that hasn't fully initialised.
    """
    return round(used / total * 100, 1) if total else 0.0


def _format_uptime(boot_time: datetime.datetime | None) -> str:
    """
    Derive a human-readable uptime string from a boot timestamp.
    Returns empty string if boot_time is None (host not yet fully started).
    The UTC offset is explicitly attached before subtraction because pyVmomi
    returns timezone-aware datetimes and naive datetimes cannot be subtracted
    from aware ones in Python 3.
    """
    if not boot_time:
        return ""
    delta = datetime.datetime.now(tz=datetime.timezone.utc) - boot_time.replace(
        tzinfo=datetime.timezone.utc
    )
    total_seconds = int(delta.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    return f"{days}d {hours}h {minutes}m"


# ==============================================================================
# FUNCTIONS — tool implementations
# ==============================================================================

# ------------------------------------------------------------------------------
# esxi_list_hosts
# Return all configured hosts and whether they currently have an active session.
# ------------------------------------------------------------------------------
def esxi_list_hosts() -> dict[str, Any]:
    """
    List every ESXi host defined in hosts.yaml along with its IP address and
    current connection status (connected / not connected).

    This is intentionally a local-only operation — it reads from the
    ConnectionManager's in-memory state rather than making any network calls,
    so it succeeds even if all hosts are offline.
    """
    mgr = ConnectionManager.instance()
    host_list = []

    for name in mgr.host_names():
        conn = mgr._connections[name]
        # A non-None si means we have an open session; we don't liveness-probe
        # here because the point is to report what we know without side effects.
        status = "connected" if conn.si is not None else "not connected"
        host_list.append({
            "name": name,
            "host": conn.config.host,
            "status": status,
        })

    return {
        "hosts": host_list,
        "default": mgr.default_host(),
    }


# ------------------------------------------------------------------------------
# esxi_host_summary
# Full hardware, CPU, memory, uptime, and product version for one ESXi host.
# ------------------------------------------------------------------------------
def esxi_host_summary(host_name: str | None = None) -> dict[str, Any]:
    """
    Return a comprehensive status snapshot for an ESXi host.

    Combines three pyVmomi sources into one response:
      summary.hardware   — static hardware specs (CPU model, core count, RAM)
      summary.quickStats — live utilisation counters (CPU MHz used, RAM MB used)
      summary.runtime    — operational state (power, maintenance, boot time)

    CPU capacity is derived by multiplying cpuMhz (per-core clock speed) by
    numCpuCores, giving total available MHz.  This matches what ESXi itself
    shows in the vSphere web client performance graphs.
    """
    conn = ConnectionManager.instance().get_connection(host_name)
    content = conn.get_content()

    # For a standalone ESXi host (no vCenter), the object hierarchy is:
    # rootFolder → datacenter (index 0) → host (index 0).
    # With vCenter this would be a cluster; this code targets standalone only.
    host = content.rootFolder.childEntity[0].host[0]
    summary = host.summary
    hw = summary.hardware
    runtime = summary.runtime
    config_summary = summary.config

    # --- CPU capacity calculations ---
    # Total MHz = per-core clock × core count.  Used MHz comes from quickStats
    # which is a rolling average updated approximately every 20 seconds.
    total_cpu_mhz = hw.cpuMhz * hw.numCpuCores
    used_cpu_mhz = summary.quickStats.overallCpuUsage or 0

    # --- Memory capacity calculations ---
    # hardware.memorySize is in bytes; quickStats.overallMemoryUsage is in MB.
    # We normalise hardware.memorySize to MB for consistent comparison.
    total_mem_mb = _mb(hw.memorySize)
    used_mem_mb = summary.quickStats.overallMemoryUsage or 0

    return {
        "host": conn.config.name,
        "address": conn.config.host,
        "product": {
            "version": config_summary.product.version,
            "build": config_summary.product.build,
            "full_name": config_summary.product.fullName,
        },
        "hardware": {
            "vendor": hw.vendor,
            "model": hw.model,
            "uuid": hw.uuid,
            "cpu_model": hw.cpuModel,
            "cpu_sockets": hw.numCpuPkgs,
            "cpu_cores": hw.numCpuCores,
            "cpu_threads": hw.numCpuThreads,
            "cpu_mhz_per_core": hw.cpuMhz,
            "memory_gb": _gb(hw.memorySize),
            "nics": hw.numNics,
            "hbas": hw.numHBAs,
        },
        "capacity": {
            "cpu_total_mhz": total_cpu_mhz,
            "cpu_used_mhz": used_cpu_mhz,
            "cpu_free_mhz": total_cpu_mhz - used_cpu_mhz,
            "cpu_used_pct": _pct(used_cpu_mhz, total_cpu_mhz),
            "memory_total_mb": total_mem_mb,
            "memory_used_mb": used_mem_mb,
            "memory_free_mb": round(total_mem_mb - used_mem_mb, 1),
            "memory_used_pct": _pct(used_mem_mb, total_mem_mb),
        },
        "runtime": {
            "power_state": runtime.powerState,
            "connection_state": runtime.connectionState,
            "maintenance_mode": runtime.inMaintenanceMode,
            "boot_time": runtime.bootTime.isoformat() if runtime.bootTime else None,
            "uptime": _format_uptime(runtime.bootTime),
        },
        "overall_status": summary.overallStatus,
    }


# ------------------------------------------------------------------------------
# esxi_host_licensing
# License edition, key, feature list, expiry, and assignment details.
# ------------------------------------------------------------------------------
def esxi_host_licensing(host_name: str | None = None) -> dict[str, Any]:
    """
    Return license information from the ESXi host's LicenseManager.

    Two related objects are queried:
      licenseManager.licenses         — available license definitions
      licenseManager.licenseAssignmentManager — what is actually assigned

    The licenseAssignmentManager call is wrapped in try/except because some
    ESXi configurations restrict access to assignment data even for root;
    missing assignment data is non-fatal.

    License properties (ProductName, ProductVersion, expirationDate) are
    stored as a heterogeneous key/value list in lic.properties, so we flatten
    it to a dict for clean JSON output.
    """
    conn = ConnectionManager.instance().get_connection(host_name)
    content = conn.get_content()
    lic_mgr = content.licenseManager

    # --- Build license definitions list ---
    licenses = []
    for lic in lic_mgr.licenses:
        # Flatten the properties list into a dict for readable output.
        props = {p.key: p.value for p in (lic.properties or [])}
        licenses.append({
            "name": lic.name,
            "key": lic.licenseKey,
            "edition": props.get("ProductName", ""),
            "version": props.get("ProductVersion", ""),
            "total_units": lic.total,
            "used_units": lic.used,
            # expirationDate is absent for perpetual licenses; default to "Never"
            # so the JSON value is always a string rather than null.
            "expiration": props.get("expirationDate", "Never"),
            "features": [f.key for f in (lic.featureInfo or [])],
        })

    # --- Build assignment list ---
    # Assignments show what license is actually applied to this host's entity ID.
    assignments = []
    try:
        assign_mgr = lic_mgr.licenseAssignmentManager
        for assignment in assign_mgr.QueryAssignedLicenses():
            assignments.append({
                "entity_id": assignment.entityId,
                "entity_display": assignment.entityDisplayName,
                "license_name": assignment.assignedLicense.name,
                "license_key": assignment.assignedLicense.licenseKey,
            })
    except Exception:
        # Assignment manager access may be restricted on some ESXi configs.
        # We return whatever we have rather than raising.
        pass

    return {
        "host": conn.config.name,
        "licenses": licenses,
        "assignments": assignments,
    }


# ------------------------------------------------------------------------------
# esxi_host_network
# Physical NICs, standard vSwitches, port groups, and VMkernel adapters.
# ------------------------------------------------------------------------------
def esxi_host_network(host_name: str | None = None) -> dict[str, Any]:
    """
    Return the full network configuration tree for an ESXi host.

    Four categories of network object are returned:
      physical_nics      — pNICs (vmnic0, vmnic1, …): speed, MAC, driver
      vswitches          — standard vSwitches: port counts, MTU, uplink pNICs
      port_groups        — named port groups: associated vSwitch, VLAN ID
      vmkernel_adapters  — vmk adapters: IP, subnet, DHCP, MTU, portgroup

    Distributed vSwitches (VDS) are a vCenter feature and are not present
    on standalone ESXi; this function only queries host.config.network, which
    covers standard switches.

    The nicTeaming block access is guarded because ESXi returns a sparse object
    graph — some vSwitch properties are absent if no uplinks are configured.
    """
    conn = ConnectionManager.instance().get_connection(host_name)
    content = conn.get_content()
    host = content.rootFolder.childEntity[0].host[0]
    net_cfg = host.config.network

    # --- Physical NICs ---
    pnics = []
    for p in (net_cfg.pnic or []):
        pnics.append({
            "device": p.device,
            "mac": p.mac,
            "driver": p.driver,
            # linkSpeed is None when the NIC is disconnected or the link is down.
            "link_speed_mb": p.linkSpeed.speedMb if p.linkSpeed else None,
            "duplex": p.linkSpeed.duplex if p.linkSpeed else None,
        })

    # --- Standard vSwitches ---
    vswitches = []
    for vs in (net_cfg.vswitch or []):
        # Safely navigate the nicTeaming → nicOrder path; any segment can be
        # absent on a freshly configured or partially configured vSwitch.
        uplinks = []
        try:
            order = vs.spec.policy.nicTeaming.nicOrder
            uplinks = list(order.activeNic or []) + list(order.standbyNic or [])
        except AttributeError:
            pass

        vswitches.append({
            "name": vs.name,
            "num_ports": vs.numPorts,
            "num_ports_available": vs.numPortsAvailable,
            "mtu": vs.mtu,
            "uplink_pnics": uplinks,
            "port_groups": list(vs.portgroup or []),
        })

    # --- Port Groups ---
    portgroups = []
    for pg in (net_cfg.portgroup or []):
        portgroups.append({
            "name": pg.spec.name,
            "vlan_id": pg.spec.vlanId,
            "vswitch": pg.spec.vswitchName,
        })

    # --- VMkernel Adapters (vmk0, vmk1, …) ---
    # These carry host management traffic, vMotion traffic (if configured),
    # and storage traffic.  Each vmk is bound to a port group on a vSwitch.
    vmknics = []
    for vnic in (net_cfg.vnic or []):
        vmknics.append({
            "device": vnic.device,
            "portgroup": vnic.portgroup,
            "ip": vnic.spec.ip.ipAddress,
            "subnet": vnic.spec.ip.subnetMask,
            "dhcp": vnic.spec.ip.dhcp,
            "mac": vnic.spec.mac,
            "mtu": vnic.spec.mtu,
        })

    return {
        "host": conn.config.name,
        "physical_nics": pnics,
        "vswitches": vswitches,
        "port_groups": portgroups,
        "vmkernel_adapters": vmknics,
    }


# ------------------------------------------------------------------------------
# esxi_host_events
# Most recent host events from the ESXi event log.
# ------------------------------------------------------------------------------
def esxi_host_events(
    host_name: str | None = None,
    max_events: int = DEFAULT_MAX_EVENTS,
) -> dict[str, Any]:
    """
    Return recent events from the ESXi host event log, newest first.

    Uses EventManager.QueryEvents() with an entity filter scoped to the host
    itself (RecursionOption.self) so we don't pick up VM-level events that
    happen to be visible in the same inventory.

    The maxCount filter limits results at the server before the payload crosses
    the wire — important because ESXi event logs can contain tens of thousands
    of entries and we don't want to transfer them all to sort client-side.

    Error handling: if the event query fails (rare but possible if the ESXi
    eventManager service is momentarily unavailable), we return an empty list
    with the error message rather than raising — partial data is more useful
    than a complete failure when Claude is using this for situational awareness.
    """
    conn = ConnectionManager.instance().get_connection(host_name)
    content = conn.get_content()
    host = content.rootFolder.childEntity[0].host[0]
    event_mgr = content.eventManager

    # Build a filter that scopes events to this host object only.
    filter_spec = vim.event.EventFilterSpec()
    filter_spec.entity = vim.event.EventFilterSpec.ByEntity()
    filter_spec.entity.entity = host
    filter_spec.entity.recursion = (
        vim.event.EventFilterSpec.RecursionOption.self
    )
    filter_spec.maxCount = max_events

    try:
        events = event_mgr.QueryEvents(filter_spec)
    except Exception as exc:
        return {
            "host": conn.config.name,
            "error": str(exc),
            "events": [],
        }

    # Sort newest-first so Claude sees the most recent activity at the top
    # without needing to scroll through the list.
    result = []
    for ev in sorted(events, key=lambda e: e.createdTime, reverse=True):
        result.append({
            "time": ev.createdTime.isoformat(),
            "type": type(ev).__name__,
            "message": ev.fullFormattedMessage,
            # userName is present on user-initiated events but absent on system
            # events; getattr guards against AttributeError.
            "user": getattr(ev, "userName", ""),
        })

    return {
        "host": conn.config.name,
        "event_count": len(result),
        "events": result,
    }
