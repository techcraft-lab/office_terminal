import html
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from office_terminal.main import create_app
from office_terminal.services.attendance_service import (
    AttendanceTransitionError,
    change_attendance,
    list_active_attendance,
    list_current_attendees,
)
from office_terminal.services.backup_service import (
    BackupConfig,
    create_backup,
    restore_backup,
)
from office_terminal.services.inspection_service import (
    ITEM_DEFINITIONS,
    InspectionSubmission,
    validate_and_save_inspection,
)
from office_terminal.storage.database import connect, initialize_database
from office_terminal.storage.repository import add_employee, get_daily_inspections, import_employees

JST = ZoneInfo("Asia/Tokyo")


@pytest.fixture
def attendance_client(tmp_path, monkeypatch):
    database_path = tmp_path / "office_terminal.db"
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
        str(tmp_path / "application.lock"),
    )
    initialize_database(database_path)
    add_employee(database_path, "001", "架空 山田")
    add_employee(database_path, "002", "架空 佐藤")
    add_employee(database_path, "003", "無効社員", is_active=False)
    with TestClient(create_app(database_path)) as client:
        yield client, database_path


def _change(database_path: Path, employee_id: str, operation: str, key=None):
    return change_attendance(
        database_path,
        employee_id=employee_id,
        operation=operation,
        idempotency_key=key or str(uuid4()),
        changed_at=datetime(2026, 10, 3, 12, 0, tzinfo=JST),
    )


def _inspection(employee_id="001", key=None):
    return InspectionSubmission(
        idempotency_key=key or str(uuid4()),
        employee_id=employee_id,
        results={item["code"]: "confirmed" for item in ITEM_DEFINITIONS},
        reasons={item["code"]: "" for item in ITEM_DEFINITIONS},
        note="",
    )


def _attendance_history(database_path: Path, employee_id: str | None = None):
    query = "SELECT * FROM attendance_history"
    values = ()
    if employee_id is not None:
        query += " WHERE employee_id = ?"
        values = (employee_id,)
    query += " ORDER BY attendance_history_id"
    with connect(database_path) as connection:
        return [dict(row) for row in connection.execute(query, values).fetchall()]


def _employee_options(page_text: str, select_id: str) -> list[tuple[str, str]]:
    match = re.search(
        rf'<select\b[^>]*\bid="{re.escape(select_id)}"[^>]*>(.*?)</select>',
        page_text,
        re.DOTALL,
    )
    assert match is not None
    return [
        (value, html.unescape(re.sub(r"<[^>]*>", "", label)).strip())
        for value, label in re.findall(
            r'<option\b[^>]*\bvalue="([^"]*)"[^>]*>(.*?)</option>',
            match.group(1),
            re.DOTALL,
        )
    ]


def _visible_html_text(markup: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]*>", "", markup)).split())


def test_attendance_transitions_are_persistent_and_idempotent(attendance_client):
    _, database_path = attendance_client
    operation_key = str(uuid4())
    first_arrival = _change(database_path, "001", "出社", operation_key)
    replayed_arrival = _change(database_path, "001", "出社", operation_key)
    assert first_arrival.current_status == "在室"
    assert replayed_arrival.duplicate is True
    assert replayed_arrival.current_status == "在室"
    assert len(_attendance_history(database_path, "001")) == 1

    assert _change(database_path, "001", "離席").current_status == "離席中"
    assert _change(database_path, "001", "戻り").current_status == "在室"
    assert _change(database_path, "001", "退勤").current_status == "退勤済み"
    history = _attendance_history(database_path, "001")
    assert [entry["operation"] for entry in history] == ["出社", "離席", "戻り", "退勤"]
    assert history[0]["employee_name"] == "架空 山田"
    assert history[0]["previous_status"] == "退勤済み"
    assert history[0]["new_status"] == "在室"
    assert history[0]["changed_at"].endswith("+09:00")
    with pytest.raises(AttendanceTransitionError, match="現在の状態は"):
        _change(database_path, "001", "戻り")
    with pytest.raises(AttendanceTransitionError, match="有効な社員"):
        _change(database_path, "003", "出社")

    initialize_database(database_path)
    assert next(
        employee for employee in list_active_attendance(database_path)
        if employee["employee_id"] == "001"
    )["status"] == "退勤済み"


def test_top_counts_current_active_employees_and_omits_checked_out_and_inactive(
    attendance_client,
):
    client, database_path = attendance_client
    _change(database_path, "001", "出社")
    _change(database_path, "002", "出社")
    _change(database_path, "002", "離席")

    page = client.get("/")
    assert 'id="attendee-present-count">1</strong>名／離席<strong id="attendee-away-count">1</strong>' in page.text
    assert "架空 山田" in page.text
    assert "架空 佐藤" in page.text
    assert "無効社員" not in page.text
    assert len(list_current_attendees(database_path)["people"]) == 2

    _change(database_path, "001", "退勤")
    _change(database_path, "002", "退勤")
    page = client.get("/")
    assert "現在、出社者はいません" in page.text
    assert 'id="attendee-present-count">0</strong>名／離席<strong id="attendee-away-count">0</strong>' in page.text


def test_employee_id_sort_and_name_only_display_across_employee_screens(attendance_client):
    client, database_path = attendance_client
    names = {
        "001": "同姓同名",
        "002": "氏名06",
        "003": "氏名05",
        "004": "氏名04",
        "005": "氏名03",
        "006": "氏名02",
        "DEV-001": "同姓同名",
        "000": "無効社員",
    }
    csv_order = ["DEV-001", "006", "005", "004", "003", "002", "001", "000"]
    import_employees(
        database_path,
        [(employee_id, names[employee_id], employee_id != "000") for employee_id in csv_order],
    )
    expected_ids = ["001", "002", "003", "004", "005", "006", "DEV-001"]
    expected_options = [(employee_id, names[employee_id]) for employee_id in expected_ids]

    for employee_id in expected_ids:
        _change(database_path, employee_id, "出社")

    inspection_page = client.get("/inspection")
    inspection_options = _employee_options(inspection_page.text, "employee-id")
    assert inspection_options[1:] == expected_options
    assert "（001）" not in inspection_page.text
    assert "無効社員" not in inspection_page.text

    for path in ("/attendees", "/pc/attendees"):
        page = client.get(path)
        options = _employee_options(page.text, "attendance-employee")
        assert options[1:] == expected_options
        assert "（001）" not in page.text
        assert "無効社員" not in page.text

    api_response = client.get("/api/attendees")
    people = api_response.json()["people"]
    assert [person["employee_id"] for person in people] == expected_ids
    assert [person["name"] for person in people] == [names[employee_id] for employee_id in expected_ids]

    home_page = client.get("/")
    top_rows = re.findall(
        r'<li class="attendee-item" data-employee-id="([^"]*)">(.*?)</li>',
        home_page.text,
        re.DOTALL,
    )
    assert [employee_id for employee_id, _ in top_rows] == expected_ids
    assert [_visible_html_text(markup) for _, markup in top_rows] == [
        f"{names[employee_id]} 在室" for employee_id in expected_ids
    ]
    assert "無効社員" not in home_page.text

    history_page = client.get("/attendance/history?target_date=2026-10-03")
    history_options = _employee_options(history_page.text, "attendance-history-employee")
    assert history_options[1:] == expected_options
    assert "社員番号：" not in history_page.text
    records = re.findall(
        r'<li class="attendance-history-record">(.*?)</li>',
        history_page.text,
        re.DOTALL,
    )
    assert [
        html.unescape(name)
        for name in re.findall(
            r'<strong class="attendance-history-name">([^<]*)</strong>',
            history_page.text,
        )
    ] == [names[employee_id] for employee_id in reversed(expected_ids)]
    assert all("001" not in _visible_html_text(record) for record in records)
    filtered_history = client.get(
        "/attendance/history?target_date=2026-10-03&employee_id=DEV-001"
    )
    assert filtered_history.text.count('class="attendance-history-record"') == 1
    assert re.search(
        r'<option\b[^>]*value="DEV-001"[^>]*selected',
        filtered_history.text,
    )

    pc_page = client.get("/pc/attendees")
    restore_epoch = re.search(
        r'<input\b[^>]*\bname="restore_epoch"[^>]*\bvalue="([^"]*)"',
        pc_page.text,
    ).group(1)
    operation_key = re.search(
        r'<input\b[^>]*\bname="operation_key"[^>]*\bvalue="([^"]*)"',
        pc_page.text,
    ).group(1)
    response = client.post(
        "/pc/attendees",
        data={
            "employee_id": "DEV-001",
            "operation": "離席",
            "operation_key": operation_key,
            "restore_epoch": restore_epoch,
        },
    )
    assert response.status_code == 200
    assert re.search(r'<option\b[^>]*value="DEV-001"[^>]*selected', response.text)
    statuses = {
        employee["employee_id"]: employee["status"]
        for employee in list_active_attendance(database_path)
    }
    assert statuses["DEV-001"] == "離席中"
    assert statuses["001"] == "在室"

    inspection_data = {
        name: re.search(
            rf'<input\b[^>]*\bname="{name}"[^>]*\bvalue="([^"]*)"',
            inspection_page.text,
        ).group(1)
        for name in ("idempotency_key", "draft_revision", "restore_epoch")
    }
    inspection_data.update({"employee_id": "DEV-001", "note": ""})
    for item in ITEM_DEFINITIONS:
        inspection_data[f"result_{item['code']}"] = "confirmed"
        inspection_data[f"reason_{item['code']}"] = ""
    inspection_data["result_everyone_left"] = "abnormal"
    inspection_data["reason_everyone_left"] = "在室者あり"
    saved_inspection = client.post(
        "/inspection",
        data=inspection_data,
        follow_redirects=False,
    )
    assert saved_inspection.status_code == 303
    inspections = get_daily_inspections(database_path, datetime.now(JST).date())
    assert inspections[-1]["employee_id"] == "DEV-001"


def test_app_restart_preserves_attendance_status(tmp_path, monkeypatch):
    database_path = tmp_path / "office_terminal.db"
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
        str(tmp_path / "application.lock"),
    )
    with TestClient(create_app(database_path)) as first_client:
        add_employee(database_path, "001", "架空 山田")
        page = first_client.get("/attendees")
        operation_key = page.text.split('name="operation_key" value="', 1)[1].split('"', 1)[0]
        response = first_client.post(
            "/attendees",
            data={
                "employee_id": "001",
                "operation": "出社",
                "operation_key": operation_key,
                "restore_epoch": "",
            },
        )
        assert response.status_code == 200

    with TestClient(create_app(database_path)) as restarted_client:
        page = restarted_client.get("/attendees")
        assert 'data-status="在室"' in page.text
        assert list_current_attendees(database_path)["present_count"] == 1


def test_attendance_screen_shows_current_state_and_rejects_stale_generation(
    attendance_client,
    tmp_path,
):
    client, database_path = attendance_client
    _change(database_path, "001", "出社")
    operations = tmp_path / "operations.json"
    operations.write_text('{"restore_epoch":"new-epoch"}', encoding="utf-8")
    page = client.get("/attendees")
    assert page.status_code == 200
    assert 'value="001"' in page.text
    assert 'data-status="在室"' in page.text
    assert "本人認証ではありません" in page.text
    assert 'value="退勤"' in page.text

    response = client.post(
        "/attendees",
        data={
            "employee_id": "001",
            "operation": "退勤",
            "operation_key": str(uuid4()),
            "restore_epoch": "old-epoch",
        },
    )
    assert response.status_code == 409
    assert "復元前に開いた画面" in response.text
    assert _attendance_history(database_path, "001")[-1]["new_status"] == "在室"

    current_page = client.get("/attendees")
    operation_key = current_page.text.split('name="operation_key" value="', 1)[1].split('"', 1)[0]
    result = client.post(
        "/attendees",
        data={
            "employee_id": "001",
            "operation": "退勤",
            "operation_key": operation_key,
            "restore_epoch": "new-epoch",
        },
    )
    assert result.status_code == 200
    assert "状態を「退勤済み」に更新しました" in result.text

    invalid_transition = client.post(
        "/attendees",
        data={
            "employee_id": "001",
            "operation": "離席",
            "operation_key": str(uuid4()),
            "restore_epoch": "new-epoch",
        },
    )
    assert invalid_transition.status_code == 422
    assert "現在の状態は「退勤済み」です" in invalid_transition.text


def test_pc_and_kiosk_pages_share_attendance_state_and_have_explicit_idle_modes(
    attendance_client,
):
    client, database_path = attendance_client
    kiosk_page = client.get("/attendees")
    pc_page = client.get("/pc/attendees")
    assert 'data-idle-return="disabled"' not in kiosk_page.text
    assert 'data-idle-return="disabled"' in pc_page.text
    assert 'action="/attendees"' in kiosk_page.text
    assert 'action="/pc/attendees"' in pc_page.text

    operation_key = pc_page.text.split('name="operation_key" value="', 1)[1].split('"', 1)[0]
    response = client.post(
        "/pc/attendees",
        data={
            "employee_id": "001",
            "operation": "出社",
            "operation_key": operation_key,
            "restore_epoch": "",
        },
    )
    assert response.status_code == 200
    assert 'data-status="在室"' in client.get("/attendees").text
    assert client.get("/").text.count("架空 山田") == 1
    assert _attendance_history(database_path, "001")[-1]["operation"] == "出社"


def test_attendance_screen_reserves_status_area_and_bottom_navigation(attendance_client):
    client, _ = attendance_client
    page = client.get("/attendees")
    form_start = page.text.index('<form class="attendance-form"')
    upper_start = page.text.index('<div class="attendance-upper">', form_start)
    center_start = page.text.index('<section class="attendance-center">', upper_start)
    form_end = page.text.index("</form>", center_start)
    footer_start = page.text.index('<nav class="attendance-footer"', form_end)
    assert form_start < upper_start < center_start < form_end < footer_start
    assert 'id="attendance-status">未選択</strong>' in page.text
    assert 'class="attendance-result-placeholder"' in page.text
    assert 'class="attendance-history-link"' in page.text
    assert 'class="home-button" href="/">TOPへ戻る</a>' in page.text


def test_pc_duplicate_submission_and_competing_transition_do_not_duplicate_history(
    attendance_client,
):
    client, database_path = attendance_client
    pc_page = client.get("/pc/attendees")
    operation_key = pc_page.text.split('name="operation_key" value="', 1)[1].split('"', 1)[0]
    payload = {
        "employee_id": "001",
        "operation": "出社",
        "operation_key": operation_key,
        "restore_epoch": "",
    }
    assert client.post("/pc/attendees", data=payload).status_code == 200
    replay = client.post("/pc/attendees", data=payload)
    assert replay.status_code == 200
    assert "すでに処理済み" in replay.text
    assert len(_attendance_history(database_path, "001")) == 1

    kiosk_page = client.get("/attendees")
    away_key = kiosk_page.text.split('name="operation_key" value="', 1)[1].split('"', 1)[0]
    away_payload = {
        "employee_id": "001",
        "operation": "離席",
        "operation_key": away_key,
        "restore_epoch": "",
    }
    assert client.post("/attendees", data=away_payload).status_code == 200
    competing = client.post(
        "/pc/attendees",
        data={
            **away_payload,
            "operation_key": str(uuid4()),
        },
    )
    assert competing.status_code == 422
    assert "現在の状態は「離席中」です" in competing.text
    assert 'data-status="離席中"' in competing.text
    assert len(_attendance_history(database_path, "001")) == 2


def test_pc_page_rejects_form_from_before_restore(attendance_client, tmp_path):
    client, database_path = attendance_client
    operations = tmp_path / "operations.json"
    page = client.get("/pc/attendees")
    operation_key = page.text.split('name="operation_key" value="', 1)[1].split('"', 1)[0]
    operations.write_text('{"restore_epoch":"restored"}', encoding="utf-8")

    response = client.post(
        "/pc/attendees",
        data={
            "employee_id": "001",
            "operation": "出社",
            "operation_key": operation_key,
            "restore_epoch": "",
        },
    )
    assert response.status_code == 409
    assert "復元前に開いた画面" in response.text
    assert _attendance_history(database_path, "001") == []


def test_attendee_api_is_read_only_and_filters_inactive_and_checked_out(
    attendance_client,
):
    client, database_path = attendance_client
    _change(database_path, "001", "出社")
    _change(database_path, "002", "出社")
    _change(database_path, "002", "退勤")
    before = _attendance_history(database_path)

    response = client.get("/api/attendees")

    assert response.status_code == 200
    assert response.json() == {
        "people": [{"employee_id": "001", "name": "架空 山田", "status": "在室"}],
        "present_count": 1,
        "away_count": 0,
    }
    assert _attendance_history(database_path) == before


def test_failed_attendance_history_write_rolls_back_status(attendance_client):
    client, database_path = attendance_client
    with connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_attendance_history
            BEFORE INSERT ON attendance_history
            BEGIN
                SELECT RAISE(ABORT, 'simulated attendance history failure');
            END;
            """
        )

    page = client.get("/attendees")
    operation_key = page.text.split('name="operation_key" value="', 1)[1].split('"', 1)[0]
    response = client.post(
        "/attendees",
        data={
            "employee_id": "001",
            "operation": "出社",
            "operation_key": operation_key,
            "restore_epoch": "",
        },
    )
    assert response.status_code == 503
    assert "状態を保存できませんでした" in response.text
    assert next(
        employee for employee in list_active_attendance(database_path)
        if employee["employee_id"] == "001"
    )["status"] == "退勤済み"
    assert _attendance_history(database_path, "001") == []


def test_inspection_checkout_is_atomic_and_only_applies_to_inspector(attendance_client):
    _, database_path = attendance_client
    _change(database_path, "001", "出社")
    _change(database_path, "002", "出社")
    key = str(uuid4())
    submission = _inspection(key=key)
    submission.results["everyone_left"] = "abnormal"
    submission.reasons["everyone_left"] = "在室者あり"
    saved = validate_and_save_inspection(
        database_path,
        submission,
        datetime(2026, 10, 3, 18, 0, tzinfo=JST),
    )
    assert saved[1] is False
    statuses = {row["employee_id"]: row["status"] for row in list_active_attendance(database_path)}
    assert statuses["001"] == "退勤済み"
    assert statuses["002"] == "在室"
    assert _attendance_history(database_path, "001")[-1]["operation"] == "退勤"
    assert _attendance_history(database_path, "002")[-1]["operation"] == "出社"

    _change(database_path, "001", "出社")
    history_count = len(_attendance_history(database_path, "001"))
    replay = validate_and_save_inspection(
        database_path,
        submission,
        datetime(2026, 10, 3, 19, 0, tzinfo=JST),
    )
    assert replay[1] is True
    assert len(_attendance_history(database_path, "001")) == history_count
    assert next(
        employee for employee in list_active_attendance(database_path)
        if employee["employee_id"] == "001"
    )["status"] == "在室"


def test_failed_inspection_rolls_back_record_items_and_checkout(attendance_client):
    _, database_path = attendance_client
    _change(database_path, "001", "出社")
    with connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_inspection_checkout
            BEFORE INSERT ON attendance_history
            WHEN NEW.operation = '退勤'
            BEGIN
                SELECT RAISE(ABORT, 'simulated attendance history failure');
            END;
            """
        )

    with pytest.raises(sqlite3.IntegrityError):
        validate_and_save_inspection(
            database_path,
            _inspection(),
            datetime(2026, 10, 3, 18, 0, tzinfo=JST),
        )

    assert next(
        employee for employee in list_active_attendance(database_path)
        if employee["employee_id"] == "001"
    )["status"] == "在室"
    assert len(_attendance_history(database_path, "001")) == 1
    with connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM inspections").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM inspection_items").fetchone()[0] == 0


def test_already_checked_out_or_unregistered_status_adds_no_checkout_history(
    attendance_client,
):
    _, database_path = attendance_client
    validate_and_save_inspection(
        database_path,
        _inspection(),
        datetime(2026, 10, 3, 18, 0, tzinfo=JST),
    )
    assert _attendance_history(database_path, "001") == []

    _change(database_path, "001", "出社")
    _change(database_path, "001", "退勤")
    history_count = len(_attendance_history(database_path, "001"))
    validate_and_save_inspection(
        database_path,
        _inspection(),
        datetime(2026, 10, 3, 19, 0, tzinfo=JST),
    )
    assert len(_attendance_history(database_path, "001")) == history_count


@pytest.mark.parametrize("result", ["confirmed", "abnormal", "not_done"])
def test_any_successful_inspection_result_checks_out_only_the_inspector(
    attendance_client,
    result,
):
    _, database_path = attendance_client
    _change(database_path, "001", "出社")
    _change(database_path, "002", "出社")
    submission = _inspection()
    submission.results["air_conditioner"] = result
    submission.reasons["air_conditioner"] = "" if result == "confirmed" else "使用中"
    submission.results["everyone_left"] = "abnormal"
    submission.reasons["everyone_left"] = "在室者あり"

    validate_and_save_inspection(
        database_path,
        submission,
        datetime(2026, 10, 3, 18, 0, tzinfo=JST),
    )

    statuses = {row["employee_id"]: row["status"] for row in list_active_attendance(database_path)}
    assert statuses["001"] == "退勤済み"
    assert statuses["002"] == "在室"
    assert _attendance_history(database_path, "001")[-1]["operation"] == "退勤"


def test_new_and_legacy_backups_restore_attendance_tables_and_migrate(tmp_path):
    database_path = tmp_path / "source.db"
    initialize_database(database_path)
    add_employee(database_path, "001", "架空 山田")
    _change(database_path, "001", "出社")
    archive = create_backup(
        database_path,
        BackupConfig(backup_directory=tmp_path / "new-backups", allow_unmounted=True),
    )
    new_target = tmp_path / "new-target.db"
    restore_backup(
        archive,
        new_target,
        config_file=tmp_path / "new-settings.json",
        lock_path=tmp_path / "new-runtime.lock",
        execution_lock_path=tmp_path / "new-backup.lock",
        confirmed=True,
    )
    assert next(
        employee for employee in list_active_attendance(new_target)
        if employee["employee_id"] == "001"
    )["status"] == "在室"
    assert len(_attendance_history(new_target, "001")) == 1

    with connect(database_path) as connection:
        connection.execute("DROP TABLE attendance_history")
        connection.execute("DROP TABLE employee_attendance")
    legacy_archive = create_backup(
        database_path,
        BackupConfig(backup_directory=tmp_path / "legacy-backups", allow_unmounted=True),
    )
    legacy_target = tmp_path / "legacy-target.db"
    restore_backup(
        legacy_archive,
        legacy_target,
        config_file=tmp_path / "legacy-settings.json",
        lock_path=tmp_path / "legacy-runtime.lock",
        execution_lock_path=tmp_path / "legacy-backup.lock",
        confirmed=True,
    )
    with connect(legacy_target) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='employee_attendance'"
        ).fetchone() is None
    initialize_database(legacy_target)
    assert next(
        employee for employee in list_active_attendance(legacy_target)
        if employee["employee_id"] == "001"
    )["status"] == "退勤済み"
    assert _attendance_history(legacy_target, "001") == []
