import re
from datetime import date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from office_terminal.main import create_app
from office_terminal.web import routes
from office_terminal.services.attendance_service import (
    change_attendance,
    list_attendance_history,
)
from office_terminal.services.inspection_service import (
    ITEM_DEFINITIONS,
    InspectionSubmission,
    validate_and_save_inspection,
)
from office_terminal.storage.database import connect, initialize_database
from office_terminal.storage.repository import add_employee, import_employees

JST = ZoneInfo("Asia/Tokyo")
TODAY = date(2026, 10, 3)
RECORDED_AT = datetime(2026, 10, 3, 9, 15, 30, tzinfo=JST)


@pytest.fixture
def history_client(tmp_path, monkeypatch):
    database_path = tmp_path / "history.db"
    monkeypatch.setenv(
        "OFFICE_TERMINAL_OPERATIONS_CONFIG_PATH",
        str(tmp_path / "operations.json"),
    )
    monkeypatch.setenv(
        "OFFICE_TERMINAL_BACKUP_STATE_PATH",
        str(tmp_path / "backup-state.json"),
    )
    monkeypatch.setenv(
        "OFFICE_TERMINAL_RUNTIME_LOCK_PATH",
        str(tmp_path / "runtime.lock"),
    )
    initialize_database(database_path)
    add_employee(database_path, "001", "架空 山田")
    add_employee(database_path, "002", "架空 佐藤")
    with TestClient(create_app(database_path)) as client:
        yield client, database_path


def _change(database_path, employee_id, operation, changed_at=RECORDED_AT, key=None):
    return change_attendance(
        database_path,
        employee_id=employee_id,
        operation=operation,
        idempotency_key=key or str(uuid4()),
        changed_at=changed_at,
    )


def _insert_attendance_rows(database_path, count, *, changed_at=RECORDED_AT):
    with connect(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO attendance_history (
                employee_id, employee_name, previous_status, new_status,
                operation, idempotency_key, changed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "001",
                    "点検時の氏名",
                    "退勤済み",
                    "在室",
                    "出社",
                    str(uuid4()),
                    changed_at.isoformat(timespec="seconds"),
                )
                for _ in range(count)
            ],
        )


def _inspection(key):
    return InspectionSubmission(
        idempotency_key=key,
        employee_id="001",
        results={item["code"]: "confirmed" for item in ITEM_DEFINITIONS},
        reasons={item["code"]: "" for item in ITEM_DEFINITIONS},
        note="",
    )


def _freeze_route_clock(monkeypatch, instant):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(routes, "datetime", FrozenDateTime)


def test_daily_history_lists_valid_transitions_newest_first_in_japan_time(history_client):
    client, database_path = history_client
    for operation in ("出社", "離席", "戻り", "退勤"):
        _change(database_path, "001", operation)

    response = client.get("/attendance/history?target_date=2026-10-03")
    assert response.status_code == 200
    assert [
        response.text.index(f">{operation}</span>")
        for operation in ("退勤", "戻り", "離席", "出社")
    ] == sorted(
        response.text.index(f">{operation}</span>")
        for operation in ("退勤", "戻り", "離席", "出社")
    )
    assert "2026-10-03 09:15:30" in response.text
    assert "架空 山田" in response.text
    assert 'value="001"' in response.text
    assert "社員番号：" not in response.text
    record = response.text.split('<li class="attendance-history-record">', 1)[1].split(
        "</li>", 1
    )[0]
    assert "001" not in record
    assert "実際の入退室時刻とは限りません" in response.text


def test_initial_date_employee_filter_and_inactive_history_name_snapshot(
    history_client,
    monkeypatch,
):
    client, database_path = history_client
    _freeze_route_clock(
        monkeypatch,
        datetime(2026, 10, 3, 12, 0, tzinfo=JST),
    )
    _change(database_path, "001", "出社")
    _change(database_path, "002", "出社")

    import_employees(database_path, [("001", "現在の氏名", False)])

    page = client.get("/attendance/history")
    assert 'value="2026-10-03" required' in page.text
    assert 'value="001"' in page.text and "架空 山田" in page.text
    assert 'value="002"' in page.text and "架空 佐藤" in page.text
    assert "（001）" not in page.text and "（002）" not in page.text
    assert "現在の氏名" not in page.text

    filtered = client.get(
        "/attendance/history?target_date=2026-10-03&employee_id=001"
    )
    history_rows = filtered.text.split('<li class="attendance-history-record">', 1)[1].split(
        "</li>", 1
    )[0]
    assert "架空 山田" in history_rows
    assert "架空 佐藤" not in history_rows


def test_initial_date_uses_japan_date_across_utc_midnight(history_client, monkeypatch):
    client, _ = history_client
    _freeze_route_clock(
        monkeypatch,
        datetime(2026, 10, 3, 15, 30, tzinfo=ZoneInfo("UTC")),
    )

    page = client.get("/attendance/history")

    assert page.status_code == 200
    assert 'value="2026-10-04" required' in page.text


def test_same_timestamp_orders_by_history_id_descending(history_client):
    client, database_path = history_client
    _change(database_path, "001", "出社", key=str(uuid4()))
    _change(database_path, "002", "出社", key=str(uuid4()))
    _change(database_path, "001", "離席", key=str(uuid4()))

    rows, _, _ = list_attendance_history(
        database_path,
        target_date=TODAY,
        employee_id=None,
        page=1,
    )
    assert [row["operation"] for row in rows] == ["離席", "出社", "出社"]
    assert [row["attendance_history_id"] for row in rows] == sorted(
        (row["attendance_history_id"] for row in rows),
        reverse=True,
    )


def test_pagination_has_twenty_rows_and_preserves_filters(history_client):
    client, database_path = history_client
    _insert_attendance_rows(database_path, 25)

    first_page = client.get(
        "/attendance/history?target_date=2026-10-03&employee_id=001"
    )
    assert first_page.status_code == 200
    assert first_page.text.count('class="attendance-history-record"') == 20
    assert "25件中 1〜20件" in first_page.text
    assert (
        "/attendance/history?target_date=2026-10-03&amp;employee_id=001&amp;page=2"
        in first_page.text
    )

    second_page = client.get(
        "/attendance/history?target_date=2026-10-03&employee_id=001&page=2"
    )
    assert second_page.status_code == 200
    assert second_page.text.count('class="attendance-history-record"') == 5
    assert "25件中 21〜25件" in second_page.text
    assert (
        "/attendance/history?target_date=2026-10-03&amp;employee_id=001&amp;page=1"
        in second_page.text
    )


def test_inspection_checkout_history_is_labeled_without_exposing_key(history_client):
    client, database_path = history_client
    _change(database_path, "001", "出社")
    key = str(uuid4())
    validate_and_save_inspection(database_path, _inspection(key), RECORDED_AT)

    response = client.get("/attendance/history?target_date=2026-10-03")
    assert "退勤（点検保存）" in response.text
    assert f"inspection:{key}" not in response.text


def test_replayed_attendance_operation_has_one_history_entry(history_client):
    client, database_path = history_client
    operation_key = str(uuid4())
    _change(database_path, "001", "出社", key=operation_key)
    _change(database_path, "001", "出社", key=operation_key)

    response = client.get("/attendance/history?target_date=2026-10-03")
    assert response.text.count('class="attendance-history-record"') == 1


def test_history_read_does_not_change_database(history_client):
    client, database_path = history_client
    _change(database_path, "001", "出社")
    with connect(database_path) as connection:
        before_history = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM attendance_history ORDER BY attendance_history_id"
            )
        ]
        before_attendance = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM employee_attendance ORDER BY employee_id"
            )
        ]

    assert client.get("/attendance/history?target_date=2026-10-03").status_code == 200

    with connect(database_path) as connection:
        after_history = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM attendance_history ORDER BY attendance_history_id"
            )
        ]
        after_attendance = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM employee_attendance ORDER BY employee_id"
            )
        ]
    assert after_history == before_history
    assert after_attendance == before_attendance


def test_empty_day_and_invalid_filters_are_handled(history_client):
    client, _ = history_client
    empty = client.get("/attendance/history?target_date=2026-10-02")
    assert empty.status_code == 200
    assert "該当する出社・在席履歴はありません" in empty.text

    assert client.get("/attendance/history?target_date=not-a-date").status_code == 422
    assert client.get("/attendance/history?page=0").status_code == 422
    assert client.get("/attendance/history?employee_id=missing").status_code == 422
    assert client.get("/attendance/history?page=2").status_code == 404


def test_shared_and_pc_routes_keep_distinct_navigation_and_idle_modes(history_client):
    client, database_path = history_client
    _change(database_path, "001", "出社")

    kiosk_form = client.get("/attendees")
    pc_form = client.get("/pc/attendees")
    assert 'href="/attendance/history"' in kiosk_form.text
    assert 'href="/pc/attendance/history"' in pc_form.text

    kiosk_history = client.get("/attendance/history?target_date=2026-10-03")
    pc_history = client.get("/pc/attendance/history?target_date=2026-10-03")
    assert 'data-idle-return="disabled"' not in kiosk_history.text
    assert 'data-idle-return="disabled"' in pc_history.text
    assert 'action="/attendance/history"' in kiosk_history.text
    assert 'action="/pc/attendance/history"' in pc_history.text
    assert client.get("/records").status_code == 200
    history_menu = client.get("/records").text
    assert 'href="/history"' in history_menu
    assert 'href="/attendance/history"' in history_menu
    menu = client.get("/menu").text
    assert 'href="/records"' in menu
    assert ">履歴</span>" in menu
    assert client.get("/history").status_code == 200


def test_history_service_filters_dates_and_returns_latest_page(history_client):
    _, database_path = history_client
    _insert_attendance_rows(database_path, 22)
    next_day = datetime(2026, 10, 4, 0, 1, tzinfo=JST)
    _change(database_path, "002", "出社", changed_at=next_day)

    rows, count, employees = list_attendance_history(
        database_path,
        target_date=TODAY,
        employee_id="001",
        page=2,
    )
    assert count == 22
    assert len(rows) == 2
    assert rows[0]["attendance_history_id"] > rows[1]["attendance_history_id"]
    assert [employee["employee_id"] for employee in employees] == ["001", "002"]


def test_home_calendar_marks_arrivals_by_japan_date_without_changing_history(history_client, monkeypatch):
    client, database_path = history_client
    _freeze_route_clock(monkeypatch, datetime(2026, 10, 3, 12, 0, tzinfo=JST))

    empty_page = client.get("/?year=2026&month=10")
    empty_api = client.get("/api/calendar-arrivals?year=2026&month=10")
    assert empty_api.json() == {"dates": []}
    assert "class=\"attendance-mark\"" not in empty_page.text

    for employee_id in ("003", "004", "005"):
        add_employee(database_path, employee_id, f"社員{employee_id}")

    sep_28 = datetime(2026, 9, 28, 9, 0, tzinfo=JST)
    for employee_id in ("001", "002"):
        _change(database_path, employee_id, "出社", sep_28)
        _change(database_path, employee_id, "退勤", datetime(2026, 9, 28, 18, 0, tzinfo=JST))
    _change(database_path, "005", "出社", sep_28)
    for operation, hour in (("離席", 9), ("戻り", 10), ("退勤", 18)):
        _change(database_path, "005", operation, datetime(2026, 10, 1, hour, 0, tzinfo=JST))
    _change(
        database_path,
        "001",
        "出社",
        datetime(2026, 10, 2, 15, 30, tzinfo=ZoneInfo("UTC")),
    )
    _change(
        database_path,
        "003",
        "出社",
        datetime(2026, 10, 31, 14, 59, tzinfo=ZoneInfo("UTC")),
    )
    _change(
        database_path,
        "004",
        "出社",
        datetime(2026, 10, 31, 15, 0, tzinfo=ZoneInfo("UTC")),
    )
    inspection = _inspection(str(uuid4()))
    inspection.results["everyone_left"] = "abnormal"
    inspection.reasons["everyone_left"] = "在室者あり"
    validate_and_save_inspection(
        database_path,
        inspection,
        datetime(2026, 10, 3, 8, 0, tzinfo=JST),
    )
    import_employees(database_path, [("001", "変更後の氏名", False)])

    def cell_for(page_text, day):
        match = re.search(
            rf'<a\b(?=[^>]*data-date="{day}")([^>]*)>(.*?)</a>',
            page_text,
            re.DOTALL,
        )
        assert match is not None
        return match.group(1), match.group(2)

    page = client.get("/?year=2026&month=10")
    assert 'data-calendar-year="2026" data-calendar-month="10"' in page.text
    assert 'style="--week-count: 5"' in page.text
    assert 'aria-label="2026年9月28日、記録なし、出社記録あり"' in page.text
    assert "出社記録あり" in cell_for(page.text, "2026-09-28")[0]
    assert cell_for(page.text, "2026-09-28")[1].count("class=\"attendance-mark\"") == 1
    assert "出社記録あり" not in cell_for(page.text, "2026-10-01")[0]
    assert "出社記録あり" not in cell_for(page.text, "2026-10-02")[0]
    oct_3_attributes, oct_3_contents = cell_for(page.text, "2026-10-03")
    assert "出社記録あり" in oct_3_attributes and "今日" in oct_3_attributes
    assert "status-mark" in oct_3_contents and "attendance-mark" in oct_3_contents
    assert "出社記録あり" in cell_for(page.text, "2026-10-31")[0]
    assert "出社記録あり" in cell_for(page.text, "2026-11-01")[0]
    assert 'href="/history/2026-10-03"' in page.text
    assert "出社記録あり" in page.text

    october_api = client.get("/api/calendar-arrivals?year=2026&month=10")
    november_api = client.get("/api/calendar-arrivals?year=2026&month=11")
    november_page = client.get("/?year=2026&month=11")
    assert 'style="--week-count: 6"' in november_page.text
    assert october_api.json()["dates"] == [
        "2026-09-28",
        "2026-10-03",
        "2026-10-31",
        "2026-11-01",
    ]
    assert november_api.json()["dates"] == ["2026-10-31", "2026-11-01"]

    # A separately registered arrival appears through the month-scoped API.
    add_employee(database_path, "006", "社員006")
    _change(database_path, "006", "出社", datetime(2026, 10, 5, 9, 0, tzinfo=JST))
    refreshed_api = client.get("/api/calendar-arrivals?year=2026&month=10")
    assert "2026-10-05" in refreshed_api.json()["dates"]
    assert str(page.url).endswith("/?year=2026&month=10")

    with connect(database_path) as connection:
        before = {
            "attendance": [
                tuple(row)
                for row in connection.execute(
                    "SELECT employee_id, status, updated_at FROM employee_attendance ORDER BY employee_id"
                )
            ],
            "history": [
                tuple(row)
                for row in connection.execute(
                    "SELECT attendance_history_id, employee_id, operation, changed_at "
                    "FROM attendance_history ORDER BY attendance_history_id"
                )
            ],
        }
    client.get("/?year=2026&month=10")
    client.get("/api/calendar-arrivals?year=2026&month=10")
    with connect(database_path) as connection:
        after = {
            "attendance": [
                tuple(row)
                for row in connection.execute(
                    "SELECT employee_id, status, updated_at FROM employee_attendance ORDER BY employee_id"
                )
            ],
            "history": [
                tuple(row)
                for row in connection.execute(
                    "SELECT attendance_history_id, employee_id, operation, changed_at "
                    "FROM attendance_history ORDER BY attendance_history_id"
                )
            ],
        }
    assert after == before
