"""
==============================================================================
Script Name:    tools/snapshots.py
Synopsis:       VM snapshot management tools for the ESXi MCP server.
Description:    Implements four MCP tool functions for managing VM snapshots
                on a standalone ESXi 6.7 host:
                  esxi_vm_snapshot_list    — list all snapshots in tree order
                  esxi_vm_snapshot_create  — create a snapshot
                  esxi_vm_snapshot_revert  — revert to a named snapshot
                  esxi_vm_snapshot_delete  — delete a named snapshot

                ESXi stores snapshots as a tree (each snapshot can have
                children), not a flat list.  The list function flattens
                the tree while preserving depth information so callers can
                reconstruct the hierarchy if needed.  The find helper
                traverses the tree recursively to locate snapshots by name
                for revert and delete operations.

                WARNING notes embedded in esxi_vm_snapshot_revert:
                Reverting discards all changes made after the snapshot was
                taken.  This is irreversible.  The function does not add any
                additional confirmation gate — the MCP tool description carries
                the warning so Claude surfaces it to the user before calling.

Parameters /
Setup:          No module-level parameters.  All functions accept optional
                host_name. Snapshot names are matched by exact string —
                ESXi snapshot names are not required to be unique within a VM,
                so this function finds and operates on the first match.

Change Log:
  2026-06-23  Richard Smith   Initial version — list, create, revert, delete;
                              recursive tree traversal; task polling via shared
                              helper from vms.py.
==============================================================================
"""

from __future__ import annotations

# ==============================================================================
# IMPORTS
# ==============================================================================
from typing import Any

from pyVmomi import vim

from ..connection import ConnectionManager
from .vms import _find_vm, _wait_for_task


# ==============================================================================
# VARIABLES — snapshot operation defaults
# ==============================================================================

# Default for the memory flag on esxi_vm_snapshot_create.
# False means the snapshot does NOT capture memory state, making it faster
# and smaller.  Set to True only when you need a crash-consistent memory image
# (e.g., before OS-level changes on a running VM where instant rollback matters).
DEFAULT_SNAPSHOT_MEMORY: bool = False

# Default for the quiesce flag on esxi_vm_snapshot_create.
# True freezes the guest filesystem before snapping (requires VMware Tools)
# producing an application-consistent backup point.  False is the safe default
# because it works regardless of Tools status.
DEFAULT_SNAPSHOT_QUIESCE: bool = False

# Default for remove_children on esxi_vm_snapshot_delete.
# False preserves child snapshots when a parent is deleted; True cascades the
# delete down the subtree.  False is the safe default — children are explicit
# to remove.
DEFAULT_REMOVE_CHILDREN: bool = False


# ==============================================================================
# FUNCTIONS — snapshot tree traversal helpers
# ==============================================================================

def _snapshot_tree_to_list(
    snapshot_list, depth: int = 0
) -> list[dict[str, Any]]:
    """
    Recursively flatten a pyVmomi snapshot tree into a list of dicts.
    ESXi represents snapshots as a nested tree; we flatten it while tagging
    each entry with its depth so callers can reconstruct the hierarchy or
    display it indented.

    The 'children' field reports how many direct child snapshots this node
    has, letting callers understand branching without recursing themselves.

    Snapshots at depth 0 are root-level (no parent); depth 1 are children
    of a root snapshot, and so on.
    """
    result = []
    for snap in (snapshot_list or []):
        result.append({
            "name": snap.name,
            "description": snap.description,
            "created": snap.createTime.isoformat(),
            # power state of the VM when this snapshot was taken
            "state": snap.state,
            "depth": depth,
            "id": snap.id,
            "children": len(snap.childSnapshotList or []),
        })
        # Recurse into children, incrementing depth each level.
        result.extend(
            _snapshot_tree_to_list(snap.childSnapshotList, depth + 1)
        )
    return result


def _find_snapshot_by_name(
    snapshot_list, name: str
) -> vim.vm.Snapshot | None:
    """
    Recursively search the snapshot tree for the first snapshot whose name
    matches exactly and return the vim.vm.Snapshot managed object.

    Returns None if not found — callers raise ValueError with a helpful
    message when None is returned rather than letting a NoneType error
    bubble up from the Revert/Remove call.

    ESXi allows duplicate snapshot names within a VM.  We find the first
    match (depth-first, tree order) and document this behaviour so operators
    know to use unique names when precision matters.
    """
    for snap in (snapshot_list or []):
        if snap.name == name:
            # snap.snapshot is the managed object reference for API operations;
            # snap itself is just the SnapshotTree data object.
            return snap.snapshot
        found = _find_snapshot_by_name(snap.childSnapshotList, name)
        if found:
            return found
    return None


# ==============================================================================
# MAIN — MCP tool functions (called by server.py dispatch)
# ==============================================================================

# ------------------------------------------------------------------------------
# esxi_vm_snapshot_list
# Return all snapshots for a VM in flattened tree order.
# ------------------------------------------------------------------------------
def esxi_vm_snapshot_list(
    vm_name: str, host_name: str | None = None
) -> dict[str, Any]:
    """
    List all snapshots for a VM, flattened from the snapshot tree.

    Returns an empty list (snapshot_count: 0) if the VM has no snapshots
    rather than raising — a VM with no snapshots is a valid and common state.

    The depth field in each snapshot entry allows callers to reconstruct
    the tree visually by indenting at depth * some unit.
    """
    conn = ConnectionManager.instance().get_connection(host_name)
    content = conn.get_content()
    vm = _find_vm(content, vm_name)

    # vm.snapshot is None when no snapshots exist — always check before
    # accessing vm.snapshot.rootSnapshotList to avoid AttributeError.
    if not vm.snapshot:
        return {
            "host": conn.config.name,
            "vm": vm_name,
            "snapshot_count": 0,
            "snapshots": [],
        }

    snapshots = _snapshot_tree_to_list(vm.snapshot.rootSnapshotList)

    return {
        "host": conn.config.name,
        "vm": vm_name,
        "snapshot_count": len(snapshots),
        "snapshots": snapshots,
    }


# ------------------------------------------------------------------------------
# esxi_vm_snapshot_create
# Take a new snapshot of the specified VM.
# ------------------------------------------------------------------------------
def esxi_vm_snapshot_create(
    vm_name: str,
    snapshot_name: str,
    description: str = "",
    memory: bool = DEFAULT_SNAPSHOT_MEMORY,
    quiesce: bool = DEFAULT_SNAPSHOT_QUIESCE,
    host_name: str | None = None,
) -> dict[str, Any]:
    """
    Create a VM snapshot.

    memory=True: includes a copy of the guest's memory state in the snapshot.
    The VM remains powered on during the snapshot (no freeze); the snapshot
    file will be significantly larger and the operation takes longer.  Use
    when you need a full suspend-to-disk style rollback point.

    quiesce=True: instructs VMware Tools to flush and freeze the guest
    filesystem before the snapshot is taken, then thaw it afterwards.
    This produces an application-consistent snapshot suitable for backup.
    Requires VMware Tools to be installed and running; fails silently if Tools
    is absent (ESXi falls back to crash-consistent).

    The snapshot name does not need to be unique — ESXi allows multiple
    snapshots with the same name.  Using unique names is strongly recommended
    to avoid ambiguity in revert and delete operations.
    """
    conn = ConnectionManager.instance().get_connection(host_name)
    content = conn.get_content()
    vm = _find_vm(content, vm_name)

    task = vm.CreateSnapshot(
        name=snapshot_name,
        description=description,
        memory=memory,
        quiesce=quiesce,
    )
    result = _wait_for_task(task)

    return {
        "host": conn.config.name,
        "vm": vm_name,
        "snapshot_name": snapshot_name,
        "description": description,
        "memory_included": memory,
        "quiesced": quiesce,
        "result": result,
    }


# ------------------------------------------------------------------------------
# esxi_vm_snapshot_revert
# Roll the VM back to a previously taken snapshot.
# ------------------------------------------------------------------------------
def esxi_vm_snapshot_revert(
    vm_name: str,
    snapshot_name: str,
    host_name: str | None = None,
) -> dict[str, Any]:
    """
    Revert a VM to a named snapshot.

    WARNING: All changes made to the VM after the target snapshot was taken
    are permanently discarded.  This includes disk writes, configuration
    changes, and any snapshots taken after this one (child snapshots of
    the reverted snapshot are NOT deleted, but the VM state rolls back to
    the target snapshot regardless).

    If the snapshot was taken with memory=True, the VM will resume running
    from the captured memory state.  If taken without memory, the VM powers
    off after revert and must be powered on manually.

    Snapshot lookup is first-match by name.  If multiple snapshots share
    the same name, use esxi_vm_snapshot_list to verify the tree structure
    and rename duplicates if necessary before reverting.
    """
    conn = ConnectionManager.instance().get_connection(host_name)
    content = conn.get_content()
    vm = _find_vm(content, vm_name)

    if not vm.snapshot:
        raise ValueError(
            f"VM '{vm_name}' has no snapshots to revert to."
        )

    snap_obj = _find_snapshot_by_name(vm.snapshot.rootSnapshotList, snapshot_name)
    if snap_obj is None:
        raise ValueError(
            f"Snapshot '{snapshot_name}' not found on VM '{vm_name}'. "
            f"Use esxi_vm_snapshot_list to see available snapshots."
        )

    task = snap_obj.RevertToSnapshot_Task()
    result = _wait_for_task(task)

    return {
        "host": conn.config.name,
        "vm": vm_name,
        "reverted_to": snapshot_name,
        "result": result,
        "note": (
            "All VM changes after this snapshot was taken have been discarded. "
            "If the snapshot was taken without memory, the VM is now powered off."
        ),
    }


# ------------------------------------------------------------------------------
# esxi_vm_snapshot_delete
# Remove a snapshot, optionally cascading to its children.
# ------------------------------------------------------------------------------
def esxi_vm_snapshot_delete(
    vm_name: str,
    snapshot_name: str,
    remove_children: bool = DEFAULT_REMOVE_CHILDREN,
    host_name: str | None = None,
) -> dict[str, Any]:
    """
    Delete a named VM snapshot.

    remove_children=False (default): ESXi merges this snapshot's disk delta
    with its parent and removes the snapshot node.  Child snapshots of this
    node are re-parented to the deleted node's parent and remain intact.

    remove_children=True: This snapshot AND all snapshots descended from it
    in the tree are deleted.  Use with caution — this is irreversible and
    can remove many snapshots in a single operation if the tree is deep.

    Deleting a snapshot does NOT change the VM's current disk state — it
    merges the snapshot delta back into its parent layer.  This is different
    from reverting: delete commits the post-snapshot changes, revert discards
    them.
    """
    conn = ConnectionManager.instance().get_connection(host_name)
    content = conn.get_content()
    vm = _find_vm(content, vm_name)

    if not vm.snapshot:
        raise ValueError(
            f"VM '{vm_name}' has no snapshots to delete."
        )

    snap_obj = _find_snapshot_by_name(vm.snapshot.rootSnapshotList, snapshot_name)
    if snap_obj is None:
        raise ValueError(
            f"Snapshot '{snapshot_name}' not found on VM '{vm_name}'. "
            f"Use esxi_vm_snapshot_list to see available snapshots."
        )

    # removeChildren=True cascades the delete to all child snapshots.
    # The consolidate parameter (second arg) tells ESXi to consolidate
    # snapshot disk files after deletion, reclaiming space immediately.
    task = snap_obj.RemoveSnapshot_Task(
        removeChildren=remove_children,
        consolidate=True,
    )
    result = _wait_for_task(task)

    return {
        "host": conn.config.name,
        "vm": vm_name,
        "deleted_snapshot": snapshot_name,
        "children_also_removed": remove_children,
        "result": result,
        "note": (
            "The snapshot delta has been merged into the parent disk layer. "
            "Current VM disk state is unchanged."
        ),
    }
