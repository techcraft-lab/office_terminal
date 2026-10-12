import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from office_terminal.main import create_app
from office_terminal.storage.database import connect
from office_terminal.storage.repository import add_employee, get_daily_inspections
from office_terminal.services.inspection_service import ITEM_DEFINITIONS

JST = ZoneInfo("Asia/Tokyo")


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "office_terminal.db"


@pytest.fixture
def client(database_path: Path, monkeypatch):
    monkeypatch.setenv("OFFICE_TERMINAL_OPERATIONS_CONFIG_PATH", str(database_path.parent / "operations.json"))
    monkeypatch.setenv("OFFICE_TERMINAL_BACKUP_STATE_PATH", str(database_path.parent / "backup-state.json"))
    monkeypatch.setenv("OFFICE_TERMINAL_RUNTIME_LOCK_PATH", str(database_path.parent / "application.lock"))
    with TestClient(create_app(database_path)) as test_client:
        yield test_client


def register_employee(database_path: Path) -> None:
    add_employee(database_path, "DEV-001", "開発用 山田")


def inspection_form(
    client: TestClient,
    *,
    employee_id: str = "DEV-001",
    status: str = "confirmed",
    token: str | None = None,
    reason: str = "",
    note: str = "",
) -> dict[str, str]:
    page = client.get("/inspection")
    key = token or page.text.split('name="idempotency_key" value="', 1)[1].split('"', 1)[0]
    restore_epoch = page.text.split('name="restore_epoch" value="', 1)[1].split('"', 1)[0]
    data = {
        "idempotency_key": key,
        "restore_epoch": restore_epoch,
        "employee_id": employee_id,
        "note": note,
    }
    for item in ITEM_DEFINITIONS:
        code = item["code"]
        data[f"result_{code}"] = status
        data[f"reason_{code}"] = reason or (
            "確認できない" if status != "confirmed" else ""
        )
    return data


def count_inspections(database_path: Path) -> int:
    with connect(database_path) as connection:
        return connection.execute("SELECT COUNT(*) FROM inspections").fetchone()[0]


def set_attendance(database_path: Path, employee_id: str, status: str) -> None:
    with connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO employee_attendance (employee_id, status, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(employee_id) DO UPDATE SET
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (employee_id, status, datetime.now(JST).isoformat(timespec="seconds")),
        )


def database_write_snapshot(database_path: Path) -> dict[str, list[tuple]]:
    with connect(database_path) as connection:
        return {
            table: [
                tuple(row)
                for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")
            ]
            for table in (
                "inspections",
                "inspection_items",
                "employee_attendance",
                "attendance_history",
            )
        }


def test_startup_does_not_create_employees_or_inspections(client, database_path):
    response = client.get("/inspection")
    assert response.status_code == 200
    assert "点検者が登録されていません" in response.text
    assert count_inspections(database_path) == 0
    with connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 0


def test_development_employees_are_added_only_by_explicit_command(tmp_path):
    database_path = tmp_path / "cli" / "office_terminal.db"
    environment = os.environ.copy()
    environment["OFFICE_TERMINAL_DB_PATH"] = str(database_path)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "office_terminal.cli",
            "add-development-employees",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert "開発用社員を登録しました" in completed.stdout
    with connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM inspections").fetchone()[0] == 0


def test_normal_inspection_is_saved_and_calendar_updates(client, database_path):
    register_employee(database_path)

    data = inspection_form(client)
    response = client.post(
        "/inspection",
        data=data,
        follow_redirects=False,
    )

    assert response.status_code == 303
    redirect = urlparse(response.headers["location"])
    redirect_query = parse_qs(redirect.query)
    assert redirect.path == "/"
    assert redirect_query["saved"] == ["complete"]
    assert redirect_query["saved_key"] == [data["idempotency_key"]]
    records = get_daily_inspections(database_path, datetime.now(JST).date())
    assert len(records) == 1
    assert records[0]["employee_id"] == "DEV-001"
    assert records[0]["employee_name"] == "開発用 山田"
    assert records[0]["overall_status"] == "complete"
    assert len(records[0]["items"]) == 5
    assert records[0]["note"] == ""
    assert "+09:00" in records[0]["recorded_at"]
    home = client.get(f"/?year={datetime.now(JST).year}&month={datetime.now(JST).month}")
    assert f'href="/history/{datetime.now(JST).date().isoformat()}"' in home.text
    assert "status-complete" in home.text


def test_inspection_keeps_employee_name_snapshot(client, database_path):
    register_employee(database_path)
    response = client.post(
        "/inspection",
        data=inspection_form(client),
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect(database_path) as connection:
        connection.execute(
            "UPDATE employees SET display_name = ? WHERE employee_id = ?",
            ("変更後の名前", "DEV-001"),
        )

    record = get_daily_inspections(database_path, datetime.now(JST).date())[0]
    assert record["employee_name"] == "開発用 山田"
    assert "変更後の名前" not in record["employee_name"]


def test_empty_invalid_and_inactive_employees_are_rejected(client, database_path):
    register_employee(database_path)
    valid_data = inspection_form(client)

    missing_result = dict(valid_data)
    del missing_result["result_lights"]
    response = client.post("/inspection", data=missing_result)
    assert response.status_code == 422
    assert "5項目すべてを選択してください" in response.text

    missing_employee = dict(valid_data)
    missing_employee["employee_id"] = ""
    response = client.post("/inspection", data=missing_employee)
    assert response.status_code == 422
    assert "有効な点検者を選択してください" in response.text

    invalid_choice = dict(valid_data)
    invalid_choice["result_lights"] = "maybe"
    response = client.post("/inspection", data=invalid_choice)
    assert response.status_code == 422
    assert "結果を選択してください" in response.text

    inactive_employee = dict(valid_data)
    inactive_employee["employee_id"] = "DISABLED"
    add_employee(database_path, "DISABLED", "無効社員", is_active=False)
    response = client.post("/inspection", data=inactive_employee)
    assert response.status_code == 422
    assert "有効な点検者を選択してください" in response.text
    assert count_inspections(database_path) == 0


def test_attention_requires_reason_and_is_saved_with_reason(client, database_path):
    register_employee(database_path)
    missing_reason = inspection_form(client, status="abnormal")
    for item in ITEM_DEFINITIONS:
        missing_reason[f"reason_{item['code']}"] = ""
    response = client.post("/inspection", data=missing_reason)
    assert response.status_code == 422
    assert "理由を選択してください" in response.text
    assert count_inspections(database_path) == 0

    with_reason = inspection_form(
        client,
        status="not_done",
        reason="確認できない",
    )
    response = client.post("/inspection", data=with_reason, follow_redirects=False)
    assert response.status_code == 303
    assert parse_qs(urlparse(response.headers["location"]).query)["saved"] == ["attention"]
    records = get_daily_inspections(database_path, datetime.now(JST).date())
    assert records[0]["overall_status"] == "attention"
    assert all(item["reason"] == "確認できない" for item in records[0]["items"])
    assert "要確認の記録を保存しました" in client.get(response.headers["location"]).text


def test_reason_choices_are_required_only_for_attention_and_confirmed_reasons_are_cleared(
    client,
    database_path,
):
    register_employee(database_path)
    confirmed = inspection_form(client)
    confirmed["reason_air_conditioner"] = "使用中"
    response = client.post("/inspection", data=confirmed, follow_redirects=False)
    assert response.status_code == 303
    record = get_daily_inspections(database_path, datetime.now(JST).date())[0]
    assert record["items"][0]["reason"] == ""

    abnormal = inspection_form(client, status="abnormal")
    abnormal["reason_windows"] = "使用中"
    response = client.post("/inspection", data=abnormal)
    assert response.status_code == 422
    assert "項目に対応する理由を選択してください" in response.text
    assert count_inspections(database_path) == 1


def test_inspection_form_has_touch_reason_choices_and_no_note_input(client):
    page = client.get("/inspection")
    assert 'name="reason_air_conditioner"' in page.text
    assert "理由を選択してください" in page.text
    assert 'name="note"' not in page.text
    assert "<textarea" not in page.text


def test_inspection_attendance_api_excludes_inspector_and_inactive_employees(
    client,
    database_path,
):
    register_employee(database_path)
    add_employee(database_path, "DEV-002", "開発用 佐藤")
    add_employee(database_path, "DEV-003", "開発用 鈴木")
    add_employee(database_path, "DEV-004", "無効社員", is_active=False)
    set_attendance(database_path, "DEV-001", "在室")
    set_attendance(database_path, "DEV-002", "在室")
    set_attendance(database_path, "DEV-003", "離席中")
    set_attendance(database_path, "DEV-004", "在室")

    response = client.get("/api/inspection/attendance?employee_id=DEV-001")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "conflicts": [
            {"display_name": "開発用 佐藤", "status": "在室"},
            {"display_name": "開発用 鈴木", "status": "離席中"},
        ]
    }
    another_inspector = client.get(
        "/api/inspection/attendance?employee_id=DEV-002"
    ).json()
    assert another_inspector["conflicts"] == [
        {"display_name": "開発用 山田", "status": "在室"},
        {"display_name": "開発用 鈴木", "status": "離席中"},
    ]


def test_inspection_form_disables_confirmed_until_attendance_is_checked(client, database_path):
    register_employee(database_path)

    response = client.get("/inspection")

    assert re.search(
        r'<input[^>]*name="result_everyone_left"[^>]*value="confirmed"[^>]*disabled',
        response.text,
    )
    assert "判定は登録上の在席状態に基づきます" in response.text
    assert "点検者本人は判定対象外です" in response.text


def test_inspector_alone_can_confirm_and_is_checked_out_atomically(client, database_path):
    register_employee(database_path)
    set_attendance(database_path, "DEV-001", "在室")

    response = client.post(
        "/inspection",
        data=inspection_form(client),
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert count_inspections(database_path) == 1
    with connect(database_path) as connection:
        assert connection.execute(
            "SELECT status FROM employee_attendance WHERE employee_id = ?",
            ("DEV-001",),
        ).fetchone()[0] == "退勤済み"
        assert connection.execute(
            "SELECT COUNT(*) FROM attendance_history"
        ).fetchone()[0] == 1


def test_departed_and_inactive_attendees_do_not_block_confirmation(client, database_path):
    register_employee(database_path)
    add_employee(database_path, "DEV-002", "退勤済み社員")
    add_employee(database_path, "DEV-003", "無効社員", is_active=False)
    set_attendance(database_path, "DEV-003", "在室")

    response = client.post(
        "/inspection",
        data=inspection_form(client),
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert count_inspections(database_path) == 1


def test_other_active_attendee_rejects_confirmed_without_changing_any_records(
    client,
    database_path,
):
    register_employee(database_path)
    add_employee(database_path, "DEV-002", "開発用 佐藤")
    add_employee(database_path, "DEV-003", "開発用 鈴木")
    add_employee(database_path, "DEV-004", "無効社員", is_active=False)
    set_attendance(database_path, "DEV-001", "在室")
    set_attendance(database_path, "DEV-002", "在室")
    set_attendance(database_path, "DEV-003", "離席中")
    set_attendance(database_path, "DEV-004", "在室")
    with connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO attendance_history (
                employee_id, employee_name, previous_status, new_status,
                operation, idempotency_key, changed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "DEV-002",
                "開発用 佐藤",
                "退勤済み",
                "在室",
                "出社",
                str(uuid4()),
                datetime.now(JST).isoformat(timespec="seconds"),
            ),
        )
    before = database_write_snapshot(database_path)

    response = client.post("/inspection", data=inspection_form(client))

    assert response.status_code == 422
    assert "点検者以外に在室・離席中の社員が登録されています" in response.text
    assert "開発用 佐藤（在室）" in response.text
    assert "開発用 鈴木（離席中）" in response.text
    assert "無効社員" not in response.text
    assert re.search(
        r'<input[^>]*name="result_air_conditioner"[^>]*'
        r'value="confirmed"[^>]*checked',
        response.text,
    )
    assert not re.search(
        r'<input[^>]*name="result_everyone_left"[^>]*'
        r'value="confirmed"[^>]*checked',
        response.text,
    )
    assert database_write_snapshot(database_path) == before


def test_attendance_added_after_form_load_is_rechecked_when_saving(client, database_path):
    register_employee(database_path)
    add_employee(database_path, "DEV-002", "開発用 佐藤")
    assert client.get("/api/inspection/attendance?employee_id=DEV-001").json() == {
        "conflicts": []
    }
    set_attendance(database_path, "DEV-002", "在室")
    before = database_write_snapshot(database_path)

    response = client.post(
        "/inspection",
        data=inspection_form(client),
    )

    assert response.status_code == 422
    assert "開発用 佐藤（在室）" in response.text
    assert database_write_snapshot(database_path) == before


@pytest.mark.parametrize(
    ("result", "reason"),
    [("abnormal", "在室者あり"), ("not_done", "確認できない")],
)
def test_attendance_conflict_does_not_reject_abnormal_or_not_done(
    client,
    database_path,
    result,
    reason,
):
    register_employee(database_path)
    add_employee(database_path, "DEV-002", "開発用 佐藤")
    set_attendance(database_path, "DEV-002", "在室")
    data = inspection_form(client)
    data["result_everyone_left"] = result
    data["reason_everyone_left"] = reason

    response = client.post("/inspection", data=data, follow_redirects=False)

    assert response.status_code == 303
    assert count_inspections(database_path) == 1
    assert get_daily_inspections(database_path, datetime.now(JST).date())[0][
        "overall_status"
    ] == "attention"


def test_duplicate_inspection_retry_precedes_new_attendance_conflict(client, database_path):
    register_employee(database_path)
    token = str(uuid4())
    data = inspection_form(client, token=token)
    first = client.post("/inspection", data=data, follow_redirects=False)
    assert first.status_code == 303
    add_employee(database_path, "DEV-002", "開発用 佐藤")
    set_attendance(database_path, "DEV-002", "在室")
    before = database_write_snapshot(database_path)

    retry = client.post("/inspection", data=data, follow_redirects=False)

    assert retry.status_code == 303
    assert count_inspections(database_path) == 1
    assert database_write_snapshot(database_path) == before


def test_identical_retry_is_idempotent_but_new_submission_is_kept(client, database_path):
    register_employee(database_path)
    token = str(uuid4())
    first = client.post(
        "/inspection",
        data=inspection_form(client, token=token),
        follow_redirects=False,
    )
    retry = client.post(
        "/inspection",
        data=inspection_form(client, token=token),
        follow_redirects=False,
    )
    deliberate_new_record = client.post(
        "/inspection",
        data=inspection_form(client),
        follow_redirects=False,
    )

    assert first.status_code == retry.status_code == deliberate_new_record.status_code == 303
    assert count_inspections(database_path) == 2


def test_success_redirect_identifies_the_exact_draft_revision(client, database_path):
    register_employee(database_path)
    token = str(uuid4())
    revision = str(uuid4())
    data = inspection_form(client, token=token)
    data["draft_revision"] = revision

    response = client.post("/inspection", data=data, follow_redirects=False)

    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["saved_key"] == [token]
    assert query["saved_revision"] == [revision]


def test_latest_same_timestamp_controls_calendar_and_history(client, database_path, monkeypatch):
    register_employee(database_path)
    fixed = datetime(2026, 8, 12, 18, 30, 0, tzinfo=JST)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr("office_terminal.web.routes.datetime", FixedDateTime)
    first = client.post(
        "/inspection",
        data=inspection_form(client, status="confirmed"),
        follow_redirects=False,
    )
    second = client.post(
        "/inspection",
        data=inspection_form(client, status="abnormal", reason="確認できない"),
        follow_redirects=False,
    )

    assert first.status_code == second.status_code == 303
    home = client.get("/?year=2026&month=8")
    assert 'href="/history/2026-08-12"' in home.text
    assert "status-alert" in home.text
    history = client.get("/history/2026-08-12")
    assert history.text.count("18:30:00") == 2
    assert "開発用 山田" in history.text
    assert "確認できない" in history.text
    assert "要確認" in history.text


def test_database_survives_application_restart(database_path, monkeypatch):
    monkeypatch.setenv("OFFICE_TERMINAL_OPERATIONS_CONFIG_PATH", str(database_path.parent / "operations.json"))
    monkeypatch.setenv("OFFICE_TERMINAL_BACKUP_STATE_PATH", str(database_path.parent / "backup-state.json"))
    monkeypatch.setenv("OFFICE_TERMINAL_RUNTIME_LOCK_PATH", str(database_path.parent / "application.lock"))
    with TestClient(create_app(database_path)) as client:
        register_employee(database_path)
        response = client.post(
            "/inspection",
            data=inspection_form(client, status="abnormal", reason="確認できない"),
            follow_redirects=False,
        )
        assert response.status_code == 303

    with TestClient(create_app(database_path)) as restarted_client:
        history = restarted_client.get("/history")
        assert "記録のある日" in history.text
        recorded_date = datetime.now(JST).date().isoformat()
        daily = restarted_client.get(f"/history/{recorded_date}")
        assert "確認できない" in daily.text
        assert count_inspections(database_path) == 1


def test_save_failure_keeps_input_and_never_reports_success(
    client,
    database_path,
    monkeypatch,
):
    register_employee(database_path)

    def fail_save(*args, **kwargs):
        raise sqlite3.OperationalError("simulated disk error")

    monkeypatch.setattr(
        "office_terminal.services.inspection_service.repository.save_inspection",
        fail_save,
    )
    data = inspection_form(client, status="abnormal", reason="確認できない")
    response = client.post("/inspection", data=data)

    assert response.status_code == 503
    assert "保存に失敗しました" in response.text
    assert "確認できない" in response.text
    assert "点検記録を保存しました" not in response.text
    assert count_inspections(database_path) == 0


def test_stale_restore_epoch_is_rejected_but_current_epoch_is_saved(
    client,
    database_path,
):
    register_employee(database_path)
    stale_data = inspection_form(client)
    stale_data["restore_epoch"] = "before-restoration"
    stale_data["draft_revision"] = str(uuid4())
    operations_path = database_path.parent / "operations.json"
    operations_path.write_text('{"restore_epoch":"restored-generation"}', encoding="utf-8")

    stale_response = client.post("/inspection", data=stale_data)

    assert stale_response.status_code == 409
    assert "復元前に開いた点検画面から送信されたため" in stale_response.text
    assert 'data-discard-target-available="true"' in stale_response.text
    assert f'data-discard-idempotency-key="{stale_data["idempotency_key"]}"' in stale_response.text
    assert f'data-discard-draft-revision="{stale_data["draft_revision"]}"' in stale_response.text
    assert "点検記録は保存されませんでした" in stale_response.text
    assert count_inspections(database_path) == 0

    current_data = inspection_form(client)
    assert current_data["restore_epoch"] == "restored-generation"
    current_response = client.post("/inspection", data=current_data, follow_redirects=False)
    assert current_response.status_code == 303
    assert count_inspections(database_path) == 1


def test_missing_or_unreadable_restore_epoch_is_rejected(client, database_path):
    register_employee(database_path)
    form_data = inspection_form(client)
    del form_data["restore_epoch"]
    missing_response = client.post("/inspection", data=form_data)
    assert missing_response.status_code == 409
    assert "画面の復元世代情報を確認できないため" in missing_response.text
    assert 'data-discard-target-available="false"' in missing_response.text
    assert count_inspections(database_path) == 0

    operations_path = database_path.parent / "operations.json"
    operations_path.write_text('{"restore_epoch":"restored-generation"}', encoding="utf-8")
    form_data = inspection_form(client)
    form_data["restore_epoch"] = ""
    empty_response = client.post("/inspection", data=form_data)
    assert empty_response.status_code == 409
    assert "画面の復元世代情報を確認できないため" in empty_response.text
    assert 'data-discard-target-available="false"' in empty_response.text
    assert count_inspections(database_path) == 0

    operations_path.write_text("{invalid json", encoding="utf-8")
    unreadable_response = client.post("/inspection", data=inspection_form(client))
    assert unreadable_response.status_code == 503
    assert "復元世代を確認できない" in unreadable_response.text
    assert count_inspections(database_path) == 0


def test_failure_while_saving_item_rolls_back_the_whole_record(client, database_path):
    register_employee(database_path)
    with connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_lights BEFORE INSERT ON inspection_items
            WHEN NEW.item_code = 'lights'
            BEGIN
                SELECT RAISE(ABORT, 'simulated item write failure');
            END
            """
        )

    response = client.post(
        "/inspection",
        data=inspection_form(client),
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert "保存に失敗しました" in response.text
    assert count_inspections(database_path) == 0
    with connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM inspection_items").fetchone()[0] == 0


def test_free_text_and_notes_are_rejected_but_history_keeps_legacy_text(client, database_path):
    register_employee(database_path)
    free_text = inspection_form(client, status="abnormal")
    free_text["reason_lights"] = "<script>alert(1)</script>"
    assert client.post("/inspection", data=free_text).status_code == 422

    note = inspection_form(client)
    note["note"] = "<script>alert(1)</script>"
    assert client.post("/inspection", data=note).status_code == 422
    assert count_inspections(database_path) == 0

    legacy_note = "<script>legacy note</script>"
    legacy_reason = "<script>legacy reason</script>"
    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO inspections (
                idempotency_key, employee_id, employee_name, recorded_at,
                overall_status, note
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                "DEV-001",
                "開発用 山田",
                datetime.now(JST).isoformat(timespec="seconds"),
                "attention",
                legacy_note,
            ),
        )
        connection.executemany(
            """
            INSERT INTO inspection_items (inspection_id, item_code, result, reason)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    cursor.lastrowid,
                    item["code"],
                    "abnormal" if item["code"] == "lights" else "confirmed",
                    legacy_reason if item["code"] == "lights" else "",
                )
                for item in ITEM_DEFINITIONS
            ],
        )

    target_date = datetime.now(JST).date().isoformat()
    history = client.get(f"/history/{target_date}")
    assert legacy_reason not in history.text
    assert legacy_note not in history.text
    assert "&lt;script&gt;legacy reason&lt;/script&gt;" in history.text
    assert "&lt;script&gt;legacy note&lt;/script&gt;" in history.text
    assert "記録なし" in client.get("/history/2026-01-01").text
    redirect = client.get("/history?target_date=2026-01-01", follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/history/2026-01-01"
