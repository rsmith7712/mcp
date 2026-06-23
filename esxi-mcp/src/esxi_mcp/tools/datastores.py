"""
==============================================================================
Script Name:    tools/datastores.py
Synopsis:       ESXi datastore capacity and file browser tools.
Description:    Implements two MCP tool functions for querying datastore
                state on a standalone ESXi 6.7 host:
                  esxi_list_datastores   — capacity, free space, usage %, type
                  esxi_datastore_files   — browse files in a datastore path

                Results from esxi_list_datastores are sorted by usage
                (highest first) so the most-pressured datastores appear at
                the top — the most useful ordering for capacity planning.

                File browsing uses the DatastoreBrowser API which runs as an
                asynchronous ESXi task; we poll for completion before returning.

Parameters /
Setup:          No module-level parameters.  host_name is optional in both
                functions and defaults to the configured default host.
                esxi_datastore_files requires a datastore_name; path defaults
                to the datastore root if omitted.

Change Log:
  2026-06-23  Richard Smith   Initial version — list datastores, file browser
                              with async task polling.
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
# VARIABLES — unit conversion and polling constants
# ==============================================================================

# Bytes → gigabytes divisor.  Datastore sizes are reported in bytes by pyVmomi
# but displayed in GB for readability.
BYTES_PER_GB: int = 1024 * 1024 * 1024

# Maximum seconds to wait for a DatastoreBrowser.Search() task to complete.
# File browsing is a server-side operation; 30 seconds is generous for even
# a heavily loaded ESXi host browsing a large directory.
BROWSER_TASK_TIMEOUT_SECONDS: int = 30

# Poll interval when waiting for the browser task.
BROWSER_POLL_INTERVAL_SECONDS: float = 1.0


# ==============================================================================
# FUNCTIONS — unit conversion helpers
# ==============================================================================

def _gb(bytes_val: int) -> float:
    """
    Convert bytes to gigabytes, rounded to two decimal places.
    Consistent rounding prevents floating-point noise in JSON output
    (e.g., 999.9999999... → 1000.0 versus 999.99).
    """
    return round(bytes_val / BYTES_PER_GB, 2)


def _pct(used: int, total: int) -> float:
    """
    Calculate used/total as a percentage.  Guards against zero-division
    for datastores that haven't fully initialised their capacity reporting.
    """
    return round(used / total * 100, 1) if total else 0.0


# ==============================================================================
# FUNCTIONS — async task polling helper
# ==============================================================================

def _wait_for_browser_task(task: vim.Task) -> Any:
    """
    Poll a DatastoreBrowser.Search() task until it succeeds or the timeout
    expires.  Returns task.info.result on success.

    Browser tasks are much faster than VM power tasks, but they are still
    async — calling task.info.result immediately after Search() returns
    None until the server completes the directory scan.

    We use time.monotonic() rather than time.time() for the deadline to
    avoid skew from system clock adjustments during the wait.
    """
    deadline = time.monotonic() + BROWSER_TASK_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        state = task.info.state

        if state == "success":
            return task.info.result

        if state == "error":
            raise RuntimeError(
                f"Datastore browser task failed: {task.info.error.msg}"
            )

        time.sleep(BROWSER_POLL_INTERVAL_SECONDS)

    raise TimeoutError(
        f"Datastore file browser did not complete within "
        f"{BROWSER_TASK_TIMEOUT_SECONDS} seconds."
    )


# ==============================================================================
# MAIN — MCP tool functions (called by server.py dispatch)
# ==============================================================================

# ------------------------------------------------------------------------------
# esxi_list_datastores
# Return capacity, usage, and connection state for all visible datastores.
# ------------------------------------------------------------------------------
def esxi_list_datastores(host_name: str | None = None) -> dict[str, Any]:
    """
    List all datastores visible to the ESXi host sorted by usage (highest first).

    Datastore visibility on a standalone host depends on which storage adapters
    (HBAs, iSCSI initiators, NFS mounts) are configured.  A datastore shared
    across both ESXi hosts over a shared SAN or NFS mount will appear on both
    hosts independently — this function queries the local host's view only.

    summary.capacity and summary.freeSpace are in bytes.  We derive used space
    by subtraction rather than trusting a third API field to avoid any
    inconsistency between fields that are updated at different intervals.

    Sorting by descending used_pct ensures datastores under pressure appear
    first in the list, making capacity concerns immediately visible without
    requiring Claude to scan through the full list.
    """
    conn = ConnectionManager.instance().get_connection(host_name)
    content = conn.get_content()

    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.Datastore], True
    )
    try:
        results = []
        for ds in container.view:
            summary = ds.summary
            capacity = summary.capacity
            free = summary.freeSpace
            used = capacity - free  # Derived; no direct "used" field in the API

            results.append({
                "name": summary.name,
                "type": summary.type,
                # URL format: ds:///vmfs/volumes/<uuid>/ for VMFS,
                # nfs://<server>/<path> for NFS.
                "url": ds.info.url if hasattr(ds.info, "url") else "",
                "accessible": summary.accessible,
                "maintenance_mode": summary.maintenanceMode,
                "capacity_gb": _gb(capacity),
                "free_gb": _gb(free),
                "used_gb": _gb(used),
                "used_pct": _pct(used, capacity),
                # vm count: how many VMs have files on this datastore.
                "vm_count": len(ds.vm or []),
            })
    finally:
        container.Destroy()

    # Sort by usage descending so capacity pressure is visible at a glance.
    results.sort(key=lambda d: d["used_pct"], reverse=True)

    return {
        "host": conn.config.name,
        "datastore_count": len(results),
        "datastores": results,
    }


# ------------------------------------------------------------------------------
# esxi_datastore_files
# Browse files in a datastore directory via the DatastoreBrowser API.
# ------------------------------------------------------------------------------
def esxi_datastore_files(
    datastore_name: str,
    path: str = "",
    host_name: str | None = None,
) -> dict[str, Any]:
    """
    Browse files inside a datastore directory.

    The DatastoreBrowser API is the correct mechanism for listing datastore
    contents — it runs on the ESXi host itself and traverses the VMFS or NFS
    volume directly.  This is different from the datastore HTTP file access
    endpoint (/folder), which has more authentication complexity.

    The path argument uses the VMFS path convention within the datastore —
    for example "" for root, or "SymUtility" for a VM subdirectory.
    The full datastore path we construct is: "[datastore_name] path"
    which is the standard ESXi bracket notation.

    Results are sorted by file size descending so the largest files appear
    first — useful for identifying snapshot chains, large ISOs, or unexpectedly
    large log files consuming datastore space.

    File sizes are returned in MB rather than bytes for readability.  Files
    smaller than 1 KB round to 0.00 MB — acceptable for a directory listing.
    """
    conn = ConnectionManager.instance().get_connection(host_name)
    content = conn.get_content()

    # --- Locate the target datastore ---
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.Datastore], True
    )
    ds = None
    try:
        for candidate in container.view:
            if candidate.name == datastore_name:
                ds = candidate
                break
    finally:
        container.Destroy()

    if ds is None:
        raise ValueError(
            f"Datastore '{datastore_name}' not found. "
            f"Use esxi_list_datastores to see available datastores."
        )

    # --- Configure the browser search spec ---
    browser = ds.browser
    search_spec = vim.host.DatastoreBrowser.SearchSpec()

    # Request file type, size, and modification time.  We skip fileOwner
    # because it is rarely populated on VMFS and adds no value here.
    search_spec.details = vim.host.DatastoreBrowser.FileInfo.Details(
        fileType=True,
        fileSize=True,
        fileOwner=False,
        modification=True,
    )

    # ESXi bracket notation: "[datastoreName] relativePath"
    # An empty path after stripping gives us the root directory.
    ds_path = f"[{datastore_name}]" + (f" {path}" if path else "")

    # --- Run the async browser task ---
    task = browser.Search(datastorePath=ds_path, searchSpec=search_spec)
    result = _wait_for_browser_task(task)

    # --- Format file list ---
    files = []
    for f in (result.file or []):
        files.append({
            "name": f.path,
            # fileSize is in bytes; convert to MB for readability.
            "size_mb": round(f.fileSize / 1024 / 1024, 2) if f.fileSize else 0.0,
            "modified": f.modification.isoformat() if f.modification else None,
            # The class name tells us the file type: VmDiskFileInfo, VmConfigFileInfo,
            # LogFileInfo, etc. — more informative than a raw extension.
            "type": type(f).__name__,
        })

    # Sort by size descending to surface large files at the top of the list.
    files.sort(key=lambda x: x["size_mb"], reverse=True)

    return {
        "host": conn.config.name,
        "datastore": datastore_name,
        "path": path if path else "/",
        "file_count": len(files),
        "files": files,
    }
