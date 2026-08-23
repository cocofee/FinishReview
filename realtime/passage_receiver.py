"""Durable CycleRace passage-event receiver for FinishReview."""

from __future__ import annotations

import hmac
import json
import logging
import os
import socket
import threading
from dataclasses import asdict, dataclass
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

try:
    from .race_metadata import (
        ACK_MESSAGE_TYPE as METADATA_ACK_MESSAGE_TYPE,
        MESSAGE_TYPE as METADATA_MESSAGE_TYPE,
        RaceMetadata,
        RaceMetadataConflictError,
        RaceMetadataError,
        RaceMetadataStore,
    )
except ImportError:
    from race_metadata import (
        ACK_MESSAGE_TYPE as METADATA_ACK_MESSAGE_TYPE,
        MESSAGE_TYPE as METADATA_MESSAGE_TYPE,
        RaceMetadata,
        RaceMetadataConflictError,
        RaceMetadataError,
        RaceMetadataStore,
    )


logger = logging.getLogger("FinishReview.PassageReceiver")

SCHEMA_VERSION = 1
MESSAGE_TYPE = "passage"
ACK_MESSAGE_TYPE = "passage_ack"
FOCUS_MESSAGE_TYPE = "race_focus"
FOCUS_ACK_MESSAGE_TYPE = "race_focus_ack"
DEFAULT_PATH = "/api/v1/passage-events"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 18765
DEFAULT_DISCOVERY_PORT = 18766
DISCOVERY_REQUEST = b"CYCLERACE_DISCOVER_VIDEOPIPE_V1"
DISCOVERY_SERVICE = "videopipe-finish"
MAX_BODY_BYTES = 1024 * 1024

_MISSING = object()


class PassageEventError(ValueError):
    """Raised when a passage payload does not satisfy protocol v1."""


class PassageEventConflictError(RuntimeError):
    """Raised when one revision is reused with different content."""


class PassageEventDeliveryError(RuntimeError):
    """Raised when the accepted-event callback cannot be notified."""


class PassageJournalError(RuntimeError):
    """Raised when the durable JSONL journal is invalid or unavailable."""


class PassageIngestResult(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class RaceFocus:
    """Ephemeral CycleRace command selecting one athlete in the finish console."""

    race_id: str
    stage_id: str
    athlete_id: str
    bib: str
    group_id: str
    emitted_at_ms: int
    schema_version: int = SCHEMA_VERSION
    message_type: str = FOCUS_MESSAGE_TYPE

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise PassageEventError("unsupported race focus schema_version")
        if self.message_type != FOCUS_MESSAGE_TYPE:
            raise PassageEventError("message_type must be race_focus")
        if not self.race_id.strip() or not self.stage_id.strip():
            raise PassageEventError("race_id and stage_id are required")
        if not self.athlete_id.strip() and not self.bib.strip():
            raise PassageEventError("athlete_id or bib is required")
        if self.emitted_at_ms < 0:
            raise PassageEventError("emitted_at_ms must be non-negative")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RaceFocus":
        if not isinstance(payload, Mapping):
            raise PassageEventError("race focus must be a JSON object")
        return cls(
            schema_version=_integer_field(payload, "schema_version"),
            message_type=_string_field(payload, "message_type"),
            race_id=_string_field(payload, "race_id"),
            stage_id=_string_field(payload, "stage_id"),
            athlete_id=_string_field(payload, "athlete_id", default=""),
            bib=_string_field(payload, "bib", default=""),
            group_id=_string_field(payload, "group_id", default=""),
            emitted_at_ms=_integer_field(payload, "emitted_at_ms"),
        )


def _string_field(
    payload: Mapping[str, Any],
    name: str,
    *,
    default: object = _MISSING,
) -> str:
    if name not in payload:
        if default is _MISSING:
            raise PassageEventError(f"{name} is required")
        return str(default)
    value = payload[name]
    if not isinstance(value, str):
        raise PassageEventError(f"{name} must be a string")
    return value


def _integer_field(
    payload: Mapping[str, Any],
    name: str,
    *,
    default: object = _MISSING,
) -> int:
    if name not in payload:
        if default is _MISSING:
            raise PassageEventError(f"{name} is required")
        return int(default)
    value = payload[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise PassageEventError(f"{name} must be an integer")
    return value


def _boolean_field(
    payload: Mapping[str, Any],
    name: str,
    *,
    default: object = _MISSING,
) -> bool:
    if name not in payload:
        if default is _MISSING:
            raise PassageEventError(f"{name} is required")
        return bool(default)
    value = payload[name]
    if not isinstance(value, bool):
        raise PassageEventError(f"{name} must be a boolean")
    return value


def _optional_integer_field(
    payload: Mapping[str, Any],
    name: str,
) -> Optional[int]:
    if name not in payload or payload[name] is None:
        return None
    return _integer_field(payload, name)


@dataclass(frozen=True, slots=True)
class PassageEvent:
    """Stable CycleRace -> FinishReview protocol v1 payload."""

    event_id: str
    race_id: str
    stage_id: str
    group_id: str
    sequence: int
    chip_id: str = ""
    bib: str = ""
    passage_time_ms: int = 0
    lap: int = 0
    source: str = "cyclerace"
    emitted_at_ms: int = 0
    revision: int = 1
    schema_version: int = SCHEMA_VERSION
    message_type: str = MESSAGE_TYPE
    passage_timestamp_ms: Optional[int] = None
    race_name: str = ""
    stage_name: str = ""
    group_name: str = ""
    athlete_id: str = ""
    athlete_name: str = ""
    team_name: str = ""
    is_active: bool = True

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise PassageEventError("unsupported passage event schema_version")
        if self.message_type != MESSAGE_TYPE:
            raise PassageEventError("message_type must be passage")
        for name in ("event_id", "race_id", "stage_id", "group_id"):
            if not str(getattr(self, name)).strip():
                raise PassageEventError(f"{name} is required")
        if self.sequence <= 0:
            raise PassageEventError("sequence must be positive")
        if not self.chip_id.strip() and not self.bib.strip():
            raise PassageEventError("chip_id or bib is required")
        if self.passage_time_ms < 0:
            raise PassageEventError("passage_time_ms must be non-negative")
        if (
            self.passage_timestamp_ms is not None
            and self.passage_timestamp_ms < 0
        ):
            raise PassageEventError(
                "passage_timestamp_ms must be non-negative when provided"
            )
        if self.lap < 0:
            raise PassageEventError("lap must be non-negative")
        if not self.source.strip():
            raise PassageEventError("source is required")
        if self.emitted_at_ms < 0:
            raise PassageEventError("emitted_at_ms must be non-negative")
        if self.revision <= 0:
            raise PassageEventError("revision must be positive")
        if not isinstance(self.is_active, bool):
            raise PassageEventError("is_active must be a boolean")

    @property
    def timeline_timestamp_ms(self) -> int:
        if self.passage_timestamp_ms is not None:
            return int(self.passage_timestamp_ms)
        return int(self.passage_time_ms)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PassageEvent":
        if not isinstance(payload, Mapping):
            raise PassageEventError("passage event must be a JSON object")
        return cls(
            schema_version=_integer_field(payload, "schema_version"),
            message_type=_string_field(payload, "message_type"),
            event_id=_string_field(payload, "event_id"),
            race_id=_string_field(payload, "race_id"),
            stage_id=_string_field(payload, "stage_id"),
            group_id=_string_field(payload, "group_id"),
            sequence=_integer_field(payload, "sequence"),
            chip_id=_string_field(payload, "chip_id", default=""),
            bib=_string_field(payload, "bib", default=""),
            passage_time_ms=_integer_field(payload, "passage_time_ms"),
            lap=_integer_field(payload, "lap"),
            source=_string_field(payload, "source"),
            emitted_at_ms=_integer_field(payload, "emitted_at_ms", default=0),
            revision=_integer_field(payload, "revision", default=1),
            passage_timestamp_ms=_optional_integer_field(
                payload,
                "passage_timestamp_ms",
            ),
            race_name=_string_field(payload, "race_name", default=""),
            stage_name=_string_field(payload, "stage_name", default=""),
            group_name=_string_field(payload, "group_name", default=""),
            athlete_id=_string_field(payload, "athlete_id", default=""),
            athlete_name=_string_field(payload, "athlete_name", default=""),
            team_name=_string_field(payload, "team_name", default=""),
            is_active=_boolean_field(payload, "is_active", default=True),
        )

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        return {
            "schema_version": payload["schema_version"],
            "message_type": payload["message_type"],
            "event_id": payload["event_id"],
            "race_id": payload["race_id"],
            "stage_id": payload["stage_id"],
            "group_id": payload["group_id"],
            "sequence": payload["sequence"],
            "chip_id": payload["chip_id"],
            "bib": payload["bib"],
            "passage_time_ms": payload["passage_time_ms"],
            "lap": payload["lap"],
            "source": payload["source"],
            "emitted_at_ms": payload["emitted_at_ms"],
            "revision": payload["revision"],
            "passage_timestamp_ms": payload["passage_timestamp_ms"],
            "race_name": payload["race_name"],
            "stage_name": payload["stage_name"],
            "group_name": payload["group_name"],
            "athlete_id": payload["athlete_id"],
            "athlete_name": payload["athlete_name"],
            "team_name": payload["team_name"],
            "is_active": payload["is_active"],
        }


def _looks_like_incomplete_json(value: str) -> bool:
    in_string = False
    escaped = False
    nesting = 0
    for character in value:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            nesting += 1
        elif character in "]}":
            if nesting == 0:
                return False
            nesting -= 1
    return in_string or nesting > 0


class PassageEventStore:
    """Append-only JSONL store retaining the latest revision per event id."""

    def __init__(self, journal_path: str | Path):
        self.journal_path = Path(journal_path).expanduser().absolute()
        if not str(self.journal_path):
            raise ValueError("passage event journal path is required")
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._events: dict[str, PassageEvent] = {}
        self._event_order: list[str] = []
        self._race_ids: set[str] = set()
        self._recovered_incomplete_tail = False
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.journal_path.exists():
            return
        try:
            content = self.journal_path.read_bytes()
        except OSError as error:
            raise PassageJournalError(
                f"failed to read passage event journal: {self.journal_path}"
            ) from error

        offset = 0
        lines = content.splitlines(keepends=True)
        for line_number, raw_line in enumerate(lines, start=1):
            terminated = raw_line.endswith(b"\n") or raw_line.endswith(b"\r")
            stripped = raw_line.rstrip(b"\r\n")
            if not stripped:
                offset += len(raw_line)
                continue
            try:
                text = stripped.decode("utf-8")
                payload = json.loads(text)
                event = PassageEvent.from_payload(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, PassageEventError) as error:
                is_tail = line_number == len(lines) and not terminated
                if is_tail:
                    try:
                        candidate = stripped.decode("utf-8")
                    except UnicodeDecodeError:
                        candidate = ""
                    if candidate and _looks_like_incomplete_json(candidate):
                        self._truncate(offset)
                        self._recovered_incomplete_tail = True
                        return
                raise PassageJournalError(
                    f"invalid passage event journal line {line_number}: {error}"
                ) from error
            self._merge_loaded(event, line_number)
            offset += len(raw_line)

    def _merge_loaded(self, event: PassageEvent, line_number: int) -> None:
        self._race_ids.add(event.race_id)
        current = self._events.get(event.event_id)
        if current is None:
            self._event_order.append(event.event_id)
            self._events[event.event_id] = event
            return
        if event.revision < current.revision:
            return
        if event.revision == current.revision:
            if event != current:
                raise PassageJournalError(
                    "conflicting passage event revision in journal "
                    f"line {line_number}: {event.event_id}"
                )
            return
        self._events[event.event_id] = event

    def _truncate(self, size: int) -> None:
        try:
            with self.journal_path.open("r+b") as journal:
                journal.truncate(size)
                journal.flush()
                os.fsync(journal.fileno())
        except OSError as error:
            raise PassageJournalError(
                f"failed to recover passage event journal: {self.journal_path}"
            ) from error

    def append(self, event: PassageEvent) -> PassageIngestResult:
        if not isinstance(event, PassageEvent):
            raise TypeError("event must be a PassageEvent")
        with self._lock:
            current = self._events.get(event.event_id)
            if current is not None:
                if event.revision < current.revision:
                    return PassageIngestResult.DUPLICATE
                if event.revision == current.revision:
                    if event != current:
                        raise PassageEventConflictError(
                            "passage event revision was reused with different content: "
                            f"{event.event_id}"
                        )
                    return PassageIngestResult.DUPLICATE

            record = (
                json.dumps(
                    event.to_payload(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            original_size = (
                self.journal_path.stat().st_size
                if self.journal_path.exists()
                else 0
            )
            separator = b""
            if original_size:
                try:
                    with self.journal_path.open("rb") as journal:
                        journal.seek(-1, os.SEEK_END)
                        if journal.read(1) not in {b"\n", b"\r"}:
                            separator = b"\n"
                except OSError as error:
                    raise PassageJournalError(
                        f"failed to inspect passage event journal: {self.journal_path}"
                    ) from error
            try:
                with self.journal_path.open("ab") as journal:
                    journal.write(separator)
                    journal.write(record)
                    journal.flush()
                    os.fsync(journal.fileno())
            except OSError as error:
                try:
                    self._truncate(original_size)
                except PassageJournalError:
                    logger.exception("Failed to roll back passage event journal")
                raise PassageJournalError(
                    f"failed to append passage event journal: {self.journal_path}"
                ) from error

            if current is None:
                self._event_order.append(event.event_id)
            self._events[event.event_id] = event
            self._race_ids.add(event.race_id)
            return PassageIngestResult.ACCEPTED

    def get(self, event_id: str) -> Optional[PassageEvent]:
        with self._lock:
            return self._events.get(str(event_id))

    def events(self, *, include_inactive: bool = False) -> tuple[PassageEvent, ...]:
        with self._lock:
            events = tuple(
                self._events[event_id] for event_id in self._event_order
            )
            if include_inactive:
                return events
            return tuple(event for event in events if event.is_active)

    def __len__(self) -> int:
        return len(self.events())

    @property
    def recovered_incomplete_tail(self) -> bool:
        with self._lock:
            return self._recovered_incomplete_tail


class PassageEventIngestor:
    """Transport-neutral ingestion with durable-before-callback ordering."""

    def __init__(
        self,
        store: PassageEventStore,
        on_accepted: Optional[Callable[[PassageEvent], None]] = None,
    ):
        self.store = store
        self._on_accepted = on_accepted
        self._delivery_lock = threading.RLock()
        self._delivered_revisions: dict[str, int] = {}

    def ingest_payload(self, payload: Mapping[str, Any]) -> PassageIngestResult:
        return self.ingest(PassageEvent.from_payload(payload))

    def ingest(self, event: PassageEvent) -> PassageIngestResult:
        result = self.store.append(event)
        current = self.store.get(event.event_id)
        if current is None or current.revision != event.revision:
            return PassageIngestResult.DUPLICATE

        with self._delivery_lock:
            delivered_revision = self._delivered_revisions.get(event.event_id, 0)
            if self._on_accepted is not None and delivered_revision < event.revision:
                try:
                    self._on_accepted(current)
                except Exception as error:
                    raise PassageEventDeliveryError(str(error)) from error
                self._delivered_revisions[event.event_id] = event.revision
        return result


class _PassageHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class PassageDiscoveryResponder:
    """Answer CycleRace LAN discovery without requiring an IP address."""

    def __init__(
        self,
        http_port: int,
        *,
        discovery_port: int = DEFAULT_DISCOVERY_PORT,
        host_name: str | None = None,
        auth_required: bool = False,
    ) -> None:
        self.http_port = int(http_port)
        self.discovery_port = int(discovery_port)
        self.host_name = str(host_name or socket.gethostname()).strip() or "FinishReview"
        self.auth_required = bool(auth_required)
        if not 1 <= self.http_port <= 65535:
            raise ValueError("passage receiver port is out of range")
        if self.discovery_port < 0 or self.discovery_port > 65535:
            raise ValueError("discovery port is out of range")
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((DEFAULT_HOST, self.discovery_port))
                sock.settimeout(0.2)
            except Exception:
                sock.close()
                raise
            self._stop.clear()
            thread = threading.Thread(
                target=self._run,
                args=(sock,),
                name="CycleRacePassageDiscovery",
                daemon=True,
            )
            self._socket = sock
            self._thread = thread
            thread.start()

    def _run(self, sock: socket.socket) -> None:
        payload = json.dumps(
            {
                "schema_version": 1,
                "message_type": "discovery_response",
                "service": DISCOVERY_SERVICE,
                "host_name": self.host_name,
                "port": self.http_port,
                "auth_required": self.auth_required,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        while not self._stop.is_set():
            try:
                request, sender = sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                break
            if request != DISCOVERY_REQUEST:
                continue
            try:
                sock.sendto(payload, sender)
            except OSError:
                if not self._stop.is_set():
                    logger.exception("Failed to answer CycleRace discovery")

    def stop(self) -> None:
        with self._lock:
            sock = self._socket
            thread = self._thread
            self._socket = None
            self._thread = None
            self._stop.set()
        if sock is not None:
            sock.close()
        if thread is not None:
            thread.join(timeout=2.0)

    @property
    def listen_port(self) -> int:
        with self._lock:
            if self._socket is not None:
                return int(self._socket.getsockname()[1])
            return self.discovery_port


def _handler_type(
    ingestor: PassageEventIngestor,
    metadata_store: RaceMetadataStore,
    on_metadata_accepted: Optional[Callable[[RaceMetadata], None]],
    on_focus_accepted: Optional[Callable[[RaceFocus], None]],
    request_path: str,
    max_body_bytes: int,
    shared_token: str,
):
    class PassageRequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            if urlsplit(self.path).path != request_path:
                self._send_json(404, "rejected", "endpoint not found")
                return
            if shared_token and not self._is_authorized(shared_token):
                self._send_json(
                    401,
                    "rejected",
                    "valid Bearer token is required",
                    authenticate=True,
                )
                return
            length_header = self.headers.get("Content-Length")
            try:
                content_length = int(length_header) if length_header is not None else -1
            except ValueError:
                content_length = -1
            if content_length < 0:
                self._send_json(400, "rejected", "valid Content-Length is required")
                return
            if content_length > max_body_bytes:
                self._send_json(413, "rejected", "passage event payload is too large")
                return
            try:
                raw_body = self.rfile.read(content_length)
                payload = json.loads(raw_body.decode("utf-8"))
                message_type = (
                    payload.get("message_type")
                    if isinstance(payload, Mapping)
                    else None
                )
                if message_type == MESSAGE_TYPE:
                    result = ingestor.ingest_payload(payload)
                    ack_message_type = ACK_MESSAGE_TYPE
                elif message_type == METADATA_MESSAGE_TYPE:
                    metadata = RaceMetadata.from_payload(payload)
                    result = metadata_store.store(metadata)
                    if (
                        result.value == PassageIngestResult.ACCEPTED.value
                        and on_metadata_accepted is not None
                    ):
                        on_metadata_accepted(metadata)
                    ack_message_type = METADATA_ACK_MESSAGE_TYPE
                elif message_type == FOCUS_MESSAGE_TYPE:
                    focus = RaceFocus.from_payload(payload)
                    if on_focus_accepted is not None:
                        on_focus_accepted(focus)
                    result = PassageIngestResult.ACCEPTED
                    ack_message_type = FOCUS_ACK_MESSAGE_TYPE
                else:
                    raise PassageEventError(
                        "message_type must be passage, race_metadata, or race_focus"
                    )
                status = 201 if result.value == PassageIngestResult.ACCEPTED.value else 200
                self._send_json(
                    status,
                    result.value,
                    message_type=ack_message_type,
                )
            except (PassageEventConflictError, RaceMetadataConflictError) as error:
                self._send_json(409, "rejected", str(error))
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                PassageEventError,
                RaceMetadataError,
            ) as error:
                self._send_json(400, "rejected", str(error))
            except PassageEventDeliveryError as error:
                self._send_json(503, "retry", str(error))
            except Exception as error:
                logger.exception("Passage event request failed")
                self._send_json(500, "error", str(error))

        def _send_json(
            self,
            status: int,
            result: str,
            error: str = "",
            *,
            message_type: str = ACK_MESSAGE_TYPE,
            authenticate: bool = False,
        ) -> None:
            body: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "message_type": message_type,
                "status": result,
            }
            if error:
                body["error"] = error
            encoded = json.dumps(
                body,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Connection", "close")
            if authenticate:
                self.send_header("WWW-Authenticate", "Bearer")
            self.end_headers()
            self.wfile.write(encoded)
            self.close_connection = True

        def _is_authorized(self, shared_token: str) -> bool:
            authorization = str(self.headers.get("Authorization") or "")
            scheme, separator, supplied_token = authorization.partition(" ")
            return (
                bool(separator)
                and scheme.lower() == "bearer"
                and hmac.compare_digest(
                    supplied_token.encode("utf-8"),
                    shared_token.encode("utf-8"),
                )
            )

        def log_message(self, format: str, *args: Any) -> None:
            logger.debug("CycleRace HTTP: " + format, *args)

    return PassageRequestHandler


class PassageEventReceiver:
    """Background HTTP receiver scoped to one FinishReview race directory."""

    def __init__(
        self,
        host: str,
        port: int,
        store: PassageEventStore,
        *,
        request_path: str = DEFAULT_PATH,
        max_body_bytes: int = MAX_BODY_BYTES,
        discovery_port: int | None = DEFAULT_DISCOVERY_PORT,
        shared_token: str = "",
        on_accepted: Optional[Callable[[PassageEvent], None]] = None,
        metadata_store: RaceMetadataStore | None = None,
        on_metadata_accepted: Optional[Callable[[RaceMetadata], None]] = None,
        on_focus_accepted: Optional[Callable[[RaceFocus], None]] = None,
    ):
        host = str(host).strip()
        port = int(port)
        request_path = str(request_path).strip()
        if not host:
            raise ValueError("passage receiver host is required")
        if port < 0 or port > 65535:
            raise ValueError("passage receiver port is out of range")
        if not request_path.startswith("/"):
            raise ValueError("passage receiver path must start with /")
        if int(max_body_bytes) <= 0:
            raise ValueError("max_body_bytes must be positive")
        if discovery_port is not None and not 0 <= int(discovery_port) <= 65535:
            raise ValueError("discovery port is out of range")
        self.host = host
        self.port = port
        self.request_path = request_path
        self.max_body_bytes = int(max_body_bytes)
        self.discovery_port = (
            None if discovery_port is None else int(discovery_port)
        )
        self.shared_token = str(shared_token or "").strip()
        self.store = store
        self.ingestor = PassageEventIngestor(store, on_accepted=on_accepted)
        self.metadata_store = metadata_store or RaceMetadataStore(
            store.journal_path.with_name("cyclerace_race_metadata.json")
        )
        self._on_metadata_accepted = on_metadata_accepted
        self._on_focus_accepted = on_focus_accepted
        self._lock = threading.RLock()
        self._server: Optional[_PassageHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._discovery: PassageDiscoveryResponder | None = None
        self._discovery_error = ""

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            handler = _handler_type(
                self.ingestor,
                self.metadata_store,
                self._on_metadata_accepted,
                self._on_focus_accepted,
                self.request_path,
                self.max_body_bytes,
                self.shared_token,
            )
            server = _PassageHTTPServer((self.host, self.port), handler)
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.1},
                name="CycleRacePassageReceiver",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            thread.start()
            if self.discovery_port is not None:
                discovery = PassageDiscoveryResponder(
                    self.listen_port,
                    discovery_port=self.discovery_port,
                    auth_required=bool(self.shared_token),
                )
                try:
                    discovery.start()
                except OSError as error:
                    self._discovery_error = str(error)
                    logger.warning(
                        "CycleRace automatic discovery is unavailable: %s",
                        error,
                    )
                else:
                    self._discovery = discovery
                    self._discovery_error = ""
            logger.info(
                "CycleRace passage receiver listening on %s:%s%s",
                self.host,
                self.listen_port,
                self.request_path,
            )

    def stop(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            discovery = self._discovery
            self._server = None
            self._thread = None
            self._discovery = None
        if discovery is not None:
            discovery.stop()
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5.0)
            if thread.is_alive():
                logger.error("CycleRace passage receiver did not stop within 5 seconds")

    @property
    def listen_port(self) -> int:
        with self._lock:
            if self._server is not None:
                return int(self._server.server_address[1])
            return self.port

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def discovery_error(self) -> str:
        with self._lock:
            return self._discovery_error


__all__ = [
    "ACK_MESSAGE_TYPE",
    "DEFAULT_DISCOVERY_PORT",
    "DEFAULT_HOST",
    "DEFAULT_PATH",
    "DEFAULT_PORT",
    "DISCOVERY_REQUEST",
    "DISCOVERY_SERVICE",
    "FOCUS_ACK_MESSAGE_TYPE",
    "FOCUS_MESSAGE_TYPE",
    "MESSAGE_TYPE",
    "PassageEvent",
    "PassageEventConflictError",
    "PassageEventDeliveryError",
    "PassageEventError",
    "PassageEventIngestor",
    "PassageEventReceiver",
    "PassageDiscoveryResponder",
    "PassageEventStore",
    "PassageIngestResult",
    "PassageJournalError",
    "RaceFocus",
    "SCHEMA_VERSION",
]
