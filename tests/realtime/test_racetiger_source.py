from urllib.error import HTTPError
from urllib.request import Request

import pytest

from realtime import racetiger_source as racetiger_source_module
from realtime.passage_receiver import PassageEventStore
from realtime.racetiger_source import (
    RaceTigerClient,
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
