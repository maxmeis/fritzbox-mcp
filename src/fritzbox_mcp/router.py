"""Validated, serialized access to the router's advertised TR-064 API."""

from __future__ import annotations

import ipaddress
import logging
import math
import os
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class RouterError(Exception):
    """Safe error suitable for returning to an MCP client."""


_SECRET_KEY = re.compile(
    r"(password|passwd|secret|token|session|credential|private.?key|pre.?shared.?key|key.?passphrase|wep.?key|security.?key|api.?key|(^|new|[_-])sid($|[_-]))",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_MAC = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
_INT_TYPES = {
    "ui1": (0, 255),
    "ui2": (0, 65535),
    "ui4": (0, 4294967295),
    "ui8": (0, 18446744073709551615),
    "i1": (-128, 127),
    "i2": (-32768, 32767),
    "i4": (-2147483648, 2147483647),
    "i8": (-9223372036854775808, 9223372036854775807),
    "int": (-2147483648, 2147483647),
}


class _NoTransportDebug(logging.Filter):
    """The upstream debug log includes raw SOAP envelopes and responses."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.INFO


_NO_TRANSPORT_DEBUG = _NoTransportDebug()


def redact(value: Any, key: str = "") -> Any:
    """Remove common credentials and URL query fragments from router results."""
    if _SECRET_KEY.search(key):
        return "[redacted]"
    if isinstance(value, Mapping):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _URL.sub(_redact_url, value)
    return value


def _redact_url(match: re.Match[str]) -> str:
    original = match.group(0)
    try:
        parts = urlsplit(original)
        if parts.query or parts.fragment or "@" in parts.netloc:
            host = parts.netloc.rsplit("@", 1)[-1]
            return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        return "[redacted URL]"
    return original


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    if value.lower() in ("true", "1", "yes", "on"):
        return True
    if value.lower() in ("false", "0", "no", "off"):
        return False
    raise RouterError(f"{name} must be true or false")


class RouterClient:
    """Lazy router client. The factory can be injected for offline tests."""

    def __init__(self, factory: Callable[..., Any] | None = None):
        self._factory = factory
        self._connection: Any | None = None
        self._lock = threading.RLock()
        self._auth_token: str | None = None

    def close(self) -> None:
        """Release an opened HTTP session and discard the 2FA context."""
        with self._lock:
            self._auth_token = None
            connection, self._connection = self._connection, None
            if connection is not None:
                session = getattr(connection, "session", None)
                if session is not None:
                    session.close()

    def _private_soap_call(
        self, connection: Any, service: Any, action: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        """Send a sensitive SOAP request without changing library global state.

        fritzconnection 1.x has no per-request SOAP header hook. Its Soaper
        helpers still construct and parse the request; we add only the header.
        Its normal execute method logs request envelopes at DEBUG, so this
        path deliberately avoids that method to keep the token out of logs.
        """
        from fritzconnection.core.soaper import (
            preprocess_arguments,
            raise_fritzconnection_error,
        )

        soaper = connection.soaper
        if getattr(soaper, "session", None) is None:
            raise RouterError("Router connection cannot send authenticated requests")
        encoded = preprocess_arguments(values)
        arguments = "".join(
            soaper.argument_template.format(name=name, value=value)
            for name, value in encoded.items()
        )
        body = soaper.get_body(service, action, arguments)
        header = ""
        if self._auth_token:
            header = (
                '<s:Header><avm:token xmlns:avm="avm.de" s:mustUnderstand="1">'
                f"{self._auth_token}</avm:token></s:Header>"
            )
        envelope = soaper.envelope.format(body=header + body).encode("utf-8")
        headers = soaper.headers.copy()
        headers["soapaction"] = f"{service.serviceType}#{action}"
        url = f"{soaper.address}:{soaper.port}{service.controlURL}"
        with soaper.session.post(
            url, data=envelope, headers=headers, timeout=soaper.timeout
        ) as response:
            if response.status_code != 200:
                raise_fritzconnection_error(response)
            return soaper.parse_response(response, service, action)

    def _connect(self) -> Any:
        if self._connection is None:
            if self._factory is None:
                from fritzconnection import FritzConnection
                from fritzconnection.core.logger import fritzlogger

                if _NO_TRANSPORT_DEBUG not in fritzlogger.filters:
                    fritzlogger.addFilter(_NO_TRANSPORT_DEBUG)

                factory = FritzConnection
            else:
                factory = self._factory
            password = os.getenv("FRITZ_PASSWORD")
            password_file = os.getenv("FRITZ_PASSWORD_FILE")
            if password and password_file:
                raise RouterError("Set either FRITZ_PASSWORD or FRITZ_PASSWORD_FILE")
            if password_file:
                try:
                    password = (
                        Path(password_file).read_text(encoding="utf-8").rstrip("\r\n")
                    )
                except OSError:
                    raise RouterError("Cannot read FRITZ_PASSWORD_FILE") from None
            try:
                timeout = float(os.getenv("FRITZ_TIMEOUT", "10"))
                if not 0 < timeout <= 120:
                    raise ValueError
            except ValueError as exc:
                raise RouterError(
                    "FRITZ_TIMEOUT must be between 0 and 120 seconds"
                ) from exc
            try:
                self._connection = factory(
                    address=os.getenv("FRITZ_HOST", "fritz.box"),
                    user=os.getenv("FRITZ_USERNAME"),
                    password=password or "",
                    timeout=timeout,
                    use_tls=_env_bool("FRITZ_TLS", True),
                    redact_debug_log=True,
                )
            except RouterError:
                raise
            except Exception:  # noqa: BLE001 - Keep raw library errors out of MCP responses.
                raise RouterError(
                    "Could not connect to FRITZ!Box; check host, TLS, and credentials"
                ) from None
        return self._connection

    def _service(self, connection: Any, name: str) -> Any:
        service = connection.services.get(name)
        if service is None:
            raise RouterError("Service is not advertised by this router")
        return service

    def _action(self, service: Any, name: str) -> Any:
        action = service.actions.get(name)
        if action is None:
            raise RouterError("Action is not advertised by this router")
        return action

    @staticmethod
    def _argument_type(service: Any, argument: Any) -> str:
        variable = service.state_variables.get(argument.relatedStateVariable)
        return str(variable.dataType).lower() if variable is not None else "string"

    @staticmethod
    def _coerce(value: Any, kind: str, name: str) -> Any:
        if kind == "boolean":
            if type(value) is bool:
                return value
            if type(value) is int and value in (0, 1):
                return bool(value)
            raise RouterError(f"{name} must be a boolean")
        if kind in _INT_TYPES:
            lower, upper = _INT_TYPES[kind]
            if type(value) is not int or not lower <= value <= upper:
                raise RouterError(f"{name} must be an integer from {lower} to {upper}")
            return value
        if kind in {"r4", "r8", "number", "float", "fixed.14.4"}:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise RouterError(f"{name} must be a finite number")
            return value
        if not isinstance(value, str):
            raise RouterError(f"{name} must be a string")
        return value

    def action(
        self, service: str, action: str, arguments: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Call any advertised action after exact input schema validation."""
        if not isinstance(service, str) or not isinstance(action, str):
            raise RouterError("Service and action must be strings")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, Mapping):
            raise RouterError("arguments must be an object")
        with self._lock:
            connection = self._connect()
            svc = self._service(connection, service)
            act = self._action(svc, action)
            inputs = {
                name: arg
                for name, arg in act.arguments.items()
                if arg.direction == "in"
            }
            missing = set(inputs) - set(arguments)
            extra = set(arguments) - set(inputs)
            if missing or extra:
                parts = []
                if missing:
                    parts.append("missing " + ", ".join(sorted(missing)))
                if extra:
                    parts.append("unknown " + ", ".join(sorted(str(x) for x in extra)))
                raise RouterError("Invalid input arguments: " + "; ".join(parts))
            values = {}
            for name, arg in inputs.items():
                kind = self._argument_type(svc, arg)
                value = self._coerce(arguments[name], kind, name)
                variable = svc.state_variables.get(arg.relatedStateVariable)
                allowed = list(getattr(variable, "allowed_values", []) or [])
                if (
                    allowed
                    and str(int(value) if kind == "boolean" else value) not in allowed
                ):
                    raise RouterError(
                        f"{name} must be one of the router-advertised values: {', '.join(allowed)}"
                    )
                value_range = getattr(variable, "allowedValueRange", None)
                if kind in _INT_TYPES and value_range is not None:
                    minimum = getattr(value_range, "minimum", None)
                    maximum = getattr(value_range, "maximum", None)
                    if minimum is not None and value < int(minimum):
                        raise RouterError(
                            f"{name} is below the router-advertised minimum"
                        )
                    if maximum is not None and value > int(maximum):
                        raise RouterError(
                            f"{name} is above the router-advertised maximum"
                        )
                values[name] = value
            auth_action = (
                service == "X_AVM-DE_Auth1"
                and action == "SetConfig"
                and values.get("NewAction") in {"start", "stop"}
            )
            starting = auth_action and values["NewAction"] == "start"
            stopping = auth_action and values["NewAction"] == "stop"
            if starting:
                self._auth_token = None
            try:
                if hasattr(connection, "soaper"):
                    result = self._private_soap_call(connection, svc, action, values)
                else:
                    result = connection.call_action(service, action, arguments=values)
            except Exception:  # noqa: BLE001 - SOAP failures may contain credentials.
                self._auth_token = None
                raise RouterError(
                    "Router rejected the action; check permissions, inputs, and router state"
                ) from None
            if starting:
                token = (result or {}).get("NewToken")
                if token:
                    try:
                        self._auth_token = str(uuid.UUID(str(token)))
                    except (TypeError, ValueError, AttributeError):
                        raise RouterError(
                            "Router returned an invalid 2FA token"
                        ) from None
            elif stopping:
                self._auth_token = None
            elif service == "X_AVM-DE_Auth1" and action == "GetState":
                if (result or {}).get("NewState") in {
                    "disabled",
                    "stopped",
                    "blocked",
                    "failure",
                }:
                    self._auth_token = None
            return redact(result or {})

    def schema(
        self, service: str | None = None, action: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            connection = self._connect()
            if service is None:
                if action is not None:
                    raise RouterError("Specify a service with action")
                return {
                    name: sorted(svc.actions)
                    for name, svc in sorted(connection.services.items())
                }
            svc = self._service(connection, service)
            if action is None:
                return {"service": service, "actions": sorted(svc.actions)}
            act = self._action(svc, action)
            args = []
            for name, arg in act.arguments.items():
                variable = svc.state_variables.get(arg.relatedStateVariable)
                args.append(
                    {
                        "name": name,
                        "direction": arg.direction,
                        "type": self._argument_type(svc, arg),
                        "allowed_values": list(
                            getattr(variable, "allowed_values", []) or []
                        ),
                    }
                )
            return {"service": service, "action": action, "arguments": args}

    def capabilities(self) -> dict[str, Any]:
        with self._lock:
            services = self._connect().services
            has = lambda service, action: (
                service in services and action in services[service].actions
            )
            return {
                "router_actions": "All advertised TR-064 actions available via call_action",
                "device_names": has("Hosts1", "X_AVM-DE_SetHostNameByMACAddress"),
                "friendly_names": has("Hosts1", "X_AVM-DE_SetFriendlyNameByMAC"),
                "device_wan_blocking": has(
                    "X_AVM-DE_HostFilter1", "DisallowWANAccessByIP"
                ),
                "lan_dns_read": has("LANHostConfigManagement1", "GetDNSServers")
                or has("LANHostConfigManagement1", "GetDNSServer"),
                "wan_dns_read": has("WANIPConnection1", "X_GetDNSServers")
                or has("WANPPPConnection1", "X_GetDNSServers"),
                "dns_filter_rules": "No documented TR-064 action; use router UI if available",
                "dns_server_write": "No documented supported TR-064 setter; use router UI if available",
            }

    def router_info(self) -> dict[str, Any]:
        return self.action("DeviceInfo1", "GetInfo")

    def list_devices(self, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise RouterError(
                "offset must be nonnegative and limit must be 1 through 100"
            )
        count = self.action("Hosts1", "GetHostNumberOfEntries").get(
            "NewHostNumberOfEntries", 0
        )
        try:
            count = int(count)
        except (TypeError, ValueError) as exc:
            raise RouterError("Router returned an invalid device count") from exc
        entries = []
        for index in range(offset, min(count, offset + limit)):
            entries.append(
                self.action("Hosts1", "GetGenericHostEntry", {"NewIndex": index})
            )
        return {"total": count, "offset": offset, "limit": limit, "devices": entries}

    def get_device(self, mac: str) -> dict[str, Any]:
        return self.action(
            "Hosts1", "GetSpecificHostEntry", {"NewMACAddress": self._mac(mac)}
        )

    @staticmethod
    def _mac(mac: str) -> str:
        if not isinstance(mac, str) or not _MAC.fullmatch(mac):
            raise RouterError("MAC address must have six colon-separated hex octets")
        return mac.upper()

    def rename_device(self, mac: str, name: str) -> dict[str, Any]:
        if not isinstance(name, str) or not 1 <= len(name) <= 64:
            raise RouterError("Device hostname must be 1 through 64 characters")
        return self.action(
            "Hosts1",
            "X_AVM-DE_SetHostNameByMACAddress",
            {"NewMACAddress": self._mac(mac), "NewHostName": name},
        )

    def set_friendly_name(self, mac: str, name: str) -> dict[str, Any]:
        if not isinstance(name, str) or not 1 <= len(name) <= 64:
            raise RouterError("Friendly name must be 1 through 64 characters")
        return self.action(
            "Hosts1",
            "X_AVM-DE_SetFriendlyNameByMAC",
            {"NewMACAddress": self._mac(mac), "NewX_AVM-DE_FriendlyName": name},
        )

    @staticmethod
    def _ipv4(ip: str) -> str:
        try:
            address = ipaddress.ip_address(ip)
            if address.version != 4:
                raise ValueError
            return str(address)
        except ValueError as exc:
            raise RouterError("A valid IPv4 address is required") from exc

    def get_device_blocking(self, ip: str) -> dict[str, Any]:
        return self.action(
            "X_AVM-DE_HostFilter1",
            "GetWANAccessByIP",
            {"NewIPv4Address": self._ipv4(ip)},
        )

    def set_device_blocking(self, ip: str, blocked: bool) -> dict[str, Any]:
        if type(blocked) is not bool:
            raise RouterError("blocked must be a boolean")
        return self.action(
            "X_AVM-DE_HostFilter1",
            "DisallowWANAccessByIP",
            {"NewIPv4Address": self._ipv4(ip), "NewDisallow": blocked},
        )

    def wifi_info(self, service: str = "WLANConfiguration1") -> dict[str, Any]:
        if not re.fullmatch(r"WLANConfiguration[1-9][0-9]*", service):
            raise RouterError("Service must be a WLANConfiguration instance")
        return self.action(service, "GetInfo")

    def set_wifi_enabled(self, service: str, enabled: bool) -> dict[str, Any]:
        if not re.fullmatch(r"WLANConfiguration[1-9][0-9]*", service):
            raise RouterError("Service must be a WLANConfiguration instance")
        if type(enabled) is not bool:
            raise RouterError("enabled must be a boolean")
        return self.action(service, "SetEnable", {"NewEnable": enabled})

    def get_lan_dns(self) -> dict[str, Any]:
        with self._lock:
            service = self._service(self._connect(), "LANHostConfigManagement1")
            for action in ("GetDNSServers", "GetDNSServer"):
                if action in service.actions:
                    return self.action("LANHostConfigManagement1", action)
        raise RouterError("LAN DNS read action is not advertised by this router")

    def get_wan_dns(self, service: str | None = None) -> dict[str, Any]:
        if service is not None and service not in {
            "WANIPConnection1",
            "WANPPPConnection1",
        }:
            raise RouterError("Service must be WANIPConnection1 or WANPPPConnection1")
        with self._lock:
            services = self._connect().services
            candidates = (
                (service,) if service else ("WANIPConnection1", "WANPPPConnection1")
            )
            for name in candidates:
                if name in services and "X_GetDNSServers" in services[name].actions:
                    return self.action(name, "X_GetDNSServers")
        raise RouterError("WAN DNS read action is not advertised by this router")

    def get_2fa_info(self) -> dict[str, Any]:
        return self.action("X_AVM-DE_Auth1", "GetInfo")

    def get_2fa_state(self) -> dict[str, Any]:
        return self.action("X_AVM-DE_Auth1", "GetState")

    def start_2fa(self) -> dict[str, Any]:
        return self.action("X_AVM-DE_Auth1", "SetConfig", {"NewAction": "start"})

    def stop_2fa(self) -> dict[str, Any]:
        return self.action("X_AVM-DE_Auth1", "SetConfig", {"NewAction": "stop"})
