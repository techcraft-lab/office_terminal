import asyncio
import copy
import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from office_terminal.main import create_app
from office_terminal.services import weather_service
from office_terminal.services.weather_service import (
    WeatherAreaNotFoundError,
    WeatherConfig,
    WeatherConfigError,
    WeatherDataError,
    WeatherService,
    build_today_view,
    load_weather_config,
    parse_forecast,
    parse_weather_config,
)

JST = ZoneInfo("Asia/Tokyo")
FUKUOKA = WeatherConfig()
KITAKYUSHU = WeatherConfig("北九州地方", "400000", "400020", "82056")


def _areas(*items):
    return [{"area": {"name": name, "code": code}, **values} for name, code, values in items]


def forecast_payload():
    """2026-10-03 17:00発表の福岡県予報(実JSONの構造)。"""
    return [
        {
            "publishingOffice": "福岡管区気象台",
            "reportDatetime": "2026-10-03T17:00:00+09:00",
            "timeSeries": [
                {
                    "timeDefines": ["2026-10-03T17:00:00+09:00", "2026-10-04T00:00:00+09:00", "2026-10-05T00:00:00+09:00"],
                    "areas": _areas(
                        ("福岡地方", "400010", {"weatherCodes": ["111", "212", "200"], "weathers": ["晴れ\u3000夜遅く\u3000くもり", "くもり\u3000夜\u3000雨", "くもり"]}),
                        ("北九州地方", "400020", {"weatherCodes": ["111", "214", "202"], "weathers": ["晴れ\u3000夜遅く\u3000くもり", "くもり\u3000夕方\u3000から\u3000雨", "くもり\u3000一時\u3000雨"]}),
                    ),
                },
                {
                    "timeDefines": [
                        "2026-10-03T18:00:00+09:00", "2026-10-04T00:00:00+09:00", "2026-10-04T06:00:00+09:00",
                        "2026-10-04T12:00:00+09:00", "2026-10-04T18:00:00+09:00",
                    ],
                    "areas": _areas(
                        ("福岡地方", "400010", {"pops": ["0", "0", "10", "30", "50"]}),
                        ("北九州地方", "400020", {"pops": ["5", "5", "15", "35", "55"]}),
                    ),
                },
                {
                    "timeDefines": ["2026-10-04T00:00:00+09:00", "2026-10-04T09:00:00+09:00"],
                    "areas": _areas(
                        ("福岡", "82182", {"temps": ["19", "24"]}),
                        ("八幡", "82056", {"temps": ["18", "23"]}),
                    ),
                },
            ],
        },
        {
            "publishingOffice": "福岡管区気象台",
            "reportDatetime": "2026-10-03T17:00:00+09:00",
            "timeSeries": [
                {
                    "timeDefines": ["2026-10-04T00:00:00+09:00", "2026-10-05T00:00:00+09:00"],
                    "areas": _areas(("福岡県", "400000", {"weatherCodes": ["212", "200"], "pops": ["", "40"]})),
                },
                {
                    "timeDefines": ["2026-10-04T00:00:00+09:00", "2026-10-05T00:00:00+09:00"],
                    "areas": _areas(("福岡", "82182", {"tempsMin": ["", "20"], "tempsMax": ["", "27"]})),
                },
            ],
        },
    ]


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=JST)


def view(payload, config=FUKUOKA, now="2026-10-03T20:41:00"):
    return build_today_view(parse_forecast(payload, config), config, at(now))


def test_today_view_does_not_borrow_tomorrow_values():
    result = view(forecast_payload())
    assert result["headline"] == "晴れ 夜遅く くもり" and result["full_text"] == "晴れ 夜遅く くもり"
    assert result["icon"] == "☀"
    assert result["temperature"] == "本日の気温予報なし"
    assert result["pops"] == [{"label": "18–24時", "text": "0%"}] and result["pop_text"] == "18–24時 0%"
    assert result["issued"] == "17:00発表" and result["office_name"] == "福岡管区気象台"


def test_date_boundary_selects_new_day_by_time_not_position():
    result = view(forecast_payload(), now="2026-10-04T00:30:00")
    assert result["headline"] == "くもり 夜 雨"
    assert result["temperature"] == "最高24℃／最低19℃"
    assert [pop["label"] for pop in result["pops"]] == ["0–6時", "6–12時", "12–18時", "18–24時"]
    assert [pop["text"] for pop in result["pops"]] == ["0%", "10%", "30%", "50%"]


def test_only_remaining_pop_bands_are_shown_without_averaging():
    result = view(forecast_payload(), now="2026-10-04T13:00:00")
    assert result["pops"] == [{"label": "12–18時", "text": "30%"}, {"label": "18–24時", "text": "50%"}]


def test_morning_issue_with_today_max_only():
    payload = forecast_payload()
    payload[0]["reportDatetime"] = "2026-10-03T05:00:00+09:00"
    payload[0]["timeSeries"][2]["timeDefines"] = [
        "2026-10-03T09:00:00+09:00", "2026-10-04T00:00:00+09:00", "2026-10-04T09:00:00+09:00",
    ]
    payload[0]["timeSeries"][2]["areas"][0]["temps"] = ["25", "19", "24"]
    assert view(payload, now="2026-10-03T06:00:00")["temperature"] == "最高25℃／最低—"


def test_missing_values_show_dash():
    payload = forecast_payload()
    payload[0]["timeSeries"][1]["areas"][0]["pops"] = ["", "", "", "", ""]
    payload[0]["timeSeries"][0]["areas"][0]["weathers"][0] = ""
    result = view(payload)
    assert result["headline"] == "—" and result["pops"] == [{"label": "18–24時", "text": "—"}]


def test_other_region_uses_its_own_codes():
    result = view(forecast_payload(), KITAKYUSHU, now="2026-10-04T13:00:00")
    assert result["temperature"] == "最高23℃／最低18℃"
    assert result["pops"][0]["text"] == "35%"
    assert result["full_text"] == "くもり 夕方 から 雨"


def test_codes_missing_from_response_are_not_replaced_by_other_area():
    with pytest.raises(WeatherAreaNotFoundError, match="999999"):
        parse_forecast(forecast_payload(), WeatherConfig("x", "400000", "999999", "82182"))
    with pytest.raises(WeatherAreaNotFoundError, match="00000"):
        parse_forecast(forecast_payload(), WeatherConfig("x", "400000", "400010", "00000"))
    with pytest.raises(WeatherAreaNotFoundError):
        parse_forecast(forecast_payload(), WeatherConfig("x", "400000", "400000", "82182"))


@pytest.mark.parametrize("payload", [{}, [], [{"timeSeries": "x"}], [{"reportDatetime": "bad", "timeSeries": []}]])
def test_invalid_structure(payload):
    with pytest.raises(WeatherDataError):
        parse_forecast(payload, FUKUOKA)


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda payload: payload[0]["timeSeries"][0].update(timeDefines=None),
        lambda payload: payload[0]["timeSeries"][0].update(timeDefines="2026-10-03T17:00:00+09:00"),
        lambda payload: payload[0]["timeSeries"][0].update(timeDefines=["not-a-date"]),
        lambda payload: payload[0]["timeSeries"][0].update(timeDefines=["2026-10-03T17:00:00"]),
        lambda payload: payload[0]["timeSeries"][0]["areas"][0]["area"].update(code=400010),
        lambda payload: payload[0]["timeSeries"][0]["areas"][0].update(weathers=[{}]),
    ],
    ids=["null-times", "non-array-times", "invalid-time", "timezone-missing", "invalid-area-code", "invalid-values"],
)
def test_malformed_response_structures_are_weather_data_errors(corrupt):
    payload = forecast_payload()
    corrupt(payload)
    with pytest.raises(WeatherDataError):
        parse_forecast(payload, FUKUOKA)


VALID_TOML = """
[weather]
display_name = "北九州地方"
office_code = "400000"
weather_area_code = "400020"
temperature_point_code = "82056"
"""


def test_config_valid_and_url_is_built_from_codes():
    config = parse_weather_config(VALID_TOML)
    assert config == KITAKYUSHU
    assert config.forecast_url == "https://www.jma.go.jp/bosai/forecast/data/forecast/400000.json"


@pytest.mark.parametrize(
    "text",
    [
        "[weather",
        "[weather]\ndisplay_name='a'",
        VALID_TOML.replace('"400000"', '"4000"'),
        VALID_TOML.replace('"82056"', '"8205a"'),
        VALID_TOML.replace('"82056"', "82056"),
        VALID_TOML.replace('display_name = "北九州地方"', 'display_name = ""'),
        VALID_TOML + 'url = "https://example.com/x.json"\n',
        VALID_TOML + "[other]\na = 1\n",
    ],
)
def test_config_rejects_invalid(text):
    with pytest.raises(WeatherConfigError):
        parse_weather_config(text)


def test_config_file_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(weather_service, "DEFAULT_STATE_DIR", tmp_path / "absent")
    monkeypatch.delenv("OFFICE_TERMINAL_WEATHER_CONFIG_PATH", raising=False)
    assert load_weather_config() == FUKUOKA
    configured = tmp_path / "weather.toml"
    configured.write_text(VALID_TOML, encoding="utf-8")
    monkeypatch.setenv("OFFICE_TERMINAL_WEATHER_CONFIG_PATH", str(configured))
    assert load_weather_config() == KITAKYUSHU
    monkeypatch.setenv("OFFICE_TERMINAL_WEATHER_CONFIG_PATH", str(tmp_path / "missing.toml"))
    with pytest.raises(WeatherConfigError):
        load_weather_config()


def make_service(handler, config=FUKUOKA, **kwargs):
    return WeatherService(config, transport=httpx.MockTransport(handler), refresh_interval=3600, **kwargs)


def json_handler(payload, calls=None):
    def handler(request):
        if calls is not None:
            calls.append(str(request.url))
        return httpx.Response(200, json=payload)

    return handler


NOW = at("2026-10-03T20:41:00")


async def started(service):
    await service.start()
    service._task.cancel()
    try:
        await service._task
    except asyncio.CancelledError:
        pass
    service._task = None


def run(coro):
    return asyncio.run(coro)


def test_refresh_success_requests_constructed_url_and_clears_loading():
    calls = []

    async def scenario():
        service = make_service(json_handler(forecast_payload(), calls))
        assert service.view(NOW)["message"] == "天気情報を取得中"
        await started(service)
        assert await service.refresh()
        result = service.view(NOW)
        await service.stop()
        return result

    result = run(scenario())
    assert calls == ["https://www.jma.go.jp/bosai/forecast/data/forecast/400000.json"]
    assert result["status"] == "ok" and result["notice"] == ""
    assert "source" not in result and "福岡管区気象台" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=request)),
        lambda request: httpx.Response(200, content=b"{not json"),
        lambda request: httpx.Response(200, json={"unexpected": True}),
        lambda request: httpx.Response(503),
    ],
    ids=["timeout", "invalid-json", "bad-structure", "http-error"],
)
def test_failure_without_cache_then_recovery(handler):
    state = {"ok": False}

    def switching(request):
        return httpx.Response(200, json=forecast_payload()) if state["ok"] else handler(request)

    async def scenario():
        service = make_service(switching)
        await started(service)
        assert not await service.refresh()
        failed = service.view(NOW)
        state["ok"] = True
        assert await service.refresh()
        recovered = service.view(NOW)
        await service.stop()
        return failed, recovered

    failed, recovered = run(scenario())
    assert failed["message"] == "天気情報を取得できません" and "temperature" not in failed
    assert recovered["status"] == "ok" and recovered["notice"] == ""


@pytest.mark.parametrize(
    "bad_defines",
    [
        None,
        "2026-10-03T17:00:00+09:00",
        ["not-a-date"],
        ["2026-10-03T17:00:00"],
    ],
    ids=["null", "not-array", "invalid-date", "timezone-missing"],
)
def test_malformed_time_defines_initial_failure_and_recovery(bad_defines):
    state = {"valid": False}

    def handler(request):
        payload = forecast_payload()
        if not state["valid"]:
            payload[0]["timeSeries"][0]["timeDefines"] = bad_defines
        return httpx.Response(200, json=payload)

    async def scenario():
        service = make_service(handler)
        await started(service)
        assert not await service.refresh()
        initial_failure = service.view(NOW)
        assert initial_failure["message"] == "天気情報を取得できません"
        assert service.last_error

        state["valid"] = True
        assert await service.refresh()
        assert service.last_error is None
        state["valid"] = False
        assert not await service.refresh()
        retained_failure = service.view(NOW)

        state["valid"] = True
        assert await service.refresh()
        recovered = service.view(NOW)
        await service.stop()
        return initial_failure, retained_failure, recovered

    initial_failure, retained_failure, recovered = run(scenario())
    assert "temperature" not in initial_failure
    assert retained_failure["status"] == "ok"
    assert retained_failure["notice"] == "更新できていません"
    assert retained_failure["issued"] == "17:00発表"
    assert recovered["status"] == "ok" and recovered["notice"] == ""


def test_previous_data_kept_on_failure_and_old_day_not_shown_as_today():
    state = {"ok": True}

    def handler(request):
        return httpx.Response(200, json=forecast_payload()) if state["ok"] else httpx.Response(500)

    async def scenario():
        service = make_service(handler)
        await started(service)
        await service.refresh()
        state["ok"] = False
        await service.refresh()
        same_day = service.view(NOW)
        next_day = service.view(at("2026-10-04T00:30:00"))
        await service.stop()
        return same_day, next_day

    same_day, next_day = run(scenario())
    assert same_day["status"] == "ok" and same_day["notice"] == "更新できていません"
    assert same_day["issued"] == "17:00発表"
    assert next_day["status"] == "stale" and "temperature" not in next_day
    assert next_day["issued"] == "10月3日 17:00発表"


def test_area_not_found_and_404_report_config_problem(caplog):
    async def scenario(handler, config):
        service = make_service(handler, config)
        await started(service)
        with caplog.at_level(logging.ERROR):
            await service.refresh()
        result = service.view(NOW)
        await service.stop()
        return result

    wrong_area = run(scenario(json_handler(forecast_payload()), WeatherConfig("福岡地方", "400000", "400099", "82182")))
    assert wrong_area["message"] == "地域設定を確認してください" and "temperature" not in wrong_area
    assert "400099" in caplog.text
    caplog.clear()
    missing_office = run(scenario(lambda request: httpx.Response(404), WeatherConfig("x", "999999", "400010", "82182")))
    assert missing_office["message"] == "地域設定を確認してください"
    assert "999999" in caplog.text


def test_concurrent_refresh_is_deduplicated_and_views_do_not_fetch():
    calls = []

    async def scenario():
        gate = asyncio.Event()

        class SlowTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                calls.append(1)
                await gate.wait()
                return httpx.Response(200, json=forecast_payload())

        service = WeatherService(FUKUOKA, transport=SlowTransport(), refresh_interval=3600)
        await started(service)
        first = asyncio.create_task(service.refresh())
        await asyncio.sleep(0.01)
        skipped = await service.refresh()
        gate.set()
        done = await first
        for _ in range(50):
            service.view(NOW)
        await service.stop()
        return skipped, done

    skipped, done = run(scenario())
    assert (skipped, done) == (False, True) and len(calls) == 1


def test_request_timeout_is_configured_and_stop_closes_client():
    async def scenario():
        service = make_service(json_handler(forecast_payload()), timeout=3.0)
        await service.start()
        client = service._client
        timeout = client.timeout.read
        await asyncio.sleep(0.05)
        await service.stop()
        return timeout, client.is_closed, service._task

    timeout, closed, task = run(scenario())
    assert timeout == 3.0 and closed and task is None


def test_periodic_refresh_runs_in_background():
    calls = []

    async def scenario():
        service = WeatherService(
            FUKUOKA, transport=httpx.MockTransport(json_handler(forecast_payload(), calls)), refresh_interval=0.02
        )
        await service.start()
        assert service.snapshot is None
        await asyncio.sleep(0.15)
        await service.stop()

    run(scenario())
    assert len(calls) >= 3


def test_unexpected_refresh_error_is_recorded_and_periodic_task_recovers():
    async def scenario():
        service = WeatherService(
            FUKUOKA,
            transport=httpx.MockTransport(json_handler(forecast_payload())),
            refresh_interval=0.02,
        )
        original_refresh = service.refresh
        failed_once = asyncio.Event()
        attempts = 0

        async def unexpected_once():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                failed_once.set()
                raise RuntimeError("unexpected failure")
            return await original_refresh()

        service.refresh = unexpected_once
        await service.start()
        await failed_once.wait()
        await asyncio.sleep(0)
        recorded_error = service.last_error
        for _ in range(20):
            if service.last_error is None and service.request_count:
                break
            await asyncio.sleep(0.01)
        stopped_task = service._task
        await service.stop()
        return recorded_error, service.last_error, service.request_count, attempts, stopped_task

    recorded_error, last_error, request_count, attempts, task = run(scenario())
    assert recorded_error == "RuntimeError: unexpected failure"
    assert last_error is None and request_count == 1 and attempts >= 2
    assert task is not None and task.done()


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_TERMINAL_OPERATIONS_CONFIG_PATH", str(tmp_path / "operations.json"))
    monkeypatch.setenv("OFFICE_TERMINAL_BACKUP_STATE_PATH", str(tmp_path / "backup-state.json"))
    monkeypatch.setenv("OFFICE_TERMINAL_RUNTIME_LOCK_PATH", str(tmp_path / "runtime.lock"))
    monkeypatch.delenv("OFFICE_TERMINAL_BACKUP_PATH", raising=False)
    return tmp_path


def test_app_serves_cached_weather_without_extra_requests_and_survives_bad_config(app_env, monkeypatch):
    bad = app_env / "weather.toml"
    bad.write_text("[weather", encoding="utf-8")
    monkeypatch.setenv("OFFICE_TERMINAL_WEATHER_CONFIG_PATH", str(bad))
    with TestClient(create_app(app_env / "app.db")) as client:
        page = client.get("/")
        assert page.status_code == 200 and "地域設定を確認してください" in page.text
        assert client.get("/api/weather").json()["status"] == "config_error"
        assert client.get("/menu").status_code == 200

    monkeypatch.setenv("OFFICE_TERMINAL_WEATHER_CONFIG_PATH", str(app_env / "none.toml"))
    with TestClient(create_app(app_env / "app2.db")) as client:
        assert client.get("/api/weather").json()["message"] == "地域設定を確認してください"


def test_app_weather_api_shares_one_cache(app_env, monkeypatch):
    calls = []
    monkeypatch.setenv("OFFICE_TERMINAL_WEATHER_ENABLED", "1")
    original = WeatherService.from_environment

    def build(**kwargs):
        return original(transport=httpx.MockTransport(json_handler(forecast_payload(), calls)), **kwargs)

    monkeypatch.setattr(WeatherService, "from_environment", staticmethod(build))
    with TestClient(create_app(app_env / "app.db")) as client:
        for _ in range(100):
            if client.get("/api/weather").json()["status"] != "loading":
                break
            import time

            time.sleep(0.02)
        for _ in range(20):
            assert client.get("/").status_code == 200
            client.get("/api/weather")
        data = client.get("/api/weather").json()
    assert data["status"] in {"ok", "stale"}
    assert len(calls) == 1


def test_example_config_matches_default():
    example = Path(__file__).parents[1] / "docs" / "weather.example.toml"
    assert parse_weather_config(example.read_text(encoding="utf-8")) == FUKUOKA


def temperature_payload(*, defines, temps, point="82182", key="temps", reported="2026-10-03T05:00:00+09:00"):
    payload = forecast_payload()
    payload[0]["reportDatetime"] = reported
    series = payload[0]["timeSeries"][2]
    series["timeDefines"] = defines
    series["areas"][0] = {"area": {"name": "福岡", "code": point}, key: temps}
    return payload


MORNING = ["2026-10-03T09:00:00+09:00", "2026-10-04T00:00:00+09:00", "2026-10-04T09:00:00+09:00"]


def test_normal_temperatures_use_time_labels_not_magnitude():
    payload = temperature_payload(defines=MORNING, temps=["25", "19", "24"])
    assert view(payload, now="2026-10-03T10:00:00")["temperature"] == "最高25℃／最低—"
    both = temperature_payload(
        defines=["2026-10-03T00:00:00+09:00", "2026-10-03T09:00:00+09:00"], temps=["26", "18"]
    )
    # 00:00が最低、09:00が最高。値の大小では判定しない（最低26・最高18の異常値でもラベルどおり）
    assert view(both, now="2026-10-03T10:00:00")["temperature"] == "最高18℃／最低26℃"


@pytest.mark.parametrize(
    "temps, expected",
    [(["0", "-3"], "最高-3℃／最低0℃"), (["-5", "0"], "最高0℃／最低-5℃"), (["0", "0"], "最高0℃／最低0℃")],
)
def test_zero_and_negative_temperatures(temps, expected):
    payload = temperature_payload(
        defines=["2026-10-03T00:00:00+09:00", "2026-10-03T09:00:00+09:00"], temps=temps
    )
    assert view(payload, now="2026-10-03T10:00:00")["temperature"] == expected


def test_one_sided_and_missing_temperatures():
    low_only = temperature_payload(
        defines=["2026-10-03T00:00:00+09:00", "2026-10-03T09:00:00+09:00"], temps=["12", ""]
    )
    assert view(low_only, now="2026-10-03T10:00:00")["temperature"] == "最高—／最低12℃"
    neither = temperature_payload(
        defines=["2026-10-03T00:00:00+09:00", "2026-10-03T09:00:00+09:00"], temps=["", "x"]
    )
    assert view(neither, now="2026-10-03T10:00:00")["temperature"] == "本日の気温予報なし"


def test_evening_issue_has_no_today_temperature_and_never_uses_tomorrow():
    evening = forecast_payload()
    assert view(evening)["temperature"] == "本日の気温予報なし"
    assert "24" not in view(evening)["temperature"] and "19" not in view(evening)["temperature"]


def test_weekly_series_is_matched_by_date():
    payload = forecast_payload()
    payload[1]["timeSeries"][1]["areas"][0]["tempsMax"] = ["31", "27"]
    payload[1]["timeSeries"][1]["areas"][0]["tempsMin"] = ["22", "20"]
    payload[1]["timeSeries"][1]["timeDefines"] = ["2026-10-03T00:00:00+09:00", "2026-10-04T00:00:00+09:00"]
    assert view(payload, now="2026-10-03T10:00:00")["temperature"] == "最高31℃／最低22℃"
    assert view(payload, now="2026-10-04T10:00:00")["temperature"] == "最高24℃／最低19℃"


def test_temperature_point_mismatch_is_not_substituted():
    payload = temperature_payload(defines=MORNING, temps=["25", "19", "24"], point="82056")
    payload[1]["timeSeries"].pop()
    with pytest.raises(WeatherAreaNotFoundError):
        parse_forecast(payload, FUKUOKA)


def test_date_boundary_for_temperatures():
    payload = temperature_payload(defines=MORNING, temps=["25", "19", "24"])
    assert view(payload, now="2026-10-03T23:59:00")["temperature"] == "最高25℃／最低—"
    assert view(payload, now="2026-10-04T00:00:00")["temperature"] == "最高24℃／最低19℃"


def test_retained_temperatures_are_kept_with_issue_time_and_scoped():
    state = {"payload": temperature_payload(defines=MORNING, temps=["25", "19", "24"])}

    def handler(request):
        return httpx.Response(200, json=state["payload"])

    async def scenario():
        service = make_service(handler, clock=lambda: at("2026-10-03T12:00:00"))
        await started(service)
        await service.refresh()
        state["payload"] = forecast_payload()  # 17:00発表: 今日の気温を含まない
        await service.refresh()
        retained = service.view(at("2026-10-03T20:41:00"))
        tomorrow = service.view(at("2026-10-04T08:00:00"))
        other = WeatherService(KITAKYUSHU, transport=httpx.MockTransport(handler))
        other._temperatures = service._temperatures
        await service.stop()
        return retained, tomorrow, other.today_temperatures(at("2026-10-03T12:00:00").date())

    retained, tomorrow, other = run(scenario())
    assert retained["temperature"] == "最高25℃／最低—"
    assert retained["issued"] == "17:00発表" and retained["temperature_note"] == "気温は05:00発表"
    assert tomorrow["temperature"] == "最高24℃／最低19℃"  # 明日の日付になれば保持した当日値ではなく同日の値
    assert other == {}  # 地点が違えば流用しない


def test_long_text_is_summarized_and_detail_view_has_source():
    payload = forecast_payload()
    payload[0]["timeSeries"][0]["areas"][0]["weathers"][0] = "くもり\u3000時々\u3000雨\u3000で\u3000雷を伴い\u3000激しく降る"

    async def scenario():
        service = make_service(json_handler(payload))
        await started(service)
        await service.refresh()
        result = service.view(NOW), service.detail_view(NOW)
        await service.stop()
        return result

    card, detail = run(scenario())
    assert card["headline"] == "くもり ほか" and "full_text" not in card
    assert detail["full_text"] == "くもり 時々 雨 で 雷を伴い 激しく降る"
    assert detail["source"] == "出典：気象庁（加工して表示）" and detail["office_name"] == "福岡管区気象台"


def test_settings_page_shows_source_and_top_does_not(app_env, monkeypatch):
    with TestClient(create_app(app_env / "app.db")) as client:
        assert "出典：気象庁" not in client.get("/").text
        assert "福岡管区気象台" not in client.get("/").text
        assert "出典：気象庁（加工して表示）" in client.get("/settings").text


def test_top_page_renders_card_lines(app_env, monkeypatch):
    from office_terminal.web import routes

    async def scenario():
        service = make_service(json_handler(forecast_payload()))
        await started(service)
        await service.refresh()
        result = service.view(NOW)
        await service.stop()
        return result

    card = run(scenario())
    monkeypatch.setattr(routes, "weather_view", lambda request: card)
    with TestClient(create_app(app_env / "app.db")) as client:
        text = client.get("/").text
    for expected in ("福岡地方", "晴れ 夜遅く くもり", "本日の気温予報なし", "18–24時 0%", "17:00発表"):
        assert expected in text
    assert "built-in method" not in text
