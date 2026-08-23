"""Read-only RaceTiger finish feed for the FinishReview workspace."""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from typing import Any, Callable, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

try:
    from .passage_receiver import PassageEvent, PassageEventStore
except ImportError:
    from passage_receiver import PassageEvent, PassageEventStore

logger = logging.getLogger("FinishReview.RaceTiger")

BEIJING_TZ = timezone(timedelta(hours=8))
DEFAULT_POLL_INTERVAL_SECONDS = 2.0


class RaceTigerError(RuntimeError):
    """Raised when RaceTiger cannot provide a valid finish snapshot."""


def _url_origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None
    return parsed.scheme.lower(), str(parsed.hostname or "").lower(), port


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    """Allow RaceTiger redirects only within the configured transport origin."""

    def __init__(self, base_url: str):
        super().__init__()
        self._allowed_origin = _url_origin(base_url)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _url_origin(newurl) != self._allowed_origin:
            raise HTTPError(
                req.full_url,
                code,
                "RaceTiger redirect outside configured origin is not allowed",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True, slots=True)
class RaceTigerStatus:
    state: str
    message: str
    count: int = 0
    updated_at_ms: int = 0


def _key_token(value: Any) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def _first(mapping: Mapping[str, Any], *names: str, default: Any = "") -> Any:
    if not isinstance(mapping, Mapping):
        return default
    values = {
        _key_token(key): value
        for key, value in mapping.items()
        if value not in (None, "")
    }
    for name in names:
        value = values.get(_key_token(name))
        if value not in (None, ""):
            return value
    return default


def _records(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        record_keys = {
            "data",
            "rows",
            "list",
            "result",
            "items",
            "records",
            "value",
        }
        for key, value in payload.items():
            if _key_token(key) not in record_keys:
                continue
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
            if isinstance(value, Mapping):
                nested = _records(value)
                if nested:
                    return nested
        if any(
            _key_token(key) in {
                "athleteid",
                "athleteidno",
                "bib",
                "bibno",
                "passageid",
                "passtime",
                "finishtime",
                "finishstatus",
            }
            for key in payload
        ):
            return [payload]
    return []


def _record_fingerprint(records: list[Mapping[str, Any]]) -> tuple[str, ...]:
    return tuple(
        json.dumps(dict(record), ensure_ascii=False, sort_keys=True, default=str)
        for record in records
    )


def _next_page(payload: Any, current_page: int) -> Optional[int]:
    """Read common pagination metadata without assuming one response shape."""
    if not isinstance(payload, Mapping):
        return None
    for key in ("nextPage", "next_page", "NextPage"):
        value = _first(payload, key, default=None)
        try:
            page = int(value)
        except (TypeError, ValueError):
            continue
        if page > current_page:
            return page
    for key in ("hasNext", "has_next", "HasNext", "more", "hasMore"):
        value = _first(payload, key, default=None)
        if isinstance(value, str):
            value = value.strip().lower() in {"1", "true", "yes"}
        if value is True or (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value != 0
        ):
            return current_page + 1
    for key in ("totalPages", "total_pages", "pageCount", "pages"):
        value = _first(payload, key, default=None)
        try:
            if int(value) > current_page:
                return current_page + 1
        except (TypeError, ValueError):
            continue
    total = _first(payload, "total", "totalCount", "total_count", default=None)
    page_size = _first(payload, "pageSize", "page_size", "limit", "size", default=None)
    try:
        if int(total) > current_page * int(page_size):
            return current_page + 1
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    for nested in payload.values():
        if isinstance(nested, Mapping):
            nested_page = _next_page(nested, current_page)
            if nested_page is not None:
                return nested_page
    return None


def _parse_date(value: Any) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("/", "-")
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _event_date_from_payload(payload: Any) -> Optional[date]:
    if isinstance(payload, Mapping):
        value = _first(
            payload,
            "EventDate",
            "RaceDate",
            "Date",
            "EventDay",
            "CompetitionDate",
            default="",
        )
        parsed = _parse_date(value)
        if parsed is not None:
            return parsed
        for nested in payload.values():
            parsed = _event_date_from_payload(nested)
            if parsed is not None:
                return parsed
    for record in _records(payload):
        parsed = _parse_date(
            _first(
                record,
                "EventDate",
                "RaceDate",
                "Date",
                "EventDay",
                "CompetitionDate",
                default="",
            )
        )
        if parsed is not None:
            return parsed
    return None


def parse_beijing_timestamp(value: Any, event_date: Optional[date]) -> Optional[int]:
    """Parse RaceTiger PassTime into Beijing epoch milliseconds."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if math.isfinite(numeric):
            if numeric >= 100_000_000_000:
                return int(round(numeric))
            if numeric >= 1_000_000_000:
                return int(round(numeric * 1000.0))
    text = str(value or "").strip()
    if not text:
        return None
    try:
        numeric = float(text)
    except (TypeError, ValueError):
        numeric = None
    if numeric is not None and math.isfinite(numeric):
        if numeric >= 100_000_000_000:
            return int(round(numeric))
        if numeric >= 1_000_000_000:
            return int(round(numeric * 1000.0))
    iso_text = text.replace("/", "-").replace("Z", "+00:00")
    if "T" in iso_text or " " in iso_text:
        try:
            parsed_iso = datetime.fromisoformat(iso_text)
            if parsed_iso.tzinfo is None:
                parsed_iso = parsed_iso.replace(tzinfo=BEIJING_TZ)
            else:
                parsed_iso = parsed_iso.astimezone(BEIJING_TZ)
            return int(parsed_iso.timestamp() * 1000)
        except (TypeError, ValueError, OverflowError):
            pass
    text = text.replace("T", " ").replace("/", "-")
    if " " in text:
        date_text, text = text.split(" ", 1)
        event_date = _parse_date(date_text) or event_date
    text = text.rstrip("Z")
    if event_date is None:
        return None
    parts = text.split(":")
    if len(parts) != 3:
        return None
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds_text = parts[2]
        if "." in seconds_text:
            seconds, fraction = seconds_text.split(".", 1)
            microseconds = int((fraction + "000000")[:6])
        else:
            seconds = seconds_text
            microseconds = 0
        parsed = datetime.combine(
            event_date,
            datetime_time(int(hours), int(minutes), int(seconds), microseconds),
            tzinfo=BEIJING_TZ,
        )
    except (TypeError, ValueError, OverflowError):
        return None
    return int(parsed.timestamp() * 1000)


def _milliseconds_since_midnight(timestamp_ms: Optional[int]) -> int:
    if timestamp_ms is None:
        return 0
    local = datetime.fromtimestamp(timestamp_ms / 1000.0, BEIJING_TZ)
    return (
        ((local.hour * 60 + local.minute) * 60 + local.second) * 1000
        + local.microsecond // 1000
    )


@dataclass(frozen=True, slots=True)
class RaceTigerFinish:
    event_id: str
    athlete_id: str
    bib: str
    name: str
    group_id: str
    group_name: str
    team_name: str
    pass_time_text: str
    pass_timestamp_ms: int
    status: str
    position: str = ""
    gun_time: str = ""
    net_time: str = ""
    raw: Optional[Mapping[str, Any]] = None


class RaceTigerClient:
    """Small stdlib-only HTTP client for the observed RaceTiger DIF API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        pc: str = "",
        rid: str = "",
        timeout_seconds: float = 5.0,
        opener: Optional[Callable[..., Any]] = None,
    ):
        self.base_url = self._validated_base_url(base_url)
        self.token = str(token).strip()
        self.pc = str(pc).strip()
        self.rid = str(rid).strip()
        self.timeout_seconds = max(0.5, float(timeout_seconds))
        if not self.base_url:
            raise ValueError("RaceTiger base_url is required")
        if not self.token:
            raise ValueError("RaceTiger token is required")
        self._opener = opener or build_opener(
            _SameOriginRedirectHandler(self.base_url)
        ).open

    @staticmethod
    def _validated_base_url(base_url: str) -> str:
        value = str(base_url or "").strip().rstrip("/")
        if not value:
            raise ValueError("RaceTiger base_url is required")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("RaceTiger base_url must be an HTTP(S) URL")
        try:
            parsed.port
        except ValueError as error:
            raise ValueError("RaceTiger base_url contains an invalid port") from error
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("RaceTiger base_url must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("RaceTiger base_url must not contain query or fragment data")
        if parsed.scheme == "http" and not RaceTigerClient._is_loopback_host(
            parsed.hostname
        ):
            raise ValueError("RaceTiger base_url must use HTTPS outside this computer")
        return value

    @staticmethod
    def _is_loopback_host(host: str) -> bool:
        if host.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def post(
        self,
        endpoint: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        page: int = 1,
    ) -> Any:
        query: dict[str, Any] = {
            "pc": self.pc,
            "rid": self.rid,
            "token": self.token,
            "page": int(page),
        }
        if payload:
            query.update(dict(payload))
        query_text = urlencode(query)
        url = f"{self.base_url}/{str(endpoint).lstrip('/')}?{query_text}"
        request = Request(
            url,
            data=b"",
            headers={
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise RaceTigerError(f"RaceTiger request failed: {type(error).__name__}") from error
        try:
            return json.loads(raw.lstrip("\ufeff"))
        except json.JSONDecodeError as error:
            raise RaceTigerError("RaceTiger returned invalid JSON") from error


def _text(mapping: Mapping[str, Any], *names: str) -> str:
    return str(_first(mapping, *names, default="") or "").strip()


def _is_finish_status(value: Any) -> bool:
    return _key_token(value) in {"fin", "finish", "finished"}


def _is_finish_point(value: Any) -> bool:
    return _key_token(value) in {"finish", "fin", "finishline", "finishpoint"}


class RaceTigerSource:
    """Poll RaceTiger and append only FINISH observations to a passage store."""

    def __init__(
        self,
        client: RaceTigerClient,
        store: PassageEventStore,
        *,
        race_id: str,
        stage_id: str = "finish",
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        on_event: Optional[Callable[[PassageEvent], None]] = None,
        on_status: Optional[Callable[[RaceTigerStatus], None]] = None,
    ):
        self.client = client
        self.store = store
        self.race_id = str(race_id).strip()
        self.stage_id = str(stage_id).strip() or "finish"
        self.poll_interval_seconds = max(0.5, float(poll_interval_seconds))
        self._on_event = on_event
        self._on_status = on_status
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._revision_by_event: dict[str, int] = {}
        self._sequence = max((event.sequence for event in store.events()), default=0)
        self._last_poll_skipped = 0

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="RaceTigerPoller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
            if thread.is_alive():
                logger.error("RaceTiger poller did not stop within 5 seconds")
                return
            self._thread = None

    def _emit_status(self, status: RaceTigerStatus) -> None:
        callback = self._on_status
        if callback is not None:
            callback(status)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                events = self.poll_once()
                now_ms = int(time.time() * 1000.0)
                message = f"RaceTiger: received {len(self.store)} records"
                if self._last_poll_skipped:
                    message += f"; skipped {self._last_poll_skipped}"
                self._emit_status(
                    RaceTigerStatus(
                        "ok",
                        message,
                        len(self.store),
                        now_ms,
                    )
                )
                for event in events:
                    if self._on_event is not None:
                        self._on_event(event)
            except Exception as error:
                logger.warning("RaceTiger poll failed: %s", error)
                self._emit_status(
                    RaceTigerStatus("error", f"RaceTiger: API error ({error})")
                )
            self._stop.wait(self.poll_interval_seconds)

    def _post_page(self, endpoint: str, page: int) -> Any:
        try:
            return self.client.post(endpoint, page=page)
        except TypeError as error:
            # Keep lightweight test doubles and older adapters compatible with
            # the page-aware client contract.
            if "page" not in str(error):
                raise
            return self.client.post(endpoint)

    def _fetch_records(
        self,
        endpoint: str,
    ) -> tuple[tuple[Any, ...], tuple[Mapping[str, Any], ...]]:
        payloads: list[Any] = []
        records: list[Mapping[str, Any]] = []
        seen_pages: set[tuple[str, ...]] = set()
        page = 1
        while True:
            payload = self._post_page(endpoint, page)
            page_records = _records(payload)
            if not page_records:
                payloads.append(payload)
                break
            fingerprint = _record_fingerprint(page_records)
            if fingerprint in seen_pages:
                break
            seen_pages.add(fingerprint)
            payloads.append(payload)
            records.extend(page_records)
            next_page = _next_page(payload, page)
            if next_page is None or next_page <= page:
                break
            page = next_page
        return tuple(payloads), tuple(records)

    def poll_once(self) -> tuple[PassageEvent, ...]:
        # RaceTiger's DIF endpoints use POST with query parameters. Keep the
        # endpoint names centralized so the read-only source cannot drift into
        # the CycleRace receiver protocol.
        self._last_poll_skipped = 0
        info_payloads, _info_records = self._fetch_records("Dif/info")
        info = info_payloads[0] if info_payloads else {}
        event_date = next(
            (
                parsed
                for payload in info_payloads
                if (parsed := _event_date_from_payload(payload)) is not None
            ),
            None,
        )

        _bio_payloads, bios = self._fetch_records("Dif/bio")
        bio_by_id = {
            _text(item, "AthleteId", "AthleteID", "AthleteNo", "PersonId", "ID"): item
            for item in bios
            if _text(item, "AthleteId", "AthleteID", "AthleteNo", "PersonId", "ID")
        }
        bio_by_bib = {
            _text(item, "BIB", "BibNo", "Bib", "StartNo", "Number"): item
            for item in bios
            if _text(item, "BIB", "BibNo", "Bib", "StartNo", "Number")
        }
        _score_payloads, scores = self._fetch_records("Dif/score")
        finish_scores = [
            item
            for item in scores
            if _is_finish_status(
                _first(
                    item,
                    "FinishStatus",
                    "FinishState",
                    "ResultStatus",
                    "Status",
                    "State",
                    default="",
                )
            )
        ]
        _split_payloads, split_rows = self._fetch_records("Dif/split")
        split_by_id: dict[str, Mapping[str, Any]] = {}
        split_by_bib: dict[str, Mapping[str, Any]] = {}
        for row in split_rows:
            point_name = _first(
                row,
                "TpName",
                "TPName",
                "PointName",
                "TimingPoint",
                "Point",
                "Type",
                default="",
            )
            if not _is_finish_point(point_name):
                continue
            athlete_id = _text(
                row,
                "AthleteId",
                "AthleteID",
                "AthleteNo",
                "PersonId",
                "ID",
            )
            bib = _text(row, "BIB", "BibNo", "Bib", "StartNo", "Number")
            if athlete_id:
                split_by_id[athlete_id] = row
            if bib:
                split_by_bib[bib] = row

        normalized: list[PassageEvent] = []
        for score in finish_scores:
            athlete_id = _text(
                score,
                "AthleteId",
                "AthleteID",
                "AthleteNo",
                "PersonId",
                "ID",
            )
            bib = _text(score, "BIB", "BibNo", "Bib", "StartNo", "Number")
            chip_id = _text(score, "ChipNo", "ChipId", "Chip", "Transponder")
            if not bib and not chip_id:
                self._last_poll_skipped += 1
                continue
            bio = bio_by_id.get(athlete_id) or bio_by_bib.get(bib) or {}
            split = split_by_id.get(athlete_id) or split_by_bib.get(bib) or {}
            pass_text = _text(
                split,
                "PassTime",
                "PassageTime",
                "FinishPassTime",
                "FinishPassageTime",
                "FinishTime",
                "FinishTimestamp",
                "FinishDateTime",
            )
            if not pass_text:
                pass_text = _text(
                    score,
                    "PassTime",
                    "PassageTime",
                    "FinishPassTime",
                    "FinishPassageTime",
                    "FinishTime",
                    "FinishTimestamp",
                    "FinishDateTime",
                )
            pass_timestamp_ms = parse_beijing_timestamp(pass_text, event_date)
            if pass_timestamp_ms is None:
                self._last_poll_skipped += 1
                continue
            source_event_id = _text(
                score,
                "EventId",
                "EventID",
                "ResultId",
                "ResultID",
                "PassId",
                "PassID",
            )
            if not source_event_id:
                source_event_id = athlete_id or bib
            event_id = f"racetiger:{self.race_id}:{source_event_id}:{athlete_id or bib}"
            group_id = _text(
                score,
                "GroupId",
                "GroupID",
                "CategoryId",
                "CategoryID",
                "Group",
                "ClassId",
            )
            group_name = _text(
                score,
                "GroupName",
                "CategoryName",
                "ClassName",
                "Group",
            )
            current = self.store.get(event_id)
            athlete_name = _text(bio, "Name", "AthleteName", "RealName", "FullName")
            team_name = _text(bio, "TeamName", "Team", "ClubName", "Club")
            normalized_group_id = group_id or "finish"
            if current is None:
                revision = 1
            else:
                current_values = (
                    current.chip_id,
                    current.bib,
                    current.timeline_timestamp_ms,
                    current.group_id,
                    current.group_name,
                    current.athlete_id,
                    current.athlete_name,
                    current.team_name,
                )
                next_values = (
                    chip_id,
                    bib,
                    pass_timestamp_ms,
                    normalized_group_id,
                    group_name,
                    athlete_id,
                    athlete_name,
                    team_name,
                )
                revision = current.revision + (
                    1 if current_values != next_values else 0
                )
            if current is None:
                self._sequence += 1
                sequence = self._sequence
            else:
                # A revision updates the existing observation; its stable
                # sequence must remain unchanged for downstream consumers.
                sequence = current.sequence
            event = PassageEvent(
                event_id=event_id,
                race_id=self.race_id,
                stage_id=self.stage_id,
                group_id=normalized_group_id,
                sequence=sequence,
                chip_id=chip_id,
                bib=bib,
                passage_time_ms=_milliseconds_since_midnight(pass_timestamp_ms),
                passage_timestamp_ms=pass_timestamp_ms,
                lap=1,
                source="racetiger",
                emitted_at_ms=int(time.time() * 1000.0),
                revision=revision,
                race_name=str(
                    _first(
                        info if isinstance(info, Mapping) else {},
                        "EventName",
                        "RaceName",
                        default="",
                    )
                ),
                stage_name="Finish",
                group_name=group_name,
                athlete_id=athlete_id,
                athlete_name=athlete_name,
                team_name=team_name,
            )
            normalized.append(event)
        normalized.sort(key=lambda event: (event.timeline_timestamp_ms, event.event_id))
        accepted: list[PassageEvent] = []
        for event in normalized:
            current = self.store.get(event.event_id)
            if current is not None and current.revision == event.revision:
                continue
            self.store.append(event)
            accepted.append(event)
        return tuple(accepted)


__all__ = [
    "RaceTigerClient",
    "RaceTigerError",
    "RaceTigerFinish",
    "RaceTigerSource",
    "RaceTigerStatus",
    "parse_beijing_timestamp",
]
