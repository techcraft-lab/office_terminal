import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from office_terminal.services.backup_service import RuntimeLock
from office_terminal.services.attendance_service import change_attendance
from office_terminal.services.employee_import_service import (
    EmployeeImportError,
    import_employee_csv,
)
from office_terminal.services.inspection_service import (
    ITEM_DEFINITIONS,
    InspectionSubmission,
    validate_and_save_inspection,
)
from office_terminal.storage.database import connect, initialize_database
from office_terminal.storage.repository import add_employee, get_daily_inspections

JST = ZoneInfo("Asia/Tokyo")
REQUIRED_HEADER = "employee_id,display_name,is_active\n"


def import_csv(csv_path: Path, database_path: Path, tmp_path: Path):
    return import_employee_csv(
        csv_path,
        database_path,
        runtime_path=tmp_path / "application.lock",
        backup_lock_path=tmp_path / "backup.lock",
    )


def test_csv_import_adds_updates_disables_preserves_ids_and_history(tmp_path):
    database_path = tmp_path / "office_terminal.db"
    initialize_database(database_path)
    add_employee(database_path, "001", "旧氏名")
    add_employee(database_path, "004", "変更なし")
    add_employee(database_path, "KEEP", "CSVにない社員")
    change_attendance(
        database_path,
        employee_id="001",
        operation="出社",
        idempotency_key=str(uuid4()),
    )

    validate_and_save_inspection(
        database_path,
        InspectionSubmission(
            idempotency_key=str(uuid4()),
            employee_id="001",
            results={item["code"]: "confirmed" for item in ITEM_DEFINITIONS},
            reasons={item["code"]: "" for item in ITEM_DEFINITIONS},
            note="",
        ),
        datetime(2026, 10, 3, 10, 0, tzinfo=JST),
    )
    change_attendance(
        database_path,
        employee_id="001",
        operation="出社",
        idempotency_key=str(uuid4()),
    )

    csv_path = tmp_path / "employees.csv"
    csv_path.write_text(
        "\ufeff"
        + REQUIRED_HEADER
        + " 001 , 更新後の氏名 ,0\n"
        + "002,新規社員,1\n"
        + "003,無効社員,0\n"
        + "004,変更なし,1\n",
        encoding="utf-8",
    )

    assert import_csv(csv_path, database_path, tmp_path) == (2, 1, 1)
    with connect(database_path) as connection:
        employees = {
            row["employee_id"]: (row["display_name"], row["is_active"])
            for row in connection.execute(
                "SELECT employee_id, display_name, is_active FROM employees"
            )
        }
    assert employees == {
        "001": ("更新後の氏名", 0),
        "002": ("新規社員", 1),
        "003": ("無効社員", 0),
        "004": ("変更なし", 1),
        "KEEP": ("CSVにない社員", 1),
    }
    with connect(database_path) as connection:
        assert connection.execute(
            "SELECT status FROM employee_attendance WHERE employee_id = '001'"
        ).fetchone()["status"] == "在室"
        assert connection.execute(
            "SELECT COUNT(*) FROM attendance_history WHERE employee_id = '001'"
        ).fetchone()[0] == 3
    history = get_daily_inspections(database_path, datetime(2026, 10, 3).date())
    assert history[0]["employee_name"] == "旧氏名"
    assert import_csv(csv_path, database_path, tmp_path) == (0, 0, 4)

    csv_path.write_text(
        REQUIRED_HEADER + "003,再有効化,1\n",
        encoding="utf-8",
    )
    assert import_csv(csv_path, database_path, tmp_path) == (0, 1, 0)
    with connect(database_path) as connection:
        assert connection.execute(
            "SELECT is_active FROM employees WHERE employee_id = '003'"
        ).fetchone()["is_active"] == 1


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("employee_id,display_name\n001,名前\n", "必須ヘッダー"),
        ("employee_id,display_name,is_active,extra\n001,名前,1,x\n", "未対応のヘッダー"),
        (REQUIRED_HEADER + "001,名前\n", "列数"),
        (REQUIRED_HEADER + "001,名前,yes\n", "is_active"),
        (REQUIRED_HEADER + " ,名前,1\n", "社員番号が空欄"),
        (REQUIRED_HEADER + "001,  ,1\n", "氏名が空欄"),
        (REQUIRED_HEADER + "001,名前,1\n001,重複,0\n", "重複"),
    ],
)
def test_invalid_csv_is_rejected_without_partial_changes(tmp_path, body, message):
    database_path = tmp_path / "office_terminal.db"
    initialize_database(database_path)
    add_employee(database_path, "001", "元の氏名")
    csv_path = tmp_path / "invalid.csv"
    csv_path.write_text(body, encoding="utf-8")

    with pytest.raises(EmployeeImportError, match=message):
        import_csv(csv_path, database_path, tmp_path)

    with connect(database_path) as connection:
        employees = connection.execute(
            "SELECT employee_id, display_name, is_active FROM employees"
        ).fetchall()
    assert [tuple(row) for row in employees] == [("001", "元の氏名", 1)]


@pytest.mark.parametrize("operation", ["application", "backup", "restore"])
def test_import_is_rejected_while_application_backup_or_restore_lock_is_held(
    tmp_path,
    operation,
):
    database_path = tmp_path / "office_terminal.db"
    initialize_database(database_path)
    add_employee(database_path, "001", "現状維持")
    csv_path = tmp_path / "employees.csv"
    csv_path.write_text(REQUIRED_HEADER + "002,取込対象,1\n", encoding="utf-8")
    runtime_lock = RuntimeLock(tmp_path / "application.lock")
    backup_lock = RuntimeLock(tmp_path / "backup.lock")
    if operation in {"application", "restore"}:
        assert runtime_lock.acquire()
    if operation in {"backup", "restore"}:
        assert backup_lock.acquire()
    try:
        with pytest.raises(EmployeeImportError):
            import_csv(csv_path, database_path, tmp_path)
    finally:
        if backup_lock.file is not None:
            backup_lock.release()
        if runtime_lock.file is not None:
            runtime_lock.release()

    with connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 1
        assert connection.execute(
            "SELECT display_name FROM employees WHERE employee_id = '001'"
        ).fetchone()["display_name"] == "現状維持"


def test_import_cli_reports_counts(tmp_path):
    database_path = tmp_path / "office_terminal.db"
    csv_path = tmp_path / "employees.csv"
    csv_path.write_text(
        REQUIRED_HEADER + "001,開発用 山田,1\n002,開発用 佐藤,1\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "OFFICE_TERMINAL_DB_PATH": str(database_path),
            "OFFICE_TERMINAL_RUNTIME_LOCK_PATH": str(tmp_path / "application.lock"),
            "OFFICE_TERMINAL_BACKUP_STATE_PATH": str(tmp_path / "backup-state.json"),
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "office_terminal.cli",
            "import-employees",
            str(csv_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert "追加 2件、更新 0件、変更なし 0件" in completed.stdout
