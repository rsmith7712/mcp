"""
==============================================================================
Script Name:    tools/vms.py
Synopsis:       VM inventory, power management, and cold migration tools.
Description:    Implements four MCP tool functions for virtual machine
                lifecycle management on a standalone ESXi 6.7 host:
                  esxi_list_vms    — inventory with power state, OS, IP
                  esxi_vm_detail   — full detail: disks, NICs, live metrics
                  esxi_vm_power    — get/set power state (on/off/reset/suspend/
                                     shutdown/reboot)
                  esxi_vm_migrate  — cold-migrate VM to a different datastore

                IMPORTANT — vMotion scope limitation:
                Live vMotion between two ESXi hosts requires vCenter Server
                to orchestrate the migration.  On standalone ESXi hosts,
                cross-host migration is only possible by powering the VM off
                first and using the Relocate API (cold migration).  That is
                what esxi_vm_migrate implements.  The function enforces the
                powered-off requirement and documents this constraint clearly
                in its docstring and tool description.

Parameters /
Setup:          No module-level parameters.  All functions accept optional
                host_name; omitting it uses the configured default host.
                esxi_vm_power requires both vm_name and action.
                esxi_vm_migrate requires vm_name and target_datastore.

Change Log:
  2026-06-23  Richard Smith   Initial version — list, detail, power management,
                              cold datastore migration, task polling.
==============================================================================
"""

from __future__ import annotations

# ==============================================================================
# IMPORTS
# ==============================================================================
import time
from typing import Any

from pyVmomi import vim

from ..connection import ConnectionManager


# ==============================================================================
# VARIABLES — constants for power state management and task polling
# ==============================================================================

# Valid power action strings accepted by esxi_vm_power.
# Defined as a set for O(1) membership testing in the validation check.
VALID_POWER_ACTIONS: frozenset[str] = frozenset({
    "status",    # Read-only: return current power state, no change
    "power_on",  # Hard power on
    "power_off", # Hard power off — equivalent to pulling the power cord
    "reset",     # Hard reset — no OS shutdown, immediate reboot
    "suspend",   # Suspend to memory (VM must be powered on)
    "shutdown",  # Graceful guest OS shutdown via VMware Tools
    "reboot",    # Graceful guest OS reboot via VMware Tools
})

# Default timeout in seconds for waiting on a vSphere Task to complete.
# Most power operations (on/off/reset/suspend) complete well under 60 seconds.
# Migration gets a longer timeout since it involves copying disk data.
TASK_TIMEOUT_SECONDS: int = 120
MIGRATE_TASK_TIMEOUT_SECONDS: int = 600

# Poll interval when waiting for a task — 2 seconds balances responsiveness
# against unnecessary API traffic to the host.
TASK_POLL_INTERVAL_SECONDS: int = 2


# ==============================================================================
# FUNCTIONS — internal helpers (not exposed as MCP tools)
# ==============================================================================

def _vm_disk_info(vm: vim.VirtualMachine) -> list[dict]:
    """
    Extract disk layout from a VM's virtual hardware device list.
    Iterates the full device list and filters for VirtualDisk instances.
    capacityInKB is the pyVmomi field name; we convert to GB for readability.
    The backing object holds the datastore path for file-backed disks; we
    guard with hasattr because raw device mappings (RDMs) use a different
    backing type that lacks fileName.
    """
    disks = []
    for dev in (vm.config.hardware.device or []):
        if isinstance(dev, vim.vm.device.VirtualDisk):
            disks.append({
                "label": dev.deviceInfo.label,
                "capacity_gb": round(dev.capacityInKB / 1024 / 1024, 2),
                "datastore_path": (
                    dev.backing.fileName
                    if hasattr(dev.backing, "fileName") else ""
                ),
                "thin_provisioned": getattr(dev.backing, "thinProvisioned", None),
            })
    return disks


def _vm_nic_info(vm: vim.VirtualMachine) -> list[dict]:
    """
    Extract network adapter configuration from the VM's device list.
    All virtual Ethernet cards inherit from VirtualEthernetCard, so
    isinstance check on the base class catches e1000, vmxnet3, etc.
    Network backing varies: standard port groups use deviceName on a
    VirtualEthernetCard.NetworkBackingInfo, while DVS port groups use a
    DistributedVirtualPortBackingInfo with a port.portgroupKey.
    We handle both without raising by chaining getattr calls.
    """
    nics = []
    for dev in (vm.config.hardware.device or []):
        if isinstance(dev, vim.vm.device.VirtualEthernetCard):
            # Try standard port group name first; fall back to DVS portgroup key.
            network_name = getattr(dev.backing, "deviceName", "") or getattr(
                getattr(dev.backing, "port", None), "portgroupKey", ""
            )
            nics.append({
                "label": dev.deviceInfo.label,
                "adapter_type": type(dev).__name__,
                "mac": dev.macAddress,
                "network": network_name,
                "connected": dev.connectable.connected if dev.connectable else None,
                "start_connected": dev.connectable.startConnected if dev.connectable else None,
            })
    return nics


def _count_snapshots(snapshot_list) -> int:
    """
    Recursively count all snapshots in a snapshot tree.
    ESXi stores snapshots as a tree (snapshots can have children), so a simple
    len() of the root list would miss nested snapshots.  This recursive count
    ensures we report the true total regardless of snapshot tree depth.
    """
    count = 0
    for snap in (snapshot_list or []):
        count += 1 + _count_snapshots(snap.childSnapshotList)
    return count


def _vm_to_dict(vm: vim.VirtualMachine, include_detail: bool = False) -> dict[str, Any]:
    """
    Convert a pyVmomi VirtualMachine object to a serialisable dict.
    The summary object is used for both list and detail modes because it
    combines config, runtime, and guest state in a single pre-fetched payload,
    avoiding multiple round trips for the common list case.
    When include_detail is True, we also read vm.config.hardware.device for
    disk/NIC layout and vm.snapshot for snapshot count — these require
    additional API traversal and are skipped in list mode for performance.
    """
    summary = vm.summary
    config = summary.config
    runtime = summary.runtime
    guest = summary.guest

    result: dict[str, Any] = {
        "name": config.name,
        "power_state": runtime.powerState,
        "guest_os": config.guestFullName,
        "num_cpu": config.numCpu,
        "memory_mb": config.memorySizeMB,
        "num_disks": config.numVirtualDisks,
        "num_nics": config.numEthernetCards,
        # IP and hostname come from the guest agent; None if tools not running.
        "ip_address": guest.ipAddress if guest else None,
        "hostname": guest.hostName if guest else None,
        "tools_status": guest.toolsStatus if guest else None,
        "overall_status": summary.overallStatus,
        "annotation": config.annotation,
    }

    if include_detail and vm.config:
        # quickStats are rolling averages; values reflect recent activity,
        # not instantaneous — sufficient for capacity planning but not
        # for real-time monitoring.
        result["cpu_used_mhz"] = summary.quickStats.overallCpuUsage
        result["memory_active_mb"] = summary.quickStats.guestMemoryUsage
        result["memory_ballooned_mb"] = summary.quickStats.balloonedMemory
        result["uptime_seconds"] = summary.quickStats.uptimeSeconds
        result["disks"] = _vm_disk_info(vm)
        result["nics"] = _vm_nic_info(vm)
        result["datastore_urls"] = [ds.info.url for ds in (vm.datastore or [])]
        # Snapshot count: traverse the full tree rather than len(rootSnapshotList)
        # to catch child snapshots.  Returns 0 cleanly if vm.snapshot is None.
        result["snapshot_count"] = (
            _count_snapshots(vm.snapshot.rootSnapshotList)
            if vm.snapshot else 0
        )

    return result


def _find_vm(content: vim.ServiceInstanceContent, vm_name: str) -> vim.VirtualMachine:
    """
    Search the host inventory for a VM by exact name.
    ContainerView is the standard pyVmomi pattern for iterating managed objects
    of a given type across the entire inventory tree.  We destroy the view
    immediately after use to release the server-side resource.
    VM names in ESXi are case-sensitive; the error message reminds callers to
    use esxi_list_vms to confirm the exact name.
    """
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    try:
        for vm in container.view:
            if vm.name == vm_name:
                return vm
    finally:
        # Always destroy the container view even if we return early or raise,
        # to avoid accumulating stale views on the ESXi host.
        container.Destroy()

    raise ValueError(
        f"VM '{vm_name}' not found. "
        f"Use esxi_list_vms to confirm the exact name (case-sensitive)."
    )


def _find_datastore(
    content: vim.ServiceInstanceContent, ds_name: str
) -> vim.Datastore:
    """
    Search the host inventory for a Datastore by exact name.
    Same ContainerView pattern as _find_vm.  Used by esxi_vm_migrate to
    resolve a user-provided datastore name to a managed object reference
    before constructing the RelocateSpec.
    """
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.Datastore], True
    )
    try:
        for ds in container.view:
            if ds.name == ds_name:
                return ds
    finally:
        container.Destroy()

    raise ValueError(
        f"Datastore '{ds_name}' not found. "
        f"Use esxi_list_datastores to see available datastores."
    )


def _wait_for_task(task: vim.Task, timeout: int = TASK_TIMEOUT_SECONDS) -> str:
    """
    Poll a vSphere Task object until it reaches a terminal state (success or
    error) or the timeout is exceeded.

    ESXi tasks are asynchronous — methods like PowerOn() return a Task object
    immediately and execute in the background.  We poll rather than using
    WaitForTask() from pyVmomi.task because the helper module isn't always
    available and polling gives us explicit timeout control.

    Raises RuntimeError if the task fails, TimeoutError if it doesn't complete
    within the timeout window.  Both are caught and JSON-serialised in server.py.
    """
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        state = task.info.state

        if state == vim.TaskInfo.State.success:
            return "success"

        if state == vim.TaskInfo.State.error:
            # task.info.error is a LocalizedMethodFault; .msg is the human
            # readable string.
            raise RuntimeError(task.info.error.msg)

        # Task is still queued or running — wait before polling again.
        time.sleep(TASK_POLL_INTERVAL_SECONDS)

    raise TimeoutError(
        f"Task did not complete within {timeout} seconds. "
        f"Check the ESXi task console for current status."
    )


# ==============================================================================
# MAIN — MCP tool functions (called by server.py dispatch)
# ==============================================================================

# ------------------------------------------------------------------------------
# esxi_list_vms
# Return a lightweight inventory of all VMs on a host.
# ------------------------------------------------------------------------------
def esxi_list_vms(host_name: str | None = None) -> dict[str, Any]:
    """
    List all virtual machines registered on an ESXi host.
    Returns summary-level data (power state, OS, CPU, memory, IP) without
    fetching disk or NIC device lists — fast enough to use as a first-look
    inventory tool before drilling into specific VMs with esxi_vm_detail.
    """
    conn = ConnectionManager.instance().get_connection(host_name)
    content = conn.get_content()

    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    try:
        # include_detail=False keeps this call fast for large inventories.
        vms = [_vm_to_dict(vm, include_detail=False) for vm in container.view]
    finally:
        container.Destroy()

    return {
        "host": conn.config.name,
        "vm_count": len(vms),
        "vms": vms,
    }


# ------------------------------------------------------------------------------
# esxi_vm_detail
# Full detail for a single VM including disks, NICs, and live metrics.
# ------------------------------------------------------------------------------
def esxi_vm_detail(vm_name: str, host_name: str | None = None) -> dict[str, Any]:
    """
    Return comprehensive detail for a single VM.
    Supplements the summary fields from esxi_list_vms with disk layout
    (capacity, thin provisioning, datastore path), NIC configuration
    (adapter type, MAC, port group, connected state), live CPU and memory
    usage, uptime in seconds, and total snapshot count.
    """
    conn = ConnectionManager.instance().get_connection(host_name)
    content = conn.get_content()
    vm = _find_vm(content, vm_name)

    return {
        "host": conn.config.name,
        "vm": _vm_to_dict(vm, include_detail=True),
    }


# ------------------------------------------------------------------------------
# esxi_vm_power
# Get or change VM power state; enforces valid actions and state preconditions.
# ------------------------------------------------------------------------------
def esxi_vm_power(
    vm_name: str,
    action: str,
    host_name: str | None = None,
) -> dict[str, Any]:
    """
    Manage a VM's power state.

    Validates the action string against VALID_POWER_ACTIONS before touching
    the host, so invalid action names produce a clear error message rather
    than an opaque pyVmomi exception.

    Idempotency: power_on when already on and power_off when already off
    return immediately with an informational message rather than raising an
    error — this makes these actions safe to call without checking state first.

    Graceful operations (shutdown, reboot) use Guest operations rather than
    hardware-level power control.  They send a signal to VMware Tools to
    initiate the OS shutdown; if Tools isn't running, the command is silently
    ignored by ESXi.  We document this limitation in the return message.
    """
    if action not in VALID_POWER_ACTIONS:
        raise ValueError(
            f"Invalid action '{action}'. "
            f"Valid actions: {', '.join(sorted(VALID_POWER_ACTIONS))}"
        )

    conn = ConnectionManager.instance().get_connection(host_name)
    content = conn.get_content()
    vm = _find_vm(content, vm_name)
    current_state = vm.runtime.powerState

    # --- Status read-only path (no state change) ---
    if action == "status":
        return {
            "host": conn.config.name,
            "vm": vm_name,
            "power_state": current_state,
            "tools_status": vm.guest.toolsStatus if vm.guest else None,
        }

    # --- State-change paths ---
    task = None

    if action == "power_on":
        # Idempotent — already on means nothing to do.
        if current_state == "poweredOn":
            return {"host": conn.config.name, "vm": vm_name,
                    "result": "already powered on"}
        task = vm.PowerOn()

    elif action == "power_off":
        # Idempotent — already off means nothing to do.
        if current_state == "poweredOff":
            return {"host": conn.config.name, "vm": vm_name,
                    "result": "already powered off"}
        task = vm.PowerOff()

    elif action == "reset":
        # Reset from any running state; ESXi will raise if VM is off.
        task = vm.Reset()

    elif action == "suspend":
        # Suspend is only valid from poweredOn; suspended VMs cannot be
        # suspended again and off VMs cannot be suspended at all.
        if current_state != "poweredOn":
            raise RuntimeError(
                f"VM must be powered on to suspend. "
                f"Current state: {current_state}"
            )
        task = vm.Suspend()

    elif action == "shutdown":
        # ShutdownGuest is a fire-and-forget guest operation — it returns None
        # rather than a Task object, so we can't poll for completion.
        if current_state != "poweredOn":
            raise RuntimeError("VM must be powered on for graceful shutdown.")
        vm.ShutdownGuest()
        return {
            "host": conn.config.name,
            "vm": vm_name,
            "result": "Guest shutdown signal sent. Requires VMware Tools to be running.",
        }

    elif action == "reboot":
        # Same fire-and-forget pattern as ShutdownGuest.
        if current_state != "poweredOn":
            raise RuntimeError("VM must be powered on for graceful reboot.")
        vm.RebootGuest()
        return {
            "host": conn.config.name,
            "vm": vm_name,
            "result": "Guest reboot signal sent. Requires VMware Tools to be running.",
        }

    # Poll the task until completion.  _wait_for_task raises on failure or
    # timeout; those exceptions propagate to server.py and become JSON errors.
    result = _wait_for_task(task)

    return {
        "host": conn.config.name,
        "vm": vm_name,
        "action": action,
        "previous_state": current_state,
        "result": result,
    }


# ------------------------------------------------------------------------------
# esxi_vm_migrate
# Cold-migrate a powered-off VM to a different datastore on the same host.
# ------------------------------------------------------------------------------
def esxi_vm_migrate(
    vm_name: str,
    target_datastore: str,
    host_name: str | None = None,
) -> dict[str, Any]:
    """
    Relocate a VM's storage to a different datastore on the same ESXi host.

    Why cold only: live vMotion between two ESXi hosts requires vCenter Server
    to coordinate the migration.  The Relocate API used here works on a single
    host and moves disk files between datastores.  The VM must be powered off
    because we do not have vCenter to freeze memory state during the transfer.

    The migration copies all VMDK files and the VMX configuration to the new
    datastore.  Depending on disk size and datastore speed this can take
    several minutes; MIGRATE_TASK_TIMEOUT_SECONDS (600s) accommodates large VMs.

    After migration completes, the VM is registered on the same host pointing
    to its new datastore location.  Snapshots and any existing storage policy
    bindings are carried forward by the Relocate operation.
    """
    conn = ConnectionManager.instance().get_connection(host_name)
    content = conn.get_content()

    vm = _find_vm(content, vm_name)

    # Enforce the powered-off requirement before touching the datastore.
    # A clear error here is better than letting ESXi return a cryptic fault.
    if vm.runtime.powerState != "poweredOff":
        raise RuntimeError(
            f"VM '{vm_name}' must be powered off before cold migration. "
            f"Current state: {vm.runtime.powerState}. "
            f"Use esxi_vm_power with action='shutdown' or 'power_off' first."
        )

    ds_target = _find_datastore(content, target_datastore)

    # RelocateSpec with only datastore set moves all VM files to that datastore.
    # Leaving host and pool unset keeps the VM on the same host — correct for
    # standalone ESXi where there is no cluster pool to specify.
    relocate_spec = vim.vm.RelocateSpec()
    relocate_spec.datastore = ds_target

    task = vm.Relocate(relocate_spec)
    result = _wait_for_task(task, timeout=MIGRATE_TASK_TIMEOUT_SECONDS)

    return {
        "host": conn.config.name,
        "vm": vm_name,
        "migrated_to_datastore": target_datastore,
        "result": result,
        "note": (
            "This was a cold datastore migration on the same host. "
            "Cross-host live vMotion requires vCenter Server."
        ),
    }
