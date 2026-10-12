from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from office_terminal.storage.database import connect

JST = ZoneInfo("Asia/Tokyo")
DEFAULT_STATUS = "退勤済み"
ATTENDANCE_HISTORY_PAGE_SIZE = 20
OPERATIONS = {
    "出社": (("退勤済み",), "在室"),
    "離席": (("在室",), "離席中"),
    "戻り": (("離席中",), "在室"),
    "退勤": (("在室", "離席中"), "退勤済み"),
}


class AttendanceTransitionError(Exception):
    def __init__(self, message: str, current_status: str | None = None):
        self.current_status = current_status
        super().__init__(message)


@dataclass(frozen=True)
class AttendanceChange:
    current_status: str
    operation: str
    duplicate: bool


def change_attendance(
    database_path: Path,
    *,
    employee_id: str,
    operation: str,
    idempotency_key: str,
    changed_at: datetime | None = None,
) -> AttendanceChange:
    try:
        UUID(idempotency_key)
    except (ValueError, AttributeError) as error:
        raise AttendanceTransitionError("画面を再読み込みしてから、もう一度お試しください。") from error

    if operation not in OPERATIONS:
        raise AttendanceTransitionError("操作を選択してください。")
    timestamp = (changed_at or datetime.now(JST)).astimezone(JST).isoformat(timespec="seconds")

    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        employee = connection.execute(
            """
            SELECT employee.employee_id, employee.display_name,
                   COALESCE(attendance.status, ?) AS status
            FROM employees AS employee
            LEFT JOIN employee_attendance AS attendance USING (employee_id)
            WHERE employee.employee_id = ? AND employee.is_active = 1
            """,
            (DEFAULT_STATUS, employee_id),
        ).fetchone()
        if employee is None:
            raise AttendanceTransitionError("有効な社員を選択してください。")

        prior_operation = connection.execute(
            """
            SELECT employee_id, operation, new_status
            FROM attendance_history
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        if prior_operation is not None:
            if (
                prior_operation["employee_id"] != employee_id
                or prior_operation["operation"] != operation
            ):
                raise AttendanceTransitionError("この操作キーは別の操作に使用済みです。")
            connection.rollback()
            return AttendanceChange(employee["status"], operation, True)

        previous_status = employee["status"]
        allowed_previous, new_status = OPERATIONS[operation]
        if previous_status not in allowed_previous:
            raise AttendanceTransitionError(
                f"現在の状態は「{previous_status}」です。「{operation}」は実行できません。",
                current_status=previous_status,
            )

        connection.execute(
            """
            INSERT INTO employee_attendance (employee_id, status, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(employee_id) DO UPDATE SET
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (employee_id, new_status, timestamp),
        )
        connection.execute(
            """
            INSERT INTO attendance_history (
                employee_id, employee_name, previous_status, new_status,
                operation, idempotency_key, changed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                employee_id,
                employee["display_name"],
                previous_status,
                new_status,
                operation,
                idempotency_key,
                timestamp,
            ),
        )
        connection.commit()
        return AttendanceChange(new_status, operation, False)


def list_active_attendance(database_path: Path) -> list[dict[str, str]]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT employee.employee_id, employee.display_name,
                   COALESCE(attendance.status, ?) AS status
            FROM employees AS employee
            LEFT JOIN employee_attendance AS attendance USING (employee_id)
            WHERE employee.is_active = 1
            ORDER BY employee.employee_id
            """,
            (DEFAULT_STATUS,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_current_attendees(database_path: Path) -> dict:
    people = [
        {
            "employee_id": employee["employee_id"],
            "name": employee["display_name"],
            "status": employee["status"],
        }
        for employee in list_active_attendance(database_path)
        if employee["status"] in {"在室", "離席中"}
    ]
    return {
        "people": people,
        "present_count": sum(person["status"] == "在室" for person in people),
        "away_count": sum(person["status"] == "離席中" for person in people),
    }


def list_attendance_history(
    database_path: Path,
    *,
    target_date: date,
    employee_id: str | None,
    page: int,
    page_size: int = ATTENDANCE_HISTORY_PAGE_SIZE,
) -> tuple[list[dict], int, list[dict]]:
    offset = (page - 1) * page_size
    with connect(database_path) as connection:
        connection.execute("BEGIN")
        count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM attendance_history
                WHERE substr(changed_at, 1, 10) = ?
                  AND (? IS NULL OR employee_id = ?)
                """,
                (
                    target_date.isoformat(),
                    employee_id,
                    employee_id,
                ),
            ).fetchone()[0]
        )
        rows = connection.execute(
            """
            SELECT attendance_history_id, employee_id, employee_name,
                   changed_at, operation, new_status, idempotency_key
            FROM attendance_history
            WHERE substr(changed_at, 1, 10) = ?
              AND (? IS NULL OR employee_id = ?)
            ORDER BY changed_at DESC, attendance_history_id DESC
            LIMIT ? OFFSET ?
            """,
            (
                target_date.isoformat(),
                employee_id,
                employee_id,
                page_size,
                offset,
            ),
        ).fetchall()
        employees = connection.execute(
            """
            SELECT history.employee_id, history.employee_name
            FROM attendance_history AS history
            JOIN (
                SELECT employee_id, MAX(attendance_history_id) AS latest_id
                FROM attendance_history
                GROUP BY employee_id
            ) AS latest
              ON latest.latest_id = history.attendance_history_id
            ORDER BY history.employee_id
            """
        ).fetchall()
    return (
        [dict(row) for row in rows],
        count,
        [dict(row) for row in employees],
    )
