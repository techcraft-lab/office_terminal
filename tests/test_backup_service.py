import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from office_terminal.main import create_app
from office_terminal.services.backup_service import (
    BACKUP_FORMAT_VERSION,
    BackupBusyError,
    BackupConfig,
    BackupError,
    BackupManager,
    BackupValidationError,
    RuntimeLock,
    create_backup,
    prune_backups,
    restore_backup,
    verify_backup,
)
from office_terminal.services import backup_service
from office_terminal.services.inspection_service import InspectionSubmission, validate_and_save_inspection
from office_terminal.storage.database import connect, initialize_database
from office_terminal.storage.repository import add_employee

JST = ZoneInfo("Asia/Tokyo")


@pytest.fixture
def backup_environment(tmp_path: Path):
    database = tmp_path / "office_terminal.db"
    initialize_database(database)
    add_employee(database, "EMP-01", "点検担当")
    destination = tmp_path / "backup"
    operations = tmp_path / "operations.json"
    state = tmp_path / "backup-state.json"
    config = BackupConfig(backup_directory=destination, allow_unmounted=True)
    return database, destination, operations, state, config


def save_inspection(database: Path, token: str) -> None:
    codes = ["air_conditioner", "windows", "lights", "equipment", "everyone_left"]
    validate_and_save_inspection(
        database,
        InspectionSubmission(
            idempotency_key=token,
            employee_id="EMP-01",
            results={code: "confirmed" for code in codes},
            reasons={code: "" for code in codes},
            note="",
        ),
        datetime(2026, 10, 3, 8, 0, tzinfo=JST),
    )


def test_backup_is_verified_unique_and_excludes_unapproved_config(backup_environment):
    database, destination, operations, _, config = backup_environment
    save_inspection(database, str(uuid4()))
    operations.write_text(
        json.dumps(
            {
                "backup": config.to_dict(),
                "restore_epoch": "before",
                "PRIVATE_TOKEN": "must-not-be-archived",
            }
        ),
        encoding="utf-8",
    )

    first = create_backup(database, config, now=datetime(2026, 10, 3, 2, 0, tzinfo=JST), operations_config_path=operations)
    second = create_backup(database, config, now=datetime(2026, 10, 3, 2, 0, tzinfo=JST), operations_config_path=operations)

    assert first != second
    manifest, restored_config = verify_backup(first)
    assert manifest["format_version"] == BACKUP_FORMAT_VERSION
    assert manifest["application_version"]
    assert manifest["saved_at"] == "2026-10-03T02:00:00+09:00"
    assert restored_config.backup_directory == destination
    with zipfile.ZipFile(first) as archive:
        assert set(archive.namelist()) == {"database.sqlite3", "operations.json", "manifest.json"}
        archived_settings = archive.read("operations.json")
        assert b"must-not-be-archived" not in archived_settings
        assert b"PRIVATE_TOKEN" not in archived_settings
    assert first.stat().st_mode & 0o777 == 0o600


def test_backup_rejects_corruption_and_keeps_existing_backups(backup_environment, tmp_path):
    database, destination, operations, _, config = backup_environment
    valid = create_backup(database, config, operations_config_path=operations)
    corrupt = destination / "office-terminal-backup-20261003T030000000000-abcd1234.zip"
    with zipfile.ZipFile(valid) as original, zipfile.ZipFile(corrupt, "w") as damaged:
        for name in original.namelist():
            data = original.read(name)
            if name == "database.sqlite3":
                data += b"tampered"
            damaged.writestr(name, data)

    with pytest.raises(BackupValidationError):
        verify_backup(corrupt)
    assert valid.exists()


def test_failed_snapshot_never_publishes_partial_archive_or_deletes_good_backup(
    backup_environment,
    monkeypatch,
):
    database, destination, operations, _, config = backup_environment
    existing = create_backup(database, config, operations_config_path=operations)

    def fail_after_partial_snapshot(_source, output):
        output.write_bytes(b"partial snapshot")
        raise BackupError("simulated full disk")

    monkeypatch.setattr(backup_service, "_sqlite_snapshot", fail_after_partial_snapshot)
    with pytest.raises(BackupError, match="full disk"):
        create_backup(database, config, operations_config_path=operations)

    assert existing.exists()
    assert list(destination.glob("office-terminal-backup-*.zip")) == [existing]
    assert not list(destination.glob(".office-terminal-backup-*.tmp"))
    assert not list(destination.glob(".office-terminal-backup-*"))


def test_unwritable_target_is_a_safe_failure(backup_environment, monkeypatch):
    database, destination, operations, _, config = backup_environment
    monkeypatch.setattr(backup_service.os, "access", lambda *_args: False)

    with pytest.raises(BackupError, match="書き込めません"):
        create_backup(database, config, operations_config_path=operations)
    assert not list(destination.glob("*.zip"))


def test_static_directory_cannot_be_a_backup_destination(backup_environment):
    database, _, operations, _, _ = backup_environment
    static_path = Path(backup_service.__file__).parents[1] / "static" / "backup-test"
    config = BackupConfig(backup_directory=static_path, allow_unmounted=True)

    with pytest.raises(BackupError, match="静的ファイル"):
        create_backup(database, config, operations_config_path=operations)


def test_snapshot_is_consistent_while_inspection_is_saved(backup_environment):
    database, _, operations, _, config = backup_environment
    async def write_and_backup():
        before = asyncio.create_task(asyncio.to_thread(save_inspection, database, str(uuid4())))
        during = asyncio.create_task(
            asyncio.to_thread(
                create_backup,
                database,
                config,
                operations_config_path=operations,
            )
        )
        await asyncio.gather(before, during)
        save_inspection(database, str(uuid4()))

    asyncio.run(write_and_backup())
    latest = sorted(config.backup_directory.glob("*.zip"))[-1]
    with tempfile.TemporaryDirectory() as directory:
        restored_database = Path(directory) / "restored.db"
        restore_backup(
            latest,
            restored_database,
            config_file=operations,
            lock_path=operations.with_name("runtime.lock"),
            execution_lock_path=operations.with_name("backup.lock"),
            confirmed=True,
        )
        with connect(restored_database) as connection:
            count = connection.execute("SELECT COUNT(*) FROM inspections").fetchone()[0]
            item_count = connection.execute("SELECT COUNT(*) FROM inspection_items").fetchone()[0]
        assert count in {0, 1}
        assert item_count == count * 5


def test_mount_must_be_present_unless_development_override_is_explicit(backup_environment, tmp_path):
    database, destination, operations, _, _ = backup_environment
    config = BackupConfig(backup_directory=destination, mount_point=tmp_path)

    with pytest.raises(BackupError, match="接続されていません"):
        create_backup(database, config, operations_config_path=operations)
    assert not destination.exists()


def test_retention_removes_only_old_managed_files_and_keeps_latest(backup_environment):
    database, destination, operations, _, config = backup_environment
    old = create_backup(
        database,
        config,
        now=datetime(2026, 8, 1, 2, 0, tzinfo=JST),
        operations_config_path=operations,
    )
    unrelated_file = destination / "do-not-delete.txt"
    unrelated_file.write_text("untouched", encoding="utf-8")

    removed = prune_backups(destination, now=datetime(2026, 10, 3, 2, 0, tzinfo=JST))
    assert removed == []
    assert old.exists()

    newer = create_backup(
        database,
        config,
        now=datetime(2026, 10, 3, 2, 0, tzinfo=JST),
        operations_config_path=operations,
    )
    assert not old.exists()
    assert newer.exists()
    assert unrelated_file.read_text(encoding="utf-8") == "untouched"


def test_restore_recovers_database_settings_and_changes_restore_epoch(backup_environment, tmp_path):
    database, _, operations, _, config = backup_environment
    inspection_key = str(uuid4())
    save_inspection(database, inspection_key)
    config_document = {"backup": config.to_dict(), "restore_epoch": "old-epoch"}
    operations.write_text(json.dumps(config_document), encoding="utf-8")
    archive = create_backup(database, config, operations_config_path=operations)

    replacement = tmp_path / "replacement.db"
    initialize_database(replacement)
    add_employee(replacement, "OTHER", "置換前")
    replacement_settings = tmp_path / "replacement-operations.json"
    replacement_settings.write_text(json.dumps({"restore_epoch": "replacement"}), encoding="utf-8")
    lock_path = tmp_path / "runtime.lock"
    result = restore_backup(
        archive,
        replacement,
        config_file=replacement_settings,
        lock_path=lock_path,
        execution_lock_path=tmp_path / "backup.lock",
        confirmed=True,
    )

    with connect(replacement) as connection:
        employees = connection.execute("SELECT employee_id FROM employees").fetchall()
        inspections = connection.execute("SELECT idempotency_key FROM inspections").fetchall()
    restored_settings = json.loads(replacement_settings.read_text(encoding="utf-8"))
    assert [row[0] for row in employees] == ["EMP-01"]
    assert [row[0] for row in inspections] == [inspection_key]
    assert restored_settings["backup"]["schedule_time"] == "02:00"
    assert restored_settings["restore_epoch"] != "old-epoch"
    assert result["previous_database"].exists()
    assert result["previous_config"].exists()


def test_restore_requires_confirmation_and_rejects_running_application(backup_environment, tmp_path):
    database, _, operations, _, config = backup_environment
    archive = create_backup(database, config, operations_config_path=operations)
    target = tmp_path / "target.db"
    lock_path = tmp_path / "runtime.lock"
    lock = RuntimeLock(lock_path)
    assert lock.acquire()
    try:
        with pytest.raises(BackupError, match="アプリを停止"):
            restore_backup(
                archive,
                target,
                config_file=operations,
                lock_path=lock_path,
                execution_lock_path=tmp_path / "backup.lock",
                confirmed=True,
            )
    finally:
        lock.release()

    with pytest.raises(BackupError, match="キャンセル"):
        restore_backup(
            archive,
            target,
            config_file=operations,
            lock_path=lock_path,
            execution_lock_path=tmp_path / "backup.lock",
            confirmer=lambda _: "no",
        )
    assert not target.exists()


def test_application_startup_is_aborted_when_runtime_lock_is_held(tmp_path, monkeypatch):
    database = tmp_path / "office_terminal.db"
    runtime_path = tmp_path / "application.lock"
    monkeypatch.setenv("OFFICE_TERMINAL_RUNTIME_LOCK_PATH", str(runtime_path))
    with TestClient(create_app(database)) as first_client:
        with pytest.raises(RuntimeError, match="別のアプリが稼働中"):
            with TestClient(create_app(database)):
                pass

        assert first_client.get("/inspection").status_code == 200


def test_application_startup_is_aborted_when_runtime_lock_cannot_be_created(tmp_path, monkeypatch):
    database = tmp_path / "office_terminal.db"
    invalid_parent = tmp_path / "not-a-directory"
    invalid_parent.write_text("file", encoding="utf-8")
    monkeypatch.setenv(
        "OFFICE_TERMINAL_RUNTIME_LOCK_PATH",
        str(invalid_parent / "application.lock"),
    )

    with pytest.raises(RuntimeError, match="稼働ロックを取得できない"):
        with TestClient(create_app(database)):
            pass

    assert not database.exists()


def test_startup_failure_releases_the_runtime_lock(tmp_path, monkeypatch):
    runtime_path = tmp_path / "application.lock"
    monkeypatch.setenv("OFFICE_TERMINAL_RUNTIME_LOCK_PATH", str(runtime_path))
    invalid_parent = tmp_path / "database-parent"
    invalid_parent.write_text("file", encoding="utf-8")

    with pytest.raises(FileExistsError):
        with TestClient(create_app(invalid_parent / "office_terminal.db")):
            pass

    with TestClient(create_app(tmp_path / "recovered.db")) as client:
        assert client.get("/inspection").status_code == 200


def test_restore_rejects_corrupt_archive_without_touching_database(backup_environment, tmp_path):
    database, destination, operations, _, config = backup_environment
    archive = create_backup(database, config, operations_config_path=operations)
    corrupt = destination / "broken.zip"
    corrupt.write_bytes(b"not a zip")
    target = tmp_path / "restore-target.db"
    initialize_database(target)
    add_employee(target, "KEEP", "既存社員")

    with pytest.raises(BackupValidationError):
        restore_backup(
            corrupt,
            target,
            config_file=operations,
            lock_path=tmp_path / "runtime.lock",
            execution_lock_path=tmp_path / "backup.lock",
            confirmed=True,
        )
    runtime_lock = RuntimeLock(tmp_path / "runtime.lock")
    execution_lock = RuntimeLock(tmp_path / "backup.lock")
    assert runtime_lock.acquire()
    assert execution_lock.acquire()
    runtime_lock.release()
    execution_lock.release()
    with connect(target) as connection:
        assert connection.execute("SELECT employee_id FROM employees").fetchone()[0] == "KEEP"
    assert archive.exists()


def test_restore_and_backup_share_the_same_execution_lock(backup_environment, tmp_path):
    database, _, operations, state, config = backup_environment
    manager = BackupManager(database, config, state_file=state, operations_config_file=operations)
    archive = create_backup(database, config, operations_config_path=operations)
    target = tmp_path / "restore-target.db"
    runtime_path = tmp_path / "runtime.lock"
    held_backup_lock = RuntimeLock(manager.backup_lock_path)
    assert held_backup_lock.acquire()
    try:
        with pytest.raises(BackupBusyError, match="別のバックアップ"):
            restore_backup(
                archive,
                target,
                config_file=operations,
                lock_path=runtime_path,
                execution_lock_path=manager.backup_lock_path,
                confirmed=True,
            )
    finally:
        held_backup_lock.release()

    runtime_lock = RuntimeLock(runtime_path)
    assert runtime_lock.acquire()
    runtime_lock.release()
    assert not target.exists()


def test_backup_lock_prevents_concurrent_execution(backup_environment):
    database, _, operations, state, config = backup_environment
    manager = BackupManager(database, config, state_file=state, operations_config_file=operations)
    held_lock = manager.backup_lock_path
    lock = RuntimeLock(held_lock)
    assert lock.acquire()
    try:
        with pytest.raises(BackupBusyError):
            manager.run_backup()
    finally:
        lock.release()


def test_stopping_manager_waits_for_running_backup_before_releasing_runtime_lock(
    backup_environment,
    monkeypatch,
):
    database, destination, operations, state, config = backup_environment
    current = datetime(2026, 10, 3, 3, 0, tzinfo=JST)
    manager = BackupManager(
        database,
        config,
        state_file=state,
        operations_config_file=operations,
        clock=lambda: current,
    )
    state.write_text(json.dumps({"last_auto_date": "2026-10-02"}), encoding="utf-8")
    started = threading.Event()
    finish = threading.Event()

    def slow_backup(*, automatic=False):
        started.set()
        finish.wait(timeout=5)
        return destination / "completed.zip"

    monkeypatch.setattr(manager, "run_backup", slow_backup)
    runtime_lock = RuntimeLock(state.parent / "application.lock")
    assert runtime_lock.acquire()

    async def verify_shutdown_waits():
        await manager.start()
        assert await asyncio.to_thread(started.wait, 2)
        stop_task = asyncio.create_task(manager.stop())
        await asyncio.sleep(0.05)
        assert not stop_task.done()
        contender = RuntimeLock(runtime_lock.path)
        assert not contender.acquire()
        finish.set()
        await asyncio.wait_for(stop_task, timeout=2)

    try:
        asyncio.run(verify_shutdown_waits())
        assert runtime_lock.file is not None
    finally:
        finish.set()
        runtime_lock.release()

    contender = RuntimeLock(runtime_lock.path)
    assert contender.acquire()
    contender.release()


def test_manual_backup_does_not_change_schedule_and_startup_catchup_runs_once(backup_environment):
    database, destination, operations, state, config = backup_environment
    current = datetime(2026, 10, 3, 3, 0, tzinfo=JST)
    manager = BackupManager(
        database,
        config,
        state_file=state,
        operations_config_file=operations,
        clock=lambda: current,
    )
    state.write_text(json.dumps({"last_auto_date": "2026-08-01"}), encoding="utf-8")
    manager.run_backup()
    assert manager._read_state()["last_auto_date"] == "2026-08-01"
    manual_success = manager._read_state()["last_success_at"]
    restarted_manager = BackupManager(
        database,
        config,
        state_file=state,
        operations_config_file=operations,
        clock=lambda: current,
    )
    assert restarted_manager.status()["last_success_at"] == manual_success

    async def run_until_next_sleep():
        async def stop_after_run(_seconds):
            raise asyncio.CancelledError

        manager.sleeper = stop_after_run
        with pytest.raises(asyncio.CancelledError):
            await manager._scheduler_loop()

    asyncio.run(run_until_next_sleep())
    assert manager._read_state()["last_auto_date"] == current.date().isoformat()
    assert len(list(destination.glob("*.zip"))) == 2

    async def restart_without_duplicate():
        async def stop_before_next_run(_seconds):
            raise asyncio.CancelledError

        manager.sleeper = stop_before_next_run
        with pytest.raises(asyncio.CancelledError):
            await manager._scheduler_loop()

    asyncio.run(restart_without_duplicate())
    assert len(list(destination.glob("*.zip"))) == 2


def test_automatic_failure_schedules_limited_retry(backup_environment, tmp_path):
    database, _, operations, state, _ = backup_environment
    unavailable = BackupConfig(
        backup_directory=tmp_path / "detached" / "backup",
        mount_point=tmp_path / "detached",
        retry_minutes=30,
    )
    current = datetime(2026, 10, 3, 3, 0, tzinfo=JST)
    manager = BackupManager(
        database,
        unavailable,
        state_file=state,
        operations_config_file=operations,
        clock=lambda: current,
    )

    async def run_until_retry_sleep():
        async def stop_after_failure(_seconds):
            raise asyncio.CancelledError

        manager.sleeper = stop_after_failure
        with pytest.raises(asyncio.CancelledError):
            await manager._scheduler_loop()

    asyncio.run(run_until_retry_sleep())
    state_data = manager._read_state()
    assert state_data["retry_at"] == (current + timedelta(minutes=30)).isoformat(timespec="seconds")
    assert state_data.get("last_success_at") is None
    assert state_data["last_error"] == "バックアップ保存先が接続されていません。"


def test_cli_manual_backup_and_restore_use_the_same_environment_settings(backup_environment, tmp_path):
    database, destination, operations, state, config = backup_environment
    save_inspection(database, str(uuid4()))
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(tmp_path),
            "OFFICE_TERMINAL_DB_PATH": str(database),
            "OFFICE_TERMINAL_BACKUP_PATH": str(destination),
            "OFFICE_TERMINAL_BACKUP_ALLOW_UNMOUNTED": "1",
            "OFFICE_TERMINAL_OPERATIONS_CONFIG_PATH": str(operations),
            "OFFICE_TERMINAL_BACKUP_STATE_PATH": str(state),
            "OFFICE_TERMINAL_RUNTIME_LOCK_PATH": str(tmp_path / "runtime.lock"),
        }
    )
    subprocess.run(
        [sys.executable, "-m", "office_terminal.cli", "backup-now"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    backup = next(destination.glob("*.zip"))
    completed = subprocess.run(
        [sys.executable, "-m", "office_terminal.cli", "restore-backup", str(backup)],
        input="RESTORE\n",
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert "バックアップを復元しました" in completed.stdout
    assert json.loads(operations.read_text(encoding="utf-8"))["restore_epoch"]
    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM inspections").fetchone()[0] == 1


def test_status_page_reports_disabled_backup_without_exposing_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_TERMINAL_OPERATIONS_CONFIG_PATH", str(tmp_path / "operations.json"))
    monkeypatch.setenv("OFFICE_TERMINAL_BACKUP_STATE_PATH", str(tmp_path / "backup-state.json"))
    monkeypatch.setenv("OFFICE_TERMINAL_RUNTIME_LOCK_PATH", str(tmp_path / "runtime.lock"))
    database = tmp_path / "db.sqlite3"
    app = create_app(database)
    with TestClient(app) as client:
        response = client.get("/settings")

    assert response.status_code == 200
    assert "無効" in response.text
    assert "保存先が未設定" in response.text
    assert str(tmp_path) not in response.text
    assert "待機中" in response.text


def test_failed_backup_preserves_old_success_and_sets_safe_status(backup_environment, monkeypatch):
    database, destination, operations, state, config = backup_environment
    manager = BackupManager(database, config, state_file=state, operations_config_file=operations)
    existing = create_backup(database, config, operations_config_path=operations)
    old_success = datetime(2026, 9, 1, 2, 0, tzinfo=JST).isoformat()
    state.write_text(json.dumps({"last_success_at": old_success}), encoding="utf-8")

    def fail_snapshot(_source, _target):
        raise BackupError("simulated backup failure")

    monkeypatch.setattr(backup_service, "_sqlite_snapshot", fail_snapshot)

    with pytest.raises(BackupError):
        manager.run_backup(automatic=True)
    status = manager.status(now=datetime(2026, 10, 3, 3, 0, tzinfo=JST))
    assert status["last_success_at"] == old_success
    assert status["last_error"]
    assert list(destination.glob("*.zip")) == [existing]
