"""
==============================================================================
Script Name:    connection.py
Synopsis:       pyVmomi connection manager for the ESXi MCP server.
Description:    Provides a singleton ConnectionManager that loads ESXi host
                definitions from config/hosts.yaml, resolves credentials from
                environment variables, and maintains a pool of authenticated
                pyVmomi ServiceInstance objects — one per named host.
                Connections are lazily created on first use and automatically
                re-established when a stale session is detected. This design
                means individual tool calls never need to manage their own
                sessions; they simply call ConnectionManager.instance()
                .get_connection(host_name) and get a live, verified connection.

Parameters /
Setup:          config/hosts.yaml must exist relative to the project root, or
                the ESXI_MCP_CONFIG environment variable must point to an
                alternate YAML file. Host passwords are NOT stored in the YAML;
                they are resolved from environment variables using the pattern:
                  ESXI_{HOSTNAME_UPPERCASED_UNDERSCORED}_PASSWORD
                Example: host "ESX-01" → ESXI_ESX_01_PASSWORD
                A .env file in the project root is loaded automatically via
                python-dotenv so passwords never need to be set in shell
                sessions manually.

Change Log:
  2026-06-23  Richard Smith   Initial version — lazy connect, auto-reconnect,
                              singleton pattern, YAML config + env-var
                              credential resolution, SSL bypass for self-signed
                              ESXi certificates.
==============================================================================
"""

from __future__ import annotations

# ==============================================================================
# IMPORTS
# ==============================================================================
import os
import ssl
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim

# Load .env from the project root before anything else reads os.environ.
# This must happen at import time so that credential lookups in _load_config
# always see the populated environment even if no shell export was done.
load_dotenv()

logger = logging.getLogger(__name__)


# ==============================================================================
# VARIABLES — configuration constants and defaults
# ==============================================================================

# Default TCP port for the ESXi HTTPS API.  ESXi 6.x always uses 443; only
# override here if you're running a non-standard port or a proxy in front.
DEFAULT_ESX_PORT: int = 443

# Default ESXi login account.  "root" is the built-in administrator on
# standalone ESXi hosts.  If a least-privilege account is created later,
# update hosts.yaml rather than changing this default.
DEFAULT_ESX_USER: str = "root"

# ESXi ships with a self-signed TLS certificate.  By default we skip
# verification to avoid import-time failures on machines that haven't imported
# the ESXi CA cert.  Set to True only if you've deployed a trusted cert.
DEFAULT_SSL_VERIFY: bool = False

# Liveness probe — CurrentTime() is the cheapest possible round-trip to
# confirm a ServiceInstance session is still authenticated.  We call it
# before every content retrieval to catch stale sessions before tools fail.
LIVENESS_PROBE_METHOD: str = "CurrentTime"


# ==============================================================================
# DATA CLASSES — typed containers for host config and active connections
# ==============================================================================

@dataclass
class HostConfig:
    """
    Immutable configuration record for a single ESXi host.
    Populated once from YAML + env at startup; never mutated at runtime.
    Keeping config separate from the live connection object means we can
    reconnect from scratch without reloading the config file.
    """
    name: str           # Human-readable label matching the YAML key
    host: str           # IP address or FQDN
    port: int           # HTTPS API port (almost always 443)
    username: str       # ESXi login account
    password: str       # Resolved from env var, never from YAML
    ssl_verify: bool    # Whether to validate the TLS certificate chain


@dataclass
class HostConnection:
    """
    Wraps a HostConfig with a live pyVmomi ServiceInstance.
    The ServiceInstance (si) is None until the first connect() call.
    Tools never touch si directly — they call get_content() which
    handles liveness checking and reconnection transparently.
    """
    config: HostConfig
    # repr=False keeps the ServiceInstance object out of log output so we
    # don't accidentally dump session tokens into log files.
    si: Optional[object] = field(default=None, repr=False)

    # --------------------------------------------------------------------------
    # connect — establish a fresh authenticated session to the ESXi host
    # --------------------------------------------------------------------------
    def connect(self) -> None:
        """
        Open a new pyVmomi session to the ESXi HTTPS API.
        Creates a permissive SSL context when ssl_verify is False — this is
        required for ESXi's default self-signed certificate.  The risk is
        accepted deliberately on an internal management network where MITM is
        not a realistic threat; add a trusted cert to the ESXi host and set
        ssl_verify: true in hosts.yaml to harden this.
        """
        ctx = None
        if not self.config.ssl_verify:
            # TLS_CLIENT is the correct modern protocol constant; PROTOCOL_TLS
            # is deprecated.  check_hostname must be disabled before
            # CERT_NONE can be set — Python enforces that order.
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        self.si = SmartConnect(
            host=self.config.host,
            user=self.config.username,
            pwd=self.config.password,
            port=self.config.port,
            sslContext=ctx,
        )
        logger.info(
            "Connected to ESXi host '%s' at %s:%s",
            self.config.name,
            self.config.host,
            self.config.port,
        )

    # --------------------------------------------------------------------------
    # disconnect — cleanly terminate the API session
    # --------------------------------------------------------------------------
    def disconnect(self) -> None:
        """
        Release the pyVmomi session.  Swallows errors because disconnect is
        typically called during shutdown where raising would suppress other
        cleanup.  Always nulls out si so the object is safe to reconnect.
        """
        if self.si:
            try:
                Disconnect(self.si)
            except Exception:
                # A failed disconnect is non-fatal — the session will time out
                # on the server side regardless.
                pass
            self.si = None

    # --------------------------------------------------------------------------
    # get_content — return a live ServiceInstanceContent, reconnecting if stale
    # --------------------------------------------------------------------------
    def get_content(self) -> vim.ServiceInstanceContent:
        """
        The primary entry point for all tool code.  Returns the root
        ServiceInstanceContent, which is the gateway to every pyVmomi
        object model — VMs, hosts, datastores, networks, tasks, etc.

        Performs a lightweight liveness probe before returning.  ESXi sessions
        idle-timeout after 30 minutes by default; the probe catches that case
        and reconnects transparently so tools never see an authentication error.
        """
        # Lazy initial connection — connect on first use rather than at
        # startup, so the server can start even if a host is temporarily down.
        if self.si is None:
            self.connect()

        try:
            # CurrentTime() is the cheapest possible API call — it returns a
            # single datetime and proves the session is still authenticated.
            self.si.CurrentTime()
        except Exception:
            # Session is stale (idle timeout, network blip, ESXi reboot).
            # Null out si and reconnect; if reconnect fails it will raise and
            # propagate to the tool, which returns an error to Claude.
            logger.warning(
                "Session to '%s' is stale — reconnecting.", self.config.name
            )
            self.si = None
            self.connect()

        return self.si.RetrieveContent()


# ==============================================================================
# FUNCTIONS — shared helpers
# ==============================================================================

def _env_password_key(host_name: str) -> str:
    """
    Derive the environment variable name for a host's password from its
    config label.  Hyphens and spaces become underscores, all uppercase.
    Example: "ESX-01" → "ESXI_ESX_01_PASSWORD"

    Using a deterministic naming convention means operators can see exactly
    which env var to set for any given host without consulting documentation.
    """
    safe_name = host_name.upper().replace("-", "_").replace(" ", "_")
    return f"ESXI_{safe_name}_PASSWORD"


# ==============================================================================
# MAIN — ConnectionManager singleton
# ==============================================================================

class ConnectionManager:
    """
    Singleton that owns all HostConnection objects for the lifetime of the
    MCP server process.  A singleton is appropriate here because:
      - The MCP server is a single-process stdio server.
      - We want connection reuse across tool calls (reconnect, not reconnect
        on every tool invocation).
      - Config is loaded once at startup; runtime state is managed centrally.

    Usage:
        mgr = ConnectionManager.instance()          # get or create singleton
        conn = mgr.get_connection("SYM-ESX-01")    # get live HostConnection
        content = conn.get_content()               # get ServiceInstanceContent
    """

    _instance: Optional[ConnectionManager] = None

    def __init__(self, config_path: str | Path | None = None):
        self._connections: dict[str, HostConnection] = {}
        self._default_host: str = ""
        self._load_config(config_path)

    # --------------------------------------------------------------------------
    # _load_config — parse YAML and build HostConnection objects
    # --------------------------------------------------------------------------
    def _load_config(self, config_path: str | Path | None) -> None:
        """
        Load hosts.yaml and construct one HostConnection per entry.
        Passwords are intentionally not stored in YAML — the YAML file may
        end up in version control, so credentials must come from env vars.
        The env var key is derived deterministically from the host's label
        so there is no separate credential mapping to maintain.
        """
        # Resolve config file path: explicit argument → env override → default
        if config_path is None:
            config_path = os.environ.get(
                "ESXI_MCP_CONFIG",
                # Default: config/hosts.yaml relative to the project root,
                # which is three levels above this module file.
                Path(__file__).parent.parent.parent / "config" / "hosts.yaml",
            )

        with open(config_path) as fh:
            raw = yaml.safe_load(fh)

        self._default_host = raw.get("default_host", "")

        for name, cfg in raw.get("hosts", {}).items():
            # Password precedence: inline YAML (dev/test only) → env var.
            # In practice the YAML entry should never contain a password;
            # the inline option exists only for local testing convenience.
            password = cfg.get("password") or os.environ.get(
                _env_password_key(name), ""
            )

            host_cfg = HostConfig(
                name=name,
                host=cfg["host"],
                port=cfg.get("port", DEFAULT_ESX_PORT),
                username=cfg.get("username", DEFAULT_ESX_USER),
                password=password,
                ssl_verify=cfg.get("ssl_verify", DEFAULT_SSL_VERIFY),
            )
            self._connections[name] = HostConnection(config=host_cfg)

        logger.info("Loaded %d host(s) from config: %s", len(self._connections),
                    ", ".join(self._connections))

    # --------------------------------------------------------------------------
    # Public accessors
    # --------------------------------------------------------------------------

    def host_names(self) -> list[str]:
        """Return the list of configured host labels in definition order."""
        return list(self._connections.keys())

    def default_host(self) -> str:
        """
        Return the default host label.  Falls back to the first defined host
        if default_host is not set in YAML — ensures tools that omit host_name
        always resolve to something rather than failing with a KeyError.
        """
        return self._default_host or (
            self.host_names()[0] if self._connections else ""
        )

    def get_connection(self, host_name: str | None = None) -> HostConnection:
        """
        Return the HostConnection for the given host label, using the default
        host if host_name is None or empty.  Connects lazily on first call.
        Raises ValueError with a helpful message if the name isn't in config,
        so tool error responses tell the user exactly what went wrong.
        """
        name = host_name or self.default_host()
        if name not in self._connections:
            raise ValueError(
                f"Unknown host '{name}'. "
                f"Configured hosts: {', '.join(self.host_names())}"
            )
        conn = self._connections[name]
        # Trigger lazy connect now if this is the first access for this host.
        if conn.si is None:
            conn.connect()
        return conn

    def disconnect_all(self) -> None:
        """
        Clean up all active sessions.  Called on server shutdown to release
        ESXi session slots — ESXi has a limited session pool (default 256)
        and leaked sessions can block new logins if the server crashes and
        restarts frequently.
        """
        for conn in self._connections.values():
            conn.disconnect()

    @classmethod
    def instance(cls, config_path: str | Path | None = None) -> "ConnectionManager":
        """
        Return the singleton instance, creating it on first call.
        Passing config_path on subsequent calls has no effect — the config
        is loaded once and cached.  If a fresh load is ever needed (e.g.,
        in tests), set _instance = None before calling instance() again.
        """
        if cls._instance is None:
            cls._instance = cls(config_path)
        return cls._instance
