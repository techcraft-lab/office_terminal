import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_DATABASE_PATH = Path.home() / ".local/share/office_terminal/office_terminal.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    employee_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS inspections (
    inspection_id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    employee_id TEXT NOT NULL REFERENCES employees(employee_id),
    employee_name TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    overall_status TEXT NOT NULL CHECK (overall_status IN ('complete', 'attention')),
    note TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_inspections_recorded_at
    ON inspections(recorded_at DESC, inspection_id DESC);

CREATE TABLE IF NOT EXISTS inspection_items (
    inspection_id INTEGER NOT NULL REFERENCES inspections(inspection_id) ON DELETE CASCADE,
    item_code TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('confirmed', 'abnormal', 'not_done')),
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (inspection_id, item_code)
);

CREATE TABLE IF NOT EXISTS employee_attendance (
    employee_id TEXT PRIMARY KEY REFERENCES employees(employee_id),
    status TEXT NOT NULL CHECK (status IN ('在室', '離席中', '退勤済み')),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attendance_history (
    attendance_history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id TEXT NOT NULL REFERENCES employees(employee_id),
    employee_name TEXT NOT NULL,
    previous_status TEXT NOT NULL CHECK (previous_status IN ('在室', '離席中', '退勤済み')),
    new_status TEXT NOT NULL CHECK (new_status IN ('在室', '離席中', '退勤済み')),
    operation TEXT NOT NULL CHECK (operation IN ('出社', '離席', '戻り', '退勤')),
    idempotency_key TEXT NOT NULL UNIQUE,
    changed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attendance_history_employee_changed
    ON attendance_history(employee_id, changed_at DESC, attendance_history_id DESC);
"""


def get_database_path() -> Path:
    configured_path = os.environ.get("OFFICE_TERMINAL_DB_PATH")
    return Path(configured_path).expanduser() if configured_path else DEFAULT_DATABASE_PATH


@contextmanager
def connect(database_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(database_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        file_descriptor = os.open(
            database_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError:
        pass
    else:
        os.close(file_descriptor)
    with connect(database_path) as connection:
        connection.executescript(SCHEMA)
