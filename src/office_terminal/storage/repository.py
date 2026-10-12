import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from office_terminal.storage.database import connect

JST = ZoneInfo("Asia/Tokyo")


class InspectionAttendanceConflict(Exception):
    def __init__(self, employees: list[dict[str, str]]):
        self.employees = employees
        super().__init__("他の有効社員が在室または離席中です")


class InactiveInspectionEmployee(Exception):
    pass


def list_active_employees(database_path: Path) -> list[dict[str, Any]]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT employee_id, display_name
            FROM employees
            WHERE is_active = 1
            ORDER BY employee_id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_active_employee(database_path: Path, employee_id: str) -> dict[str, Any] | None:
    with connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT employee_id, display_name
            FROM employees
            WHERE employee_id = ? AND is_active = 1
            """,
            (employee_id,),
        ).fetchone()
    return dict(row) if row else None


def _get_active_attendance_conflicts(
    connection: sqlite3.Connection,
    excluded_employee_id: str,
) -> list[dict[str, str]]:
    rows = connection.execute(
        """
        SELECT employee.display_name, attendance.status
        FROM employees AS employee
        JOIN employee_attendance AS attendance USING (employee_id)
        WHERE employee.is_active = 1
          AND employee.employee_id != ?
          AND attendance.status IN ('在室', '離席中')
        ORDER BY employee.employee_id
        """,
        (excluded_employee_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_active_attendance_conflicts(
    database_path: Path,
    excluded_employee_id: str,
) -> list[dict[str, str]]:
    with connect(database_path) as connection:
        return _get_active_attendance_conflicts(connection, excluded_employee_id)


def add_employee(
    database_path: Path,
    employee_id: str,
    display_name: str,
    is_active: bool = True,
) -> None:
    with connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO employees (employee_id, display_name, is_active)
            VALUES (?, ?, ?)
            ON CONFLICT(employee_id) DO NOTHING
            """,
            (employee_id, display_name, int(is_active)),
        )


def import_employees(
    database_path: Path,
    employees: list[tuple[str, str, bool]],
) -> tuple[int, int, int]:
    added = updated = unchanged = 0
    with connect(database_path) as connection:
        with connection:
            for employee_id, display_name, is_active in employees:
                existing = connection.execute(
                    "SELECT display_name, is_active FROM employees WHERE employee_id = ?",
                    (employee_id,),
                ).fetchone()
                active_value = int(is_active)
                if existing is None:
                    connection.execute(
                        "INSERT INTO employees (employee_id, display_name, is_active) VALUES (?, ?, ?)",
                        (employee_id, display_name, active_value),
                    )
                    added += 1
                elif (
                    existing["display_name"] != display_name
                    or existing["is_active"] != active_value
                ):
                    connection.execute(
                        "UPDATE employees SET display_name = ?, is_active = ? WHERE employee_id = ?",
                        (display_name, active_value, employee_id),
                    )
                    updated += 1
                else:
                    unchanged += 1
    return added, updated, unchanged


def save_inspection(
    database_path: Path,
    *,
    idempotency_key: str,
    employee_id: str,
    recorded_at: datetime,
    overall_status: str,
    results: list[dict[str, str]],
    note: str,
) -> tuple[int, bool, str]:
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = connection.execute(
                """
                SELECT inspection_id, overall_status
                FROM inspections
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                return int(existing["inspection_id"]), True, existing["overall_status"]

            employee = connection.execute(
                """
                SELECT display_name
                FROM employees
                WHERE employee_id = ? AND is_active = 1
                """,
                (employee_id,),
            ).fetchone()
            if employee is None:
                raise InactiveInspectionEmployee
            employee_name = employee["display_name"]

            everyone_left_confirmed = any(
                item["code"] == "everyone_left" and item["result"] == "confirmed"
                for item in results
            )
            if everyone_left_confirmed:
                conflicts = _get_active_attendance_conflicts(connection, employee_id)
                if conflicts:
                    raise InspectionAttendanceConflict(conflicts)

            cursor = connection.execute(
                """
                INSERT INTO inspections (
                    idempotency_key, employee_id, employee_name,
                    recorded_at, overall_status, note
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    employee_id,
                    employee_name,
                    recorded_at.isoformat(timespec="seconds"),
                    overall_status,
                    note,
                ),
            )
            inspection_id = cursor.lastrowid
            connection.executemany(
                """
                INSERT INTO inspection_items (inspection_id, item_code, result, reason)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (inspection_id, item["code"], item["result"], item["reason"])
                    for item in results
                ],
            )
            attendance = connection.execute(
                "SELECT status FROM employee_attendance WHERE employee_id = ?",
                (employee_id,),
            ).fetchone()
            previous_status = attendance["status"] if attendance else "退勤済み"
            if previous_status != "退勤済み":
                changed_at = recorded_at.isoformat(timespec="seconds")
                connection.execute(
                    """
                    INSERT INTO employee_attendance (employee_id, status, updated_at)
                    VALUES (?, '退勤済み', ?)
                    ON CONFLICT(employee_id) DO UPDATE SET
                        status = excluded.status,
                        updated_at = excluded.updated_at
                    """,
                    (employee_id, changed_at),
                )
                connection.execute(
                    """
                    INSERT INTO attendance_history (
                        employee_id, employee_name, previous_status, new_status,
                        operation, idempotency_key, changed_at
                    ) VALUES (?, ?, ?, '退勤済み', '退勤', ?, ?)
                    """,
                    (
                        employee_id,
                        employee_name,
                        previous_status,
                        f"inspection:{idempotency_key}",
                        changed_at,
                    ),
                )
            connection.commit()
            return int(inspection_id), False, overall_status
        except sqlite3.IntegrityError:
            connection.rollback()
            existing = connection.execute(
                """
                SELECT inspection_id, overall_status
                FROM inspections
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing is None:
                raise
            return int(existing["inspection_id"]), True, existing["overall_status"]


def get_daily_inspections(database_path: Path, target_date: date) -> list[dict[str, Any]]:
    with connect(database_path) as connection:
        inspections = connection.execute(
            """
            SELECT inspection_id, employee_id, employee_name, recorded_at,
                   overall_status, note
            FROM inspections
            WHERE substr(recorded_at, 1, 10) = ?
            ORDER BY recorded_at ASC, inspection_id ASC
            """,
            (target_date.isoformat(),),
        ).fetchall()
        records = [dict(row) for row in inspections]
        for record in records:
            items = connection.execute(
                """
                SELECT item_code, result, reason
                FROM inspection_items
                WHERE inspection_id = ?
                ORDER BY item_code
                """,
                (record["inspection_id"],),
            ).fetchall()
            record["items"] = [dict(item) for item in items]
    return records


def list_inspection_dates(database_path: Path) -> list[str]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT substr(recorded_at, 1, 10) AS recorded_date
            FROM inspections
            ORDER BY recorded_date DESC
            """
        ).fetchall()
    return [row["recorded_date"] for row in rows]


def get_latest_month_statuses(
    database_path: Path,
    start_date: date,
    end_date: date,
) -> dict[date, str]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT inspection_id, recorded_at, overall_status
            FROM inspections
            WHERE recorded_at >= ? AND recorded_at < ?
            ORDER BY recorded_at DESC, inspection_id DESC
            """,
            (
                datetime.combine(start_date, time.min, JST).isoformat(),
                datetime.combine(end_date, time.min, JST).isoformat(),
            ),
        ).fetchall()

    latest_by_date: dict[date, str] = {}
    for row in rows:
        recorded_date = date.fromisoformat(row["recorded_at"][:10])
        calendar_status = (
            "complete" if row["overall_status"] == "complete" else "alert"
        )
        latest_by_date.setdefault(recorded_date, calendar_status)
    return latest_by_date


def get_attendance_arrival_dates(
    database_path: Path,
    start_date: date,
    end_date: date,
) -> set[date]:
    """Return Japan-local dates with at least one arrival in the requested range."""
    if end_date <= start_date:
        raise ValueError("end_date must be after start_date")

    # Stored timestamps may use offsets other than JST; the wider SQL window lets
    # the Python conversion below apply the exact local-date boundary.
    lower_bound = (start_date - timedelta(days=2)).isoformat()
    upper_bound = (end_date + timedelta(days=2)).isoformat()
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT changed_at
            FROM attendance_history
            WHERE operation = '出社'
              AND changed_at >= ? AND changed_at < ?
            """,
            (lower_bound, upper_bound),
        ).fetchall()

    dates: set[date] = set()
    for row in rows:
        changed_at = datetime.fromisoformat(row["changed_at"])
        if changed_at.tzinfo is None:
            changed_at = changed_at.replace(tzinfo=JST)
        local_date = changed_at.astimezone(JST).date()
        if start_date <= local_date < end_date:
            dates.add(local_date)
    return dates
