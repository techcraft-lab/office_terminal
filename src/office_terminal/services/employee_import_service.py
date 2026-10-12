import csv
from pathlib import Path

from office_terminal.services.backup_service import (
    BackupError,
    RuntimeLock,
    acquire_backup_lock,
    runtime_lock_path,
    state_path,
)
from office_terminal.storage.database import initialize_database
from office_terminal.storage.repository import import_employees

REQUIRED_HEADERS = ("employee_id", "display_name", "is_active")


class EmployeeImportError(Exception):
    pass


def _read_employee_csv(csv_path: Path) -> list[tuple[str, str, bool]]:
    employees: list[tuple[str, str, bool]] = []
    seen_ids: set[str] = set()
    try:
        with csv_path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.reader(source, strict=True)
            try:
                headers = next(reader)
            except StopIteration as error:
                raise EmployeeImportError("1行目: CSVにヘッダーがありません。") from error

            if len(headers) != len(set(headers)):
                raise EmployeeImportError("1行目: ヘッダーが重複しています。")
            missing = [header for header in REQUIRED_HEADERS if header not in headers]
            if missing:
                raise EmployeeImportError(
                    f"1行目: 必須ヘッダーがありません: {', '.join(missing)}"
                )
            unexpected = [header for header in headers if header not in REQUIRED_HEADERS]
            if unexpected:
                raise EmployeeImportError(
                    f"1行目: 未対応のヘッダーがあります: {', '.join(unexpected)}"
                )
            indexes = [headers.index(header) for header in REQUIRED_HEADERS]

            for row in reader:
                line_number = reader.line_num
                if len(row) != len(headers):
                    raise EmployeeImportError(
                        f"{line_number}行目: 列数がヘッダーと一致しません。"
                    )
                employee_id = row[indexes[0]].strip()
                display_name = row[indexes[1]].strip()
                is_active = row[indexes[2]]
                if not employee_id:
                    raise EmployeeImportError(f"{line_number}行目: 社員番号が空欄です。")
                if not display_name:
                    raise EmployeeImportError(f"{line_number}行目: 氏名が空欄です。")
                if is_active not in {"0", "1"}:
                    raise EmployeeImportError(
                        f"{line_number}行目: is_activeは0または1を指定してください。"
                    )
                if employee_id in seen_ids:
                    raise EmployeeImportError(
                        f"{line_number}行目: 社員番号 {employee_id} が重複しています。"
                    )
                seen_ids.add(employee_id)
                employees.append((employee_id, display_name, is_active == "1"))
    except EmployeeImportError:
        raise
    except (OSError, UnicodeError, csv.Error) as error:
        line_number = getattr(locals().get("reader"), "line_num", 0)
        location = f"{line_number}行目: " if line_number else ""
        raise EmployeeImportError(f"{location}CSVを読み込めません: {error}") from error
    return employees


def import_employee_csv(
    csv_path: Path,
    database_path: Path,
    *,
    runtime_path: Path | None = None,
    backup_lock_path: Path | None = None,
) -> tuple[int, int, int]:
    runtime_lock = RuntimeLock(runtime_path or runtime_lock_path())
    try:
        if not runtime_lock.acquire():
            raise EmployeeImportError("アプリ稼働中は社員CSVを取り込めません。アプリを停止してください。")
    except OSError as error:
        raise EmployeeImportError("アプリの稼働状態を確認できません。") from error

    backup_lock = None
    try:
        try:
            backup_lock = acquire_backup_lock(
                backup_lock_path or state_path().with_name("backup.lock")
            )
        except BackupError as error:
            raise EmployeeImportError(
                "バックアップまたは復元処理中は社員CSVを取り込めません。"
            ) from error

        employees = _read_employee_csv(csv_path)
        initialize_database(database_path)
        return import_employees(database_path, employees)
    finally:
        try:
            if backup_lock is not None:
                backup_lock.release()
        finally:
            runtime_lock.release()
