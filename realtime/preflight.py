"""Pre-race deployment checks and test-group workflow state."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable


def _event_key(event: Any) -> tuple[str, str, str]:
    return (
        str(getattr(event, "race_id", "") or "").strip(),
        str(getattr(event, "stage_id", "") or "").strip(),
        str(getattr(event, "event_id", "") or "").strip(),
    )


@dataclass(frozen=True, slots=True)
class PreflightRun:
    group_id: str
    started_at_ms: int
    baseline_event_keys: frozenset[tuple[str, str, str]]
    started_receive_sequence: int = 0
    require_regular: bool = True
    require_high_speed: bool = True
    event_id: str = ""
    bib: str = ""
    race_id: str = ""
    stage_id: str = ""
    passage_received: bool = False
    regular_ready: bool = False
    high_speed_ready: bool = False

    @classmethod
    def start(
        cls,
        events: Iterable[Any],
        *,
        started_at_ms: int,
        require_regular: bool,
        require_high_speed: bool,
        started_receive_sequence: int = 0,
    ) -> "PreflightRun":
        return cls(
            group_id="",
            started_at_ms=max(0, int(started_at_ms)),
            baseline_event_keys=frozenset(
                _event_key(event)
                for event in events
                if str(getattr(event, "event_id", "")).strip()
            ),
            started_receive_sequence=max(0, int(started_receive_sequence)),
            require_regular=bool(require_regular),
            require_high_speed=bool(require_high_speed),
        )

    @property
    def baseline_event_ids(self) -> frozenset[str]:
        return frozenset(event_id for _race_id, _stage_id, event_id in self.baseline_event_keys)

    def observe(
        self,
        events: Iterable[Any],
        *,
        received_order: Mapping[tuple[str, str, str], int] | None = None,
    ) -> "PreflightRun":
        if self.event_id:
            return self
        candidates = []
        for store_index, event in enumerate(events):
            event_key = _event_key(event)
            event_id = event_key[2]
            if (
                not event_id
                or event_key in self.baseline_event_keys
                or not bool(getattr(event, "is_active", True))
            ):
                continue
            if received_order is not None:
                local_sequence = int(received_order.get(event_key, 0) or 0)
                if local_sequence <= self.started_receive_sequence:
                    continue
            else:
                local_sequence = store_index + 1
            candidates.append((local_sequence, store_index, event))
        if not candidates:
            return self
        _local_sequence, _store_index, event = min(
            candidates,
            key=lambda item: (item[0], item[1], _event_key(item[2])),
        )
        return replace(
            self,
            group_id=str(getattr(event, "group_id", "") or "").strip(),
            event_id=str(event.event_id),
            bib=str(getattr(event, "bib", "") or getattr(event, "chip_id", "")),
            race_id=str(getattr(event, "race_id", "")),
            stage_id=str(getattr(event, "stage_id", "")),
            passage_received=True,
        )

    def with_evidence(
        self,
        *,
        regular_ready: bool,
        high_speed_ready: bool,
    ) -> "PreflightRun":
        if not self.event_id:
            return self
        return replace(
            self,
            regular_ready=bool(regular_ready),
            high_speed_ready=bool(high_speed_ready),
        )

    @property
    def passed(self) -> bool:
        return bool(
            self.passage_received
            and (self.regular_ready or not self.require_regular)
            and (self.high_speed_ready or not self.require_high_speed)
        )

    @property
    def status(self) -> str:
        if self.passed:
            return "passed"
        if not self.passage_received:
            return "waiting_passage"
        return "waiting_evidence"


class PreflightJournal:
    """Durably records test passages so they remain separate after restart."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def event_ids(self) -> frozenset[str]:
        return frozenset(event_id for _race_id, _stage_id, event_id in self.event_keys())

    def event_keys(self) -> frozenset[tuple[str, str, str]]:
        return frozenset(self._active_entries())

    def latest_entry(self) -> dict[str, Any] | None:
        entries = self._active_entries()
        if not entries:
            return None
        return max(
            entries.values(),
            key=lambda payload: (
                int(payload.get("recorded_at_ms") or 0),
                str(payload.get("event_id") or ""),
            ),
        )

    def _active_entries(self) -> dict[tuple[str, str, str], dict[str, Any]]:
        entries: dict[tuple[str, str, str], dict[str, Any]] = {}
        if not self.path.exists():
            return entries
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return entries
        for line in lines:
            try:
                payload = json.loads(line)
            except (TypeError, ValueError):
                continue
            event_id = str(payload.get("event_id") or "").strip()
            if not event_id:
                continue
            event_key = (
                str(payload.get("race_id") or "").strip(),
                str(payload.get("stage_id") or "").strip(),
                event_id,
            )
            if str(payload.get("action") or "isolate") == "restore":
                entries.pop(event_key, None)
            else:
                entries[event_key] = payload
        return entries

    def append(self, run: PreflightRun, *, recorded_at_ms: int) -> None:
        if not run.event_id:
            return
        payload = {
            "schema_version": 2,
            "action": "isolate",
            "recorded_at_ms": max(0, int(recorded_at_ms)),
            "started_at_ms": run.started_at_ms,
            "race_id": run.race_id,
            "stage_id": run.stage_id,
            "group_id": run.group_id,
            "event_id": run.event_id,
            "bib": run.bib,
            "regular_ready": run.regular_ready,
            "high_speed_ready": run.high_speed_ready,
            "status": run.status,
        }
        self._append_payload(payload)

    def restore(
        self,
        event_key: tuple[str, str, str],
        *,
        recorded_at_ms: int,
    ) -> None:
        race_id, stage_id, event_id = event_key
        if not str(event_id).strip():
            return
        self._append_payload(
            {
                "schema_version": 2,
                "action": "restore",
                "recorded_at_ms": max(0, int(recorded_at_ms)),
                "race_id": str(race_id).strip(),
                "stage_id": str(stage_id).strip(),
                "event_id": str(event_id).strip(),
            }
        )

    def _append_payload(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _windows_ipv4_addresses() -> tuple[str, ...]:
    if sys.platform != "win32":
        return ()
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return ()

    class SocketAddress(ctypes.Structure):
        _fields_ = [
            ("sockaddr", ctypes.c_void_p),
            ("length", ctypes.c_int),
        ]

    class UnicastAddress(ctypes.Structure):
        pass

    UnicastAddress._fields_ = [
        ("length", wintypes.ULONG),
        ("flags", wintypes.DWORD),
        ("next", ctypes.POINTER(UnicastAddress)),
        ("address", SocketAddress),
    ]

    class AdapterAddress(ctypes.Structure):
        pass

    AdapterAddress._fields_ = [
        ("length", wintypes.ULONG),
        ("if_index", wintypes.DWORD),
        ("next", ctypes.POINTER(AdapterAddress)),
        ("adapter_name", ctypes.c_char_p),
        ("first_unicast", ctypes.POINTER(UnicastAddress)),
        ("first_anycast", ctypes.c_void_p),
        ("first_multicast", ctypes.c_void_p),
        ("first_dns_server", ctypes.c_void_p),
        ("dns_suffix", wintypes.LPWSTR),
        ("description", wintypes.LPWSTR),
        ("friendly_name", wintypes.LPWSTR),
        ("physical_address", ctypes.c_ubyte * 8),
        ("physical_address_length", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("mtu", wintypes.DWORD),
        ("if_type", wintypes.DWORD),
        ("oper_status", wintypes.ULONG),
    ]

    get_addresses = ctypes.windll.iphlpapi.GetAdaptersAddresses
    get_addresses.argtypes = [
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        ctypes.POINTER(AdapterAddress),
        ctypes.POINTER(wintypes.ULONG),
    ]
    get_addresses.restype = wintypes.ULONG
    size = wintypes.ULONG(16 * 1024)
    while True:
        buffer = ctypes.create_string_buffer(size.value)
        first = ctypes.cast(buffer, ctypes.POINTER(AdapterAddress))
        result = get_addresses(
            socket.AF_INET,
            0x02 | 0x04 | 0x08,
            None,
            first,
            ctypes.byref(size),
        )
        if result == 111:
            continue
        if result != 0:
            return ()
        break

    addresses = set()
    adapter = first
    while adapter:
        current = adapter.contents
        if current.oper_status == 1:
            unicast = current.first_unicast
            while unicast:
                socket_address = unicast.contents.address
                if socket_address.sockaddr and socket_address.length >= 8:
                    raw = ctypes.string_at(socket_address.sockaddr, socket_address.length)
                    if int.from_bytes(raw[:2], byteorder=sys.byteorder) == socket.AF_INET:
                        addresses.add(socket.inet_ntoa(raw[4:8]))
                unicast = unicast.contents.next
        adapter = current.next
    return tuple(addresses)


def local_ipv4_addresses() -> tuple[str, ...]:
    """Return non-loopback IPv4 addresses without changing adapter state."""

    addresses = set(_windows_ipv4_addresses())
    try:
        entries = socket.getaddrinfo(
            socket.gethostname(),
            None,
            family=socket.AF_INET,
            type=socket.SOCK_DGRAM,
        )
    except OSError:
        entries = ()
    for entry in entries:
        address = str(entry[4][0])
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (
            parsed.version == 4
            and not parsed.is_loopback
            and not parsed.is_unspecified
            and not parsed.is_multicast
        ):
            addresses.add(str(parsed))
    usable = []
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (
            parsed.version == 4
            and not parsed.is_loopback
            and not parsed.is_unspecified
            and not parsed.is_link_local
            and not parsed.is_multicast
        ):
            usable.append(str(parsed))
    return tuple(sorted(set(usable), key=ipaddress.ip_address))


def validate_event_network(addresses: Iterable[str], *, prefix_length: int = 24) -> None:
    """Require valid, unique IPv4 addresses on one shared event subnet."""

    try:
        resolved = [ipaddress.ip_address(str(value).strip()) for value in addresses]
    except ValueError as error:
        raise ValueError("赛事网络地址必须是IPv4地址") from error
    if not resolved or any(value.version != 4 for value in resolved):
        raise ValueError("赛事网络地址必须是IPv4地址")
    if len(resolved) != len(set(resolved)):
        raise ValueError("赛事网络地址不能重复")
    for value in resolved:
        if (
            value.is_unspecified
            or value.is_loopback
            or value.is_link_local
            or value.is_multicast
            or value.is_reserved
        ):
            raise ValueError(f"{value}不是可用的赛事设备主机地址")
    network = ipaddress.ip_network(f"{resolved[0]}/{int(prefix_length)}", strict=False)
    if any(value not in network for value in resolved[1:]):
        raise ValueError(f"赛事设备必须位于同一/{int(prefix_length)}网段")
    for value in resolved:
        if (
            value in {network.network_address, network.broadcast_address}
        ):
            raise ValueError(f"{value}不是可用的赛事设备主机地址")


__all__ = [
    "PreflightJournal",
    "PreflightRun",
    "local_ipv4_addresses",
    "validate_event_network",
]
