import logging
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from realtime import racetiger_source as racetiger_source_module
from realtime.passage_receiver import PassageEvent, PassageEventStore
from realtime.racetiger_source import (
    RaceTigerClient,
    RaceTigerError,
    RaceTigerSource,
    parse_beijing_timestamp,
)


class FakeClient:
    def __init__(self):
        self.payloads = {
            "Dif/info": {"EventDate": "2026-08-22", "EventName": "Finish"},
            "Dif/bio": [
                {"AthleteId": "101", "BIB": "12", "Name": "Alice", "TeamName": "A"},
                {"AthleteId": "102", "BIB": "15", "Name": "Bob", "TeamName": "B"},
            ],
            "Dif/score": [
                {"EventId": "e-2", "AthleteId": "102", "BIB": "15", "FinishStatus": "FIN"},
                {"EventId": "e-1", "AthleteId": "101", "BIB": "12", "FinishStatus": "FIN"},
            ],
            "Dif/split": [
                {"TpName": "FINISH", "AthleteId": "102", "BIB": "15", "PassTime": "10:00:02.000"},
                {"TpName": "FINISH", "AthleteId": "101", "BIB": "12", "PassTime": "10:00:01.000"},
            ],
        }

    def post(self, endpoint, payload=None):
        return self.payloads[endpoint]


def test_parse_beijing_timestamp():
    value = parse_beijing_timestamp("10:00:01.250", __import__("datetime").date(2026, 8, 22))
    assert value == 1_787_364_001_250
    assert parse_beijing_timestamp("2026-08-22T10:00:01.250", None) == value
    assert parse_beijing_timestamp(1_787_364_001.25, None) == value
    assert parse_beijing_timestamp("1787364001250", None) == value


def test_poll_once_keeps_finish_time_order_and_deduplicates(tmp_path):
    store = PassageEventStore(tmp_path / "racetiger.jsonl")
    source = RaceTigerSource(FakeClient(), store, race_id="racetiger:test")

    accepted = source.poll_once()

    assert [event.bib for event in accepted] == ["12", "15"]
    assert [event.bib for event in store.events()] == ["12", "15"]
    assert all(event.source == "racetiger" for event in accepted)
    assert source.poll_once() == ()


def test_run_status_counts_only_current_racetiger_finish_records(tmp_path):
    store = PassageEventStore(tmp_path / "racetiger.jsonl")
    for sequence, (race_id, stage_id, source_name) in enumerate(
        (
            ("RID-CURRENT", "finish", "racetiger"),
            ("RID-OLD", "finish", "racetiger"),
            ("RID-CURRENT", "finish", "cyclerace"),
            ("RID-CURRENT", "other-stage", "racetiger"),
        ),
        start=1,
    ):
        store.append(
            PassageEvent(
                event_id=f"event-{sequence}",
                race_id=race_id,
                stage_id=stage_id,
                group_id="finish",
                sequence=sequence,
                bib=str(sequence),
                source=source_name,
            )
        )

    statuses = []
    source = RaceTigerSource(
        object(),
        store,
        race_id="RID-CURRENT",
        on_status=statuses.append,
    )
    source.poll_once = lambda: ()

    def capture_status(status):
        statuses.append(status)
        source._stop.set()

    source._on_status = capture_status
    source._run()

    assert statuses[0].count == 1
    assert statuses[0].message == "RaceTiger: received 1 records"


def test_run_logs_expected_racetiger_error_without_traceback(tmp_path, caplog):
    statuses = []
    source = RaceTigerSource(
        object(),
        PassageEventStore(tmp_path / "expected-error.jsonl"),
        race_id="RID-EXPECTED",
        on_status=statuses.append,
    )

    def fail_poll():
        source._stop.set()
        raise RaceTigerError("RaceTiger request failed: TimeoutError")

    source.poll_once = fail_poll
    with caplog.at_level(logging.WARNING, logger="FinishReview.RaceTiger"):
        source._run()

    warning = next(record for record in caplog.records if record.levelno == logging.WARNING)
    assert warning.exc_info is None
    assert statuses[-1].message == "RaceTiger: API error (RaceTiger request failed: TimeoutError)"


def test_run_logs_unexpected_error_with_traceback_and_generic_status(tmp_path, caplog):
    statuses = []
    source = RaceTigerSource(
        object(),
        PassageEventStore(tmp_path / "unexpected-error.jsonl"),
        race_id="RID-UNEXPECTED",
        on_status=statuses.append,
    )

    def fail_poll():
        source._stop.set()
        raise AttributeError("private implementation detail")

    source.poll_once = fail_poll
    with caplog.at_level(logging.ERROR, logger="FinishReview.RaceTiger"):
        source._run()

    error = next(record for record in caplog.records if record.levelno == logging.ERROR)
    assert error.exc_info is not None
    assert statuses[-1].message == "RaceTiger: internal polling error"
    assert "private implementation detail" not in statuses[-1].message


def test_run_throttles_repeated_unexpected_error_tracebacks(tmp_path, caplog):
    statuses = []
    source = RaceTigerSource(
        object(),
        PassageEventStore(tmp_path / "repeated-error.jsonl"),
        race_id="RID-REPEATED",
        on_status=statuses.append,
    )

    class FastStop:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, _timeout):
            return self.stopped

    stop = FastStop()
    source._stop = stop
    attempts = 0

    def fail_poll():
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            stop.set()
        raise AttributeError("repeated implementation failure")

    source.poll_once = fail_poll
    with caplog.at_level(logging.ERROR, logger="FinishReview.RaceTiger"):
        source._run()

    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert attempts == 2
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert [status.message for status in statuses] == [
        "RaceTiger: internal polling error",
        "RaceTiger: internal polling error",
    ]


@pytest.mark.parametrize(
    "poll_interval",
    [float("nan"), float("inf"), -float("inf")],
)
def test_source_rejects_nonfinite_poll_interval(tmp_path, poll_interval):
    with pytest.raises(ValueError, match="poll interval must be finite"):
        RaceTigerSource(
            object(),
            PassageEventStore(tmp_path / "invalid-interval.jsonl"),
            race_id="RID-INVALID",
            poll_interval_seconds=poll_interval,
        )


def test_poll_once_enriches_score_identity_from_bio(tmp_path):
    class RealShapeClient:
        def post(self, endpoint, *, page=1):
            payloads = {
                "Dif/info": {"EventDate": "2026-08-23"},
                "Dif/bio": [
                    {
                        "AthleteId": "101",
                        "BIB": "1",
                        "ChipCode": "A0000008",
                        "Name": "Alice",
                        "Category": "Women Elite",
                    }
                ],
                "Dif/score": [
                    {
                        "EventId": "event-1",
                        "AthleteId": "101",
                        "FinishStatus": "FIN",
                    }
                ],
                "Dif/split": [
                    {
                        "AthleteId": "101",
                        "TpName": "FINISH",
                        "PassTime": "2026-08-23 22:13:26",
                    }
                ],
            }
            return payloads[endpoint] if page == 1 else []

    source = RaceTigerSource(
        RealShapeClient(),
        PassageEventStore(tmp_path / "racetiger.jsonl"),
        race_id="RID-2026",
    )

    accepted = source.poll_once()

    assert len(accepted) == 1
    assert accepted[0].bib == "1"
    assert accepted[0].chip_id == "A0000008"
    assert accepted[0].athlete_name == "Alice"
    assert accepted[0].group_name == "Women Elite"


def test_poll_once_accepts_nested_case_variant_dif_rows(tmp_path):
    class NestedClient:
        def post(self, endpoint, payload=None):
            payloads = {
                "Dif/info": {"data": [{"RaceDate": "2026/08/22", "RaceName": "Nested"}]},
                "Dif/bio": {
                    "Data": [
                        {"AthleteID": "a-1", "BibNo": "8", "FullName": "Nested Athlete"}
                    ]
                },
                "Dif/score": {
                    "result": [
                        {
                            "ResultID": "result-8",
                            "AthleteID": "a-1",
                            "BibNo": "8",
                            "Status": "FIN",
                        }
                    ]
                },
                "Dif/split": {
                    "rows": [
                        {
                            "TPName": "FINISH",
                            "AthleteID": "a-1",
                            "BibNo": "8",
                            "PassTime": "10:02:03.125",
                        }
                    ]
                },
            }
            return payloads[endpoint]

    store = PassageEventStore(tmp_path / "nested.jsonl")
    accepted = RaceTigerSource(NestedClient(), store, race_id="nested").poll_once()

    assert len(accepted) == 1
    assert accepted[0].bib == "8"
    assert accepted[0].athlete_name == "Nested Athlete"
    assert accepted[0].passage_time_ms == (10 * 60 * 60 + 2 * 60 + 3) * 1000 + 125


def test_poll_once_reads_declared_additional_pages(tmp_path):
    class PagedClient:
        def post(self, endpoint, *, page=1):
            if endpoint == "Dif/info":
                return {"data": [{"EventDate": "2026-08-22", "EventName": "Paged"}]}
            if endpoint == "Dif/bio":
                rows = {
                    1: [{"AthleteId": "a-1", "BIB": "1", "Name": "One"}],
                    2: [{"AthleteId": "a-2", "BIB": "2", "Name": "Two"}],
                }[page]
                return {"data": rows, "totalPages": 2}
            if endpoint == "Dif/score":
                rows = {
                    1: [{"EventId": "e-1", "AthleteId": "a-1", "BIB": "1", "Status": "FIN"}],
                    2: [{"EventId": "e-2", "AthleteId": "a-2", "BIB": "2", "Status": "FIN"}],
                }[page]
                return {"data": rows, "totalPages": 2}
            rows = {
                1: [{"TpName": "FINISH", "AthleteId": "a-1", "BIB": "1", "PassTime": "10:00:01"}],
                2: [{"TpName": "FINISH", "AthleteId": "a-2", "BIB": "2", "PassTime": "10:00:02"}],
            }[page]
            return {"data": rows, "totalPages": 2}

    store = PassageEventStore(tmp_path / "paged.jsonl")
    accepted = RaceTigerSource(PagedClient(), store, race_id="paged").poll_once()

    assert [event.bib for event in accepted] == ["1", "2"]


def test_poll_once_accepts_numeric_has_next_and_score_finish_time(tmp_path):
    class NumericPageClient:
        def post(self, endpoint, *, page=1):
            if endpoint == "Dif/info":
                return {"EventDate": "2026-08-22", "hasNext": 0}
            if endpoint == "Dif/bio":
                return {"data": [{"AthleteId": "a-1", "BIB": "9", "Name": "Nine"}], "hasNext": 0}
            if endpoint == "Dif/score":
                if page == 1:
                    return {
                        "data": [
                            {
                                "EventId": "e-9",
                                "AthleteId": "a-1",
                                "BIB": "9",
                                "Status": "FIN",
                                "PassTime": "10:00:09.000",
                            }
                        ],
                        "hasNext": 1,
                    }
                return {
                    "data": [
                        {
                            "EventId": "e-10",
                            "AthleteId": "a-2",
                            "BIB": "10",
                            "Status": "FIN",
                            "PassTime": "10:00:10.000",
                        }
                    ],
                    "hasNext": 0,
                }
            return {"data": [], "hasNext": 0}

    store = PassageEventStore(tmp_path / "numeric-pages.jsonl")
    accepted = RaceTigerSource(NumericPageClient(), store, race_id="numeric").poll_once()

    assert [event.bib for event in accepted] == ["9", "10"]
    assert accepted[0].passage_time_ms == (10 * 60 * 60 + 9) * 1000


def test_poll_once_increments_revision_when_identity_details_change(tmp_path):
    class MutableClient:
        def __init__(self):
            self.name = "Before"
            self.team = "Team A"

        def post(self, endpoint, payload=None):
            if endpoint == "Dif/info":
                return {"EventDate": "2026-08-22"}
            if endpoint == "Dif/bio":
                return [{"AthleteId": "a-1", "BIB": "1", "Name": self.name, "TeamName": self.team}]
            if endpoint == "Dif/score":
                return [{"EventId": "e-1", "AthleteId": "a-1", "BIB": "1", "Status": "FIN"}]
            return [{"TpName": "FINISH", "AthleteId": "a-1", "BIB": "1", "PassTime": "10:00:01"}]

    client = MutableClient()
    store = PassageEventStore(tmp_path / "revision.jsonl")
    source = RaceTigerSource(client, store, race_id="revision")

    first = source.poll_once()
    client.name = "After"
    client.team = "Team B"
    second = source.poll_once()

    assert first[0].revision == 1
    assert second[0].revision == 2
    assert second[0].sequence == first[0].sequence
    assert second[0].athlete_name == "After"
    assert second[0].team_name == "Team B"


def test_racetiger_client_uses_query_parameters_for_post():
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"data": []}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.method
        captured["body"] = request.data
        captured["headers"] = dict(request.headers)
        captured["timeout"] = timeout
        return Response()

    test_token = "placeholder"
    client = RaceTigerClient(
        "https://rqs.racetigertiming.com",
        test_token,
        pc="pc-1",
        rid="rid-2",
        opener=fake_urlopen,
    )

    assert client.post("Dif/info") == {"data": []}
    assert captured["method"] == "POST"
    assert captured["body"] == b""
    assert "pc=pc-1" in captured["url"]
    assert "rid=rid-2" in captured["url"]
    assert f"token={test_token}" in captured["url"]
    assert "page=1" in captured["url"]
    assert "Authorization" not in captured["headers"]


def test_racetiger_client_reports_closed_event_data_interface():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"code":1,"msg":"The event data interface has been closed"}'

    client = RaceTigerClient(
        "https://rqs.racetigertiming.com",
        "placeholder",
        pc="pc-1",
        rid="rid-2",
        opener=lambda *_args, **_kwargs: Response(),
    )

    with pytest.raises(RaceTigerError, match="赛虎赛事数据接口未开放"):
        client.post("Dif/info")


@pytest.mark.parametrize(
    "base_url",
    [
        "http://racetiger.example.test",
        "ftp://127.0.0.1/feed",
        "rqs.racetigertiming.com",
        "https://user:password@racetiger.example.test",
        "https://racetiger.example.test?token=unexpected",
        "https://racetiger.example.test:invalid",
    ],
)
def test_racetiger_client_rejects_insecure_or_invalid_base_url(base_url):
    with pytest.raises(ValueError):
        RaceTigerClient(base_url, "placeholder")


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:8080",
        "http://localhost:8080/api",
        "http://[::1]:8080",
        "https://rqs.racetigertiming.com",
    ],
)
def test_racetiger_client_allows_https_and_local_http(base_url):
    client = RaceTigerClient(base_url, "placeholder")

    assert client.base_url == base_url


@pytest.mark.parametrize(
    "target_url",
    [
        "http://rqs.racetigertiming.com/Dif/info?token=placeholder",
        "https://other.example.test/Dif/info?token=placeholder",
        "ftp://rqs.racetigertiming.com/Dif/info?token=placeholder",
    ],
)
def test_racetiger_redirect_handler_rejects_origin_changes(target_url):
    handler = racetiger_source_module._SameOriginRedirectHandler(
        "https://rqs.racetigertiming.com"
    )
    request = Request(
        "https://rqs.racetigertiming.com/Dif/info?token=placeholder",
        data=b"",
        method="POST",
    )

    with pytest.raises(HTTPError):
        handler.redirect_request(request, None, 302, "Found", {}, target_url)


def test_racetiger_redirect_handler_allows_same_origin():
    handler = racetiger_source_module._SameOriginRedirectHandler(
        "https://rqs.racetigertiming.com"
    )
    request = Request(
        "https://rqs.racetigertiming.com/Dif/info?token=placeholder",
        data=b"",
        method="POST",
    )

    redirected = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://rqs.racetigertiming.com/Dif/info2?token=placeholder",
    )

    assert redirected.full_url.startswith(
        "https://rqs.racetigertiming.com/Dif/info2"
    )
