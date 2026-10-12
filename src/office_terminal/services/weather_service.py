"""気象庁の公開予報JSONを取得し、TOP画面用の「今日の予報」に整形する。

取得・更新はアプリ内で1つだけ共有し、画面表示や接続台数では外部アクセスしない。
"""

import asyncio
import json
import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from office_terminal.services.backup_service import DEFAULT_STATE_DIR

logger = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")
FORECAST_URL_TEMPLATE = "https://www.jma.go.jp/bosai/forecast/data/forecast/{office_code}.json"
REFRESH_INTERVAL_SECONDS = 30 * 60
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 2_000_000
POP_BAND_HOURS = 6
SOURCE_LABEL = "出典：気象庁（加工して表示）"
CONFIG_ERROR_MESSAGE = "地域設定を確認してください"
LOADING_MESSAGE = "天気情報を取得中"
UNAVAILABLE_MESSAGE = "天気情報を取得できません"
MISSING = "—"

DEFAULT_REGION_NAME = "福岡地方"
DEFAULT_OFFICE_CODE = "400000"
DEFAULT_WEATHER_AREA_CODE = "400010"
DEFAULT_TEMPERATURE_POINT_CODE = "82182"

_CONFIG_KEYS = {"display_name", "office_code", "weather_area_code", "temperature_point_code"}
_INTEGER_PATTERN = re.compile(r"-?\d+")


class WeatherConfigError(Exception):
    """天気設定ファイルの構文・値が不正。"""


class WeatherDataError(Exception):
    """公開データが想定した形式ではない。"""


class WeatherAreaNotFoundError(Exception):
    """設定した地域・地点コードが応答データに存在しない。"""


@dataclass(frozen=True)
class WeatherConfig:
    display_name: str = DEFAULT_REGION_NAME
    office_code: str = DEFAULT_OFFICE_CODE
    weather_area_code: str = DEFAULT_WEATHER_AREA_CODE
    temperature_point_code: str = DEFAULT_TEMPERATURE_POINT_CODE

    @property
    def forecast_url(self) -> str:
        return FORECAST_URL_TEMPLATE.format(office_code=self.office_code)


def weather_config_path() -> Path:
    configured = os.environ.get("OFFICE_TERMINAL_WEATHER_CONFIG_PATH")
    return Path(configured).expanduser() if configured else DEFAULT_STATE_DIR / "weather.toml"


def _code(table: dict[str, Any], key: str, pattern: str, description: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise WeatherConfigError(f"{key} は{description}の文字列で指定してください（現在値: {value!r}）")
    return value


def parse_weather_config(text: str) -> WeatherConfig:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise WeatherConfigError(f"TOMLの構文が不正です: {error}") from error
    unknown_sections = set(document) - {"weather"}
    table = document.get("weather")
    if unknown_sections or not isinstance(table, dict):
        raise WeatherConfigError("[weather] テーブルだけを記載してください")
    unknown_keys = set(table) - _CONFIG_KEYS
    if unknown_keys:
        raise WeatherConfigError(f"未知の項目があります: {', '.join(sorted(unknown_keys))}")
    missing_keys = _CONFIG_KEYS - set(table)
    if missing_keys:
        raise WeatherConfigError(f"必須項目がありません: {', '.join(sorted(missing_keys))}")
    display_name = table["display_name"]
    if (
        not isinstance(display_name, str)
        or not display_name.strip()
        or len(display_name) > 30
        or any(ord(character) < 32 for character in display_name)
    ):
        raise WeatherConfigError("display_name は1〜30文字の文字列で指定してください")
    return WeatherConfig(
        display_name=display_name.strip(),
        office_code=_code(table, "office_code", r"\d{6}", "6桁の数字"),
        weather_area_code=_code(table, "weather_area_code", r"\d{6}", "6桁の数字"),
        temperature_point_code=_code(table, "temperature_point_code", r"\d{5}", "5桁の数字"),
    )


def load_weather_config(path: Path | None = None) -> WeatherConfig:
    """明示指定(環境変数)のファイルが無い場合はエラー。既定の場所に無ければ福岡の初期設定。"""
    explicit = path is not None or bool(os.environ.get("OFFICE_TERMINAL_WEATHER_CONFIG_PATH"))
    file_path = path or weather_config_path()
    try:
        text = file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if explicit:
            raise WeatherConfigError(f"設定ファイルが見つかりません: {file_path}") from None
        return WeatherConfig()
    except (OSError, UnicodeDecodeError) as error:
        raise WeatherConfigError(f"設定ファイルを読めません: {file_path}: {error}") from error
    return parse_weather_config(text)


@dataclass(frozen=True)
class DailyWeather:
    day: date
    text: str
    code: str | None


@dataclass(frozen=True)
class PopBand:
    start: datetime
    end: datetime
    percent: int | None


@dataclass(frozen=True)
class ForecastSnapshot:
    office_name: str
    reported_at: datetime
    weathers: tuple[DailyWeather, ...] = ()
    pop_bands: tuple[PopBand, ...] = ()
    temperatures: dict[date, dict[str, int | None]] = field(default_factory=dict)


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise WeatherDataError(f"日時が文字列ではありません: {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise WeatherDataError(f"日時を解釈できません: {value!r}") from error
    if parsed.tzinfo is None:
        raise WeatherDataError(f"タイムゾーンがない日時です: {value!r}")
    return parsed.astimezone(JST)


def _to_int(value: Any) -> int | None:
    if isinstance(value, str) and _INTEGER_PATTERN.fullmatch(value.strip()):
        return int(value.strip())
    return None


def _set_temperature(
    temperatures: dict[date, dict[str, int | None]], day: date, kind: str, value: int | None
) -> None:
    if value is not None and temperatures.setdefault(day, {}).get(kind) is None:
        temperatures[day][kind] = value


def _values(area: dict[str, Any], key: str, length: int) -> list[Any]:
    values = area.get(key)
    if not isinstance(values, list):
        raise WeatherDataError(f"{key} が配列ではありません")
    if any(value is not None and not isinstance(value, str) for value in values):
        raise WeatherDataError(f"{key} の値が文字列ではありません")
    return values + [None] * max(0, length - len(values))


def parse_forecast(payload: Any, config: WeatherConfig) -> ForecastSnapshot:
    """地域コードと日時で値を対応付ける。配列位置は使わない。"""
    if not isinstance(payload, list) or not payload:
        raise WeatherDataError("予報データが空、または配列ではありません")
    reported_at: datetime | None = None
    office_name = ""
    weathers: dict[date, DailyWeather] = {}
    pop_bands: dict[datetime, PopBand] = {}
    temperatures: dict[date, dict[str, int | None]] = {}
    weather_area_found = False
    point_found = False

    for report in payload:
        if not isinstance(report, dict) or not isinstance(report.get("timeSeries"), list):
            raise WeatherDataError("report/timeSeries の形式が不正です")
        if reported_at is None:
            reported_at = _parse_time(report.get("reportDatetime"))
            raw_office_name = report.get("publishingOffice", "")
            if not isinstance(raw_office_name, str):
                raise WeatherDataError("publishingOffice が文字列ではありません")
            office_name = raw_office_name
        for series in report["timeSeries"]:
            if not isinstance(series, dict) or not isinstance(series.get("areas"), list):
                raise WeatherDataError("timeSeries の形式が不正です")
            raw_defines = series.get("timeDefines")
            if not isinstance(raw_defines, list):
                raise WeatherDataError("timeDefines が配列ではありません")
            defines = [_parse_time(item) for item in raw_defines]
            for area in series["areas"]:
                if not isinstance(area, dict) or not isinstance(area.get("area"), dict):
                    raise WeatherDataError("area の形式が不正です")
                code = area["area"].get("code")
                if not isinstance(code, str):
                    raise WeatherDataError("area/code が文字列ではありません")
                if code == config.weather_area_code:
                    if "weathers" in area:
                        weather_area_found = True
                        texts = _values(area, "weathers", len(defines))
                        codes = area.get("weatherCodes", [])
                        if not isinstance(codes, list) or any(
                            value is not None and not isinstance(value, str) for value in codes
                        ):
                            raise WeatherDataError("weatherCodes の形式が不正です")
                        for index, moment in enumerate(defines):
                            text = texts[index]
                            if isinstance(text, str) and text.strip():
                                weathers.setdefault(
                                    moment.date(),
                                    DailyWeather(
                                        moment.date(),
                                        text,
                                        codes[index] if index < len(codes) and isinstance(codes[index], str) else None,
                                    ),
                                )
                    steps = {b - a for a, b in zip(defines, defines[1:])}
                    if "pops" in area and steps <= {timedelta(hours=POP_BAND_HOURS)}:
                        weather_area_found = True
                        pops = _values(area, "pops", len(defines))
                        for index, moment in enumerate(defines):
                            percent = _to_int(pops[index])
                            pop_bands.setdefault(
                                moment,
                                PopBand(
                                    moment,
                                    moment + timedelta(hours=POP_BAND_HOURS),
                                    percent if percent is not None and 0 <= percent <= 100 else None,
                                ),
                            )
                if code == config.temperature_point_code:
                    if "temps" in area:
                        point_found = True
                        temps = _values(area, "temps", len(defines))
                        for index, moment in enumerate(defines):
                            kind = {0: "min", 9: "max"}.get(moment.hour) if moment.minute == 0 else None
                            if kind:
                                _set_temperature(temperatures, moment.date(), kind, _to_int(temps[index]))
                    for key, kind in (("tempsMin", "min"), ("tempsMax", "max")):
                        if key in area:
                            point_found = True
                            temps = _values(area, key, len(defines))
                            for index, moment in enumerate(defines):
                                _set_temperature(temperatures, moment.date(), kind, _to_int(temps[index]))

    if reported_at is None:
        raise WeatherDataError("reportDatetime がありません")
    missing = []
    if not weather_area_found:
        missing.append(f"天気地域コード {config.weather_area_code}")
    if not point_found:
        missing.append(f"気温地点コード {config.temperature_point_code}")
    if missing:
        raise WeatherAreaNotFoundError(
            f"{config.office_code} の応答に {' / '.join(missing)} が見つかりません"
        )
    return ForecastSnapshot(
        office_name=office_name,
        reported_at=reported_at,
        weathers=tuple(sorted(weathers.values(), key=lambda item: item.day)),
        pop_bands=tuple(sorted(pop_bands.values(), key=lambda band: band.start)),
        temperatures=temperatures,
    )


def _icon(code: str | None, text: str) -> str:
    leading = code[0] if code else ""
    if leading == "1":
        return "☀"
    if leading == "2":
        return "☁"
    if leading == "3":
        return "☂"
    if leading == "4":
        return "❄"
    if "雪" in text:
        return "❄"
    if "雨" in text:
        return "☂"
    if "くもり" in text or "曇" in text:
        return "☁"
    return "☀" if "晴" in text else ""


SUMMARY_MAX_CHARS = 12
NO_TEMPERATURE_MESSAGE = "本日の気温予報なし"


@dataclass(frozen=True)
class TemperatureRecord:
    value: int
    reported_at: datetime


def _normalize_text(text: str) -> str:
    return " ".join(text.replace("\u3000", " ").split())


def _summarize_weather(text: str) -> str:
    normalized = _normalize_text(text)
    if len(normalized) <= SUMMARY_MAX_CHARS:
        return normalized
    return f"{normalized.split(' ')[0]} ほか"


def _format_issue(moment: datetime, today: date | None = None) -> str:
    if today is not None and moment.date() != today:
        return f"{moment.month}月{moment.day}日 {moment:%H:%M}発表"
    return f"{moment:%H:%M}発表"


def _band_label(band: PopBand) -> str:
    end_hour = 24 if band.end.hour == 0 else band.end.hour
    return f"{band.start.hour}–{end_hour}時"


def snapshot_temperatures(snapshot: ForecastSnapshot) -> dict[date, dict[str, TemperatureRecord]]:
    return {
        day: {
            kind: TemperatureRecord(value, snapshot.reported_at)
            for kind, value in values.items()
            if value is not None
        }
        for day, values in snapshot.temperatures.items()
    }


def build_today_view(
    snapshot: ForecastSnapshot,
    config: WeatherConfig,
    now: datetime,
    temperatures: dict[str, TemperatureRecord] | None = None,
) -> dict[str, Any]:
    """今日(日本時間)の値だけを返す。temperatures は同一地点・同一日付の保持値。"""
    now = now.astimezone(JST)
    today = now.date()
    weather = next((item for item in snapshot.weathers if item.day == today), None)
    full_text = _normalize_text(weather.text) if weather else ""
    if temperatures is None:
        temperatures = snapshot_temperatures(snapshot).get(today, {})

    high, low = temperatures.get("max"), temperatures.get("min")
    if high is None and low is None:
        temperature = NO_TEMPERATURE_MESSAGE
    else:
        temperature = (
            f"最高{high.value if high else MISSING}{'℃' if high else ''}"
            f"／最低{low.value if low else MISSING}{'℃' if low else ''}"
        )
    older = sorted({r.reported_at for r in (high, low) if r and r.reported_at != snapshot.reported_at})

    midnight_end = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=JST)
    remaining = [
        band
        for band in snapshot.pop_bands
        if band.start.date() == today and band.end > now and band.start < midnight_end
    ]
    pops = [
        {"label": _band_label(band), "text": f"{band.percent}%" if band.percent is not None else MISSING}
        for band in remaining
    ] or [{"label": "今日の残り時間帯", "text": MISSING}]
    return {
        "headline": _summarize_weather(full_text) if full_text else MISSING,
        "full_text": full_text or MISSING,
        "icon": _icon(weather.code if weather else None, weather.text if weather else ""),
        "temperature": temperature,
        "temperature_note": "気温は" + "・".join(_format_issue(m, today) for m in older) if older else "",
        "pop_text": f"{pops[0]['label']} {pops[0]['text']}",
        "pops": pops,
        "issued": _format_issue(snapshot.reported_at, today),
        "office_name": snapshot.office_name,
        "reported_at": snapshot.reported_at.isoformat(),
    }


class WeatherService:
    """予報を1か所でキャッシュし、バックグラウンドで定期更新する。"""

    def __init__(
        self,
        config: WeatherConfig | None,
        *,
        config_error: str | None = None,
        enabled: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
        refresh_interval: float = REFRESH_INTERVAL_SECONDS,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        clock=None,
    ) -> None:
        self.config = config or WeatherConfig()
        self.config_error = config_error
        self.enabled = enabled
        self._transport = transport
        self._refresh_interval = refresh_interval
        self._timeout = timeout
        self._clock = clock or (lambda: datetime.now(JST))
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.snapshot: ForecastSnapshot | None = None
        self.fetched_at: datetime | None = None
        self.last_error: str | None = None
        self.area_problem = False
        self.request_count = 0
        self._temperatures: dict[tuple[str, str, date], dict[str, TemperatureRecord]] = {}

    @classmethod
    def from_environment(cls, **kwargs: Any) -> "WeatherService":
        enabled = os.environ.get("OFFICE_TERMINAL_WEATHER_ENABLED", "1") != "0"
        try:
            return cls(load_weather_config(), enabled=enabled, **kwargs)
        except WeatherConfigError as error:
            logger.error("天気設定が不正です。天気欄は取得せず、他機能は通常どおり起動します: %s", error)
            return cls(None, config_error=str(error), enabled=enabled, **kwargs)

    async def start(self) -> None:
        if self._task is not None or not self.enabled or self.config_error:
            return
        self._client = httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(self._timeout),
            headers={"User-Agent": "office-terminal/0.1 (+weather)"},
            follow_redirects=False,
        )
        self._task = asyncio.create_task(self._run(), name="weather-refresh")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _run(self) -> None:
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception("天気の更新で想定外のエラーが発生しました")
                self.last_error = f"{type(error).__name__}: {error}"
            await asyncio.sleep(self._refresh_interval)

    async def refresh(self) -> bool:
        """取得中の重複実行は行わない。成功時 True。"""
        if self._client is None or self._lock.locked():
            return False
        async with self._lock:
            self.request_count += 1
            try:
                response = await self._client.get(self.config.forecast_url)
                if response.status_code == 404:
                    raise WeatherAreaNotFoundError(
                        f"府県予報区コード {self.config.office_code} の予報が存在しません (HTTP 404)"
                    )
                response.raise_for_status()
                if len(response.content) > MAX_RESPONSE_BYTES:
                    raise WeatherDataError("応答が大きすぎます")
                try:
                    payload = json.loads(response.content)
                except ValueError as error:
                    raise WeatherDataError(f"JSONを解釈できません: {error}") from error
                snapshot = parse_forecast(payload, self.config)
                self._merge_temperatures(snapshot)
            except WeatherAreaNotFoundError as error:
                logger.error("天気の地域設定を確認してください: %s", error)
                self.snapshot = None
                self.area_problem = True
                self.last_error = str(error)
                return False
            except (httpx.HTTPError, WeatherDataError) as error:
                logger.warning("天気の更新に失敗しました: %s: %s", type(error).__name__, error)
                self.last_error = f"{type(error).__name__}: {error}"
                return False
            except Exception as error:
                logger.exception("天気の更新で想定外のエラーが発生しました")
                self.last_error = f"{type(error).__name__}: {error}"
                return False
            self.snapshot = snapshot
            self.fetched_at = self._clock()
            self.last_error = None
            self.area_problem = False
            return True

    def _merge_temperatures(self, snapshot: ForecastSnapshot) -> None:
        """同一の府県・地点・日付に限り、新しい発表の値で更新して保持する。"""
        today = self._clock().astimezone(JST).date()
        for key in [k for k in self._temperatures if k[2] < today - timedelta(days=1)]:
            del self._temperatures[key]
        for day, records in snapshot_temperatures(snapshot).items():
            stored = self._temperatures.setdefault(
                (self.config.office_code, self.config.temperature_point_code, day), {}
            )
            for kind, record in records.items():
                current = stored.get(kind)
                if current is None or record.reported_at >= current.reported_at:
                    stored[kind] = record

    def today_temperatures(self, today: date) -> dict[str, TemperatureRecord]:
        return dict(
            self._temperatures.get((self.config.office_code, self.config.temperature_point_code, today), {})
        )

    def view(self, now: datetime | None = None) -> dict[str, Any]:
        """TOP画面用。出典・気象台名・全文は含めない。"""
        full = self.detail_view(now)
        for key in ("source", "office_name", "full_text", "pops", "reported_at", "fetched_at", "last_error"):
            full.pop(key, None)
        return full

    def detail_view(self, now: datetime | None = None) -> dict[str, Any]:
        """設定画面用。出典・加工表記、全文、全時間帯、取得状況を含む。"""
        now = (now or self._clock()).astimezone(JST)
        base: dict[str, Any] = {
            "region": self.config.display_name,
            "source": SOURCE_LABEL,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else "",
            "last_error": self.last_error or "",
        }
        if self.config_error or self.area_problem:
            return {**base, "status": "config_error", "message": CONFIG_ERROR_MESSAGE}
        if self.snapshot is None:
            if self.last_error is None and self.enabled:
                return {**base, "status": "loading", "message": LOADING_MESSAGE}
            return {**base, "status": "unavailable", "message": UNAVAILABLE_MESSAGE}
        issued = _format_issue(self.snapshot.reported_at, now.date())
        failing = self.last_error is not None
        if failing and self.snapshot.reported_at.date() < now.date():
            return {
                **base,
                "status": "stale",
                "message": "更新できていません。古い予報は表示しません",
                "issued": issued,
            }
        view = {
            **base,
            "status": "ok",
            **build_today_view(self.snapshot, self.config, now, self.today_temperatures(now.date())),
        }
        view["notice"] = "更新できていません" if failing else ""
        return view
