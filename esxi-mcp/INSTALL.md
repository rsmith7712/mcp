# ESXi MCP Server — Installation Guide

## What this does
TL/DR:	MCP server for VMware ESXi 6.7 host management

DEETS: 	Gives Claude Desktop and Claude Code direct read/write access to your VMware ESXi 6.7 hosts.


Tool -- Ability
- `esxi_list_hosts` -- List configured hosts
- `esxi_host_summary` -- CPU, memory, hardware, uptime, overall status
- `esxi_host_licensing` -- License edition, features, expiration
- `esxi_host_network` -- Physical NICs, vSwitches, port groups, VMkernel adapters
- `esxi_host_events` -- Recent host event log
- `esxi_list_vms` -- VM inventory with power state, OS, IP
- `esxi_vm_detail` -- Disk layout, NIC config, live metrics, snapshot count
- `esxi_vm_power` -- Power on/off/reset/suspend/shutdown/reboot VMs
- `esxi_vm_migrate` -- Cold-migrate VM to a different datastore (VM must be off)
- `esxi_list_datastores` -- Capacity, free space, usage per datastore
- `esxi_datastore_files` -- Browse files in a datastore directory
- `esxi_vm_snapshot_list` -- List VM snapshots
- `esxi_vm_snapshot_create` -- Create a snapshot
- `esxi_vm_snapshot_revert` -- Revert to a snapshot
- `esxi_vm_snapshot_delete` -- Delete a snapshot


> **Note on vMotion and HA:** Live vMotion between hosts and HA cluster management
> require vCenter Server, which is not configured. Cold migration (VM powered off)
> between datastores on the same host is supported.

## Prerequisites

- Python 3.11 or later
- Network access from this machine to your ESXi hosts on port 443
- ESXi root credentials (or a user with sufficient privileges)

## Step 1 — Clone / copy the project

Copy the `esxi-mcp` folder to a permanent location on your machine.
A good place on Windows is `C:\AI\mcp\esxi-mcp` or `C:\Users\<you>\esxi-mcp`.

## Step 2 — Create a Python virtual environment

Open a terminal in the project root:

```powershell

	cd C:\AI\mcp\esxi-mcp
	python -m venv .venv
	.venv\Scripts\Activate.ps1
	pip install -e .

```

This installs the `esxi-mcp` package and all dependencies (pyVmomi, mcp, pyyaml, python-dotenv).

## Step 3 — Configure your ESXi hosts

Edit `config\hosts.yaml` — it contains the ESXi hosts:

```yaml

	hosts:
	  ESX-01:
		host: "10.0.0.11"
		username: "root"
		ssl_verify: false

	  ESX-02:
		host: "10.0.0.12"
		username: "root"
		ssl_verify: false

	default_host: "ESX-01"

```

## Step 4 — Set passwords in .env

Copy `.env.example` to `.env` in the project root, then fill in your root passwords:

```
	ESXI_ESX_01_PASSWORD=your_actual_password
	ESXI_ESX_02_PASSWORD=your_actual_password
```

The `.env` file is gitignored and never committed. Never put passwords in `hosts.yaml`.

## Step 5 — Test the server manually

With the virtual environment active, run:

```powershell

	python -m esxi_mcp.server

```

You should see startup log lines confirming it loaded both hosts. Press Ctrl+C to stop.
If you see a connection error, verify:
- The ESXi host is reachable: `ping 10.0.0.11`
- Port 443 is accessible: `Test-NetConnection 10.0.0.11 -Port 443`
- Your credentials are correct

## Step 6 — Add to Claude Desktop

Open your Claude Desktop configuration file:
- Windows: `%APPDATA%\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

Add the following inside the `"mcpServers"` object (adjust the path to match where
you placed the project and where Python is in your venv):

```json
{
  "mcpServers": {
    "esxi": {
      "command": "C:\\AI\\mcp\\esxi-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "esxi_mcp.server"],
      "cwd": "C:\\AI\\mcp\\esxi-mcp",
      "env": {
        "ESXI_ESX_01_PASSWORD": "your_actual_password",
        "ESXI_ESX_02_PASSWORD": "your_actual_password"
      }
    }
  }
}
```

> **Security note:** If you prefer not to put passwords in claude_desktop_config.json,
> use the `.env` file approach and omit the `"env"` block — python-dotenv will load
> passwords from the `.env` file when the server starts.

Restart Claude Desktop after editing the config.

## Step 7 — Add to Claude Code

Run from the terminal in your repo or home directory:

```powershell

	claude mcp add esxi \
	  --command "C:\AI\mcp\esxi-mcp\.venv\Scripts\python.exe" \
	  --args "-m" "esxi_mcp.server" \
	  --cwd "C:\AI\mcp\esxi-mcp"

```

Or add directly to `.claude/mcp.json`:

```json

	{
	  "mcpServers": {
		"esxi": {
		  "command": "C:\\AI\\mcp\\esxi-mcp\\.venv\\Scripts\\python.exe",
		  "args": ["-m", "esxi_mcp.server"],
		  "cwd": "C:\\AI\\mcp\\esxi-mcp"
		}
	  }
	}

```

## Step 8 — Verify in Claude

After restarting Claude Desktop, ask:

> "List my ESXi hosts and give me a summary of ESX-01"

Claude should call `esxi_list_hosts` and then `esxi_host_summary` and return live data.

## Upgrading

To update dependencies:

```powershell

	cd C:\AI\Claude_Artifacts\esxi-mcp
	.venv\Scripts\Activate.ps1
	pip install -e . --upgrade

```

## Troubleshooting

Problem -- Fix
- `SSL: CERTIFICATE_VERIFY_FAILED` -- Set `ssl_verify: false` in hosts.yaml (ESXi uses self-signed certs)
- `vim.fault.InvalidLogin` -- Check username/password in .env
- `Connection refused` on port 443 -- Confirm ESXi web access is enabled: ESXi > Manage > Services > TSM-SSH / HTTPS
- `VM 'name' not found` -- VM names are case-sensitive; use `esxi_list_vms` to confirm exact name
- Tool not appearing in Claude -- Restart Claude Desktop after config changes
