import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")
BACKUP_FORMAT_VERSION = 1
BACKUP_PREFIX = "office-terminal-backup-"
BACKUP_PATTERN = re.compile(r"^office-terminal-backup-\d{8}T\d{12}-[0-9a-f]{8}\.zip$")
DATABASE_MEMBER = "database.sqlite3"
CONFIG_MEMBER = "operations.json"
MANIFEST_MEMBER = "manifest.json"
MAX_ARCHIVE_MEMBER_BYTES = 2_000_000_000
MAX_METADATA_MEMBER_BYTES = 1_000_000
DEFAULT_STATE_DIR = Path.home() / ".local/share/office_terminal"


class BackupError(Exception):
    """A safe, user-presentable backup operation failure."""


class BackupBusyError(BackupError):
    pass


class BackupValidationError(BackupError):
    pass


@dataclass(frozen=True)
class BackupConfig:
    backup_directory: Path | None = None
    mount_point: Path | None = None
    allow_unmounted: bool = False
    schedule_time: str = "02:00"
    retry_minutes: int = 30
    retention_days: int = 30

    def validate(self) -> None:
        if self.backup_directory is not None and not isinstance(self.backup_directory, Path):
            raise BackupError("バックアップ保存先の設定が正しくありません。")
        if self.mount_point is not None and not isinstance(self.mount_point, Path):
            raise BackupError("バックアップマウント先の設定が正しくありません。")
        if not isinstance(self.schedule_time, str):
            raise BackupError("バックアップ時刻の設定が正しくありません。")
        if isinstance(self.retry_minutes, bool) or not isinstance(self.retry_minutes, int):
            raise BackupError("バックアップ再試行間隔の設定が正しくありません。")
        if isinstance(self.retention_days, bool) or not isinstance(self.retention_days, int):
            raise BackupError("バックアップ保持期間の設定が正しくありません。")
        try:
            schedule = time.fromisoformat(self.schedule_time)
        except ValueError as error:
            raise BackupError("バックアップ時刻の設定が正しくありません。") from error
        if schedule.second or schedule.microsecond or schedule.tzinfo is not None:
            raise BackupError("バックアップ時刻は HH:MM 形式で指定してください。")
        if self.retry_minutes < 5 or self.retry_minutes > 1440:
            raise BackupError("再試行間隔は5分から1440分の範囲で指定してください。")
        if self.retention_days != 30:
            raise BackupError("保持期間は30日に固定されています。")
        if self.backup_directory is not None and not self.allow_unmounted and self.mount_point is None:
            raise BackupError("保存先の誤使用を防ぐため、マウント先の設定が必要です。")

    @property
    def enabled(self) -> bool:
        return self.backup_directory is not None

    def to_dict(self) -> dict:
        return {
            "backup_directory": str(self.backup_directory) if self.backup_directory else None,
            "mount_point": str(self.mount_point) if self.mount_point else None,
            "allow_unmounted": self.allow_unmounted,
            "schedule_time": self.schedule_time,
            "retry_minutes": self.retry_minutes,
            "retention_days": self.retention_days,
        }

    @classmethod
    def from_dict(cls, data: object) -> "BackupConfig":
        if not isinstance(data, dict):
            raise BackupValidationError("バックアップ設定の形式が正しくありません。")
        try:
            backup_directory = data.get("backup_directory")
            mount_point = data.get("mount_point")
            if backup_directory is not None and not isinstance(backup_directory, str):
                raise TypeError("backup_directory must be a string")
            if mount_point is not None and not isinstance(mount_point, str):
                raise TypeError("mount_point must be a string")
            config = cls(
                backup_directory=Path(backup_directory).expanduser() if backup_directory else None,
                mount_point=Path(mount_point).expanduser() if mount_point else None,
                allow_unmounted=data.get("allow_unmounted", False),
                schedule_time=data.get("schedule_time", "02:00"),
                retry_minutes=data.get("retry_minutes", 30),
                retention_days=data.get("retention_days", 30),
            )
        except (TypeError, ValueError) as error:
            raise BackupValidationError("バックアップ設定の形式が正しくありません。") from error
        if not isinstance(config.allow_unmounted, bool):
            raise BackupValidationError("バックアップ設定の形式が正しくありません。")
        config.validate()
        return config


def settings_path() -> Path:
    configured = os.environ.get("OFFICE_TERMINAL_OPERATIONS_CONFIG_PATH")
    return Path(configured).expanduser() if configured else DEFAULT_STATE_DIR / "operations.json"


def state_path() -> Path:
    configured = os.environ.get("OFFICE_TERMINAL_BACKUP_STATE_PATH")
    return Path(configured).expanduser() if configured else DEFAULT_STATE_DIR / "backup-state.json"


def runtime_lock_path() -> Path:
    configured = os.environ.get("OFFICE_TERMINAL_RUNTIME_LOCK_PATH")
    return Path(configured).expanduser() if configured else DEFAULT_STATE_DIR / "application.lock"


def load_config(path: Path | None = None) -> BackupConfig:
    file_path = path or settings_path()
    try:
        contents = json.loads(file_path.read_text(encoding="utf-8")) if file_path.exists() else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        logger.exception("Could not read operations configuration")
        raise BackupError("運用設定を読み込めません。") from error

    if not isinstance(contents, dict):
        raise BackupError("運用設定の形式が正しくありません。")
    config = BackupConfig.from_dict(contents.get("backup", {}))
    environment = os.environ
    directory = environment.get("OFFICE_TERMINAL_BACKUP_PATH")
    mount = environment.get("OFFICE_TERMINAL_BACKUP_MOUNT")
    allow_unmounted = environment.get("OFFICE_TERMINAL_BACKUP_ALLOW_UNMOUNTED")
    schedule = environment.get("OFFICE_TERMINAL_BACKUP_TIME")
    retry = environment.get("OFFICE_TERMINAL_BACKUP_RETRY_MINUTES")
    try:
        retry_minutes = int(retry) if retry else config.retry_minutes
    except ValueError as error:
        raise BackupError("再試行間隔の設定が正しくありません。") from error
    overrides = {
        "backup_directory": Path(directory).expanduser() if directory else config.backup_directory,
        "mount_point": Path(mount).expanduser() if mount else config.mount_point,
        "allow_unmounted": (
            allow_unmounted.lower() in {"1", "true", "yes"}
            if allow_unmounted is not None
            else config.allow_unmounted
        ),
        "schedule_time": schedule or config.schedule_time,
        "retry_minutes": retry_minutes,
        "retention_days": config.retention_days,
    }
    loaded = BackupConfig(**overrides)
    loaded.validate()
    return loaded


def load_restore_epoch(path: Path | None = None) -> str | None:
    config_file = path or settings_path()
    try:
        contents = config_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        logger.exception("Could not read restore epoch")
        raise BackupError("復元世代を読み込めません。") from error
    try:
        data = json.loads(contents)
    except json.JSONDecodeError as error:
        logger.exception("Could not parse restore epoch")
        raise BackupError("復元世代を読み込めません。") from error
    if not isinstance(data, dict):
        raise BackupError("復元世代の設定形式が正しくありません。")
    epoch = data.get("restore_epoch")
    if epoch is None or epoch == "":
        return None
    if not isinstance(epoch, str):
        raise BackupError("復元世代の設定形式が正しくありません。")
    return epoch


def _read_config_document(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        logger.exception("Could not read operations configuration")
        raise BackupError("運用設定を読み込めません。") from error
    if not isinstance(data, dict):
        raise BackupError("運用設定の形式が正しくありません。")
    return data


def write_config(config: BackupConfig, path: Path | None = None, *, restore_epoch: str | None = None) -> None:
    config.validate()
    config_file = path or settings_path()
    document = _read_config_document(config_file)
    document["backup"] = config.to_dict()
    if restore_epoch is not None:
        document["restore_epoch"] = restore_epoch
    _atomic_write(config_file, json.dumps(document, ensure_ascii=False, indent=2).encode())


def _portable_config_document(path: Path, config: BackupConfig) -> dict:
    document = _read_config_document(path)
    portable = {"backup": config.to_dict()}
    restore_epoch = document.get("restore_epoch")
    if isinstance(restore_epoch, str):
        portable["restore_epoch"] = restore_epoch
    return portable


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as file_handle:
        os.fsync(file_handle.fileno())


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_member_sha256(archive: zipfile.ZipFile, member: str, output: Path | None = None) -> str:
    digest = hashlib.sha256()
    with archive.open(member, "r") as source:
        if output is None:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        else:
            with output.open("wb") as destination:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
                    destination.write(chunk)
    return digest.hexdigest()


def _database_integrity(database_path: Path) -> None:
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise BackupValidationError("SQLiteの整合性検証に失敗しました。")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BackupValidationError("SQLiteの参照整合性検証に失敗しました。")
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise BackupValidationError("SQLiteの整合性検証に失敗しました。") from error


def validate_backup_directory(config: BackupConfig) -> Path:
    config.validate()
    if not config.enabled:
        raise BackupError("バックアップ保存先が設定されていません。")
    target = config.backup_directory
    assert target is not None

    if config.mount_point is not None:
        mount_point = config.mount_point.expanduser()
        if not mount_point.exists() or not mount_point.is_dir() or not os.path.ismount(mount_point):
            raise BackupError("バックアップ保存先が接続されていません。")
        root = mount_point.resolve(strict=True)
        try:
            target.resolve(strict=False).relative_to(root)
        except ValueError as error:
            raise BackupError("バックアップ保存先が指定マウント先の外です。") from error
    elif not config.allow_unmounted:
        raise BackupError("バックアップ保存先が接続されていません。")

    try:
        static_directory = (Path(__file__).parents[1] / "static").resolve()
        try:
            target.resolve(strict=False).relative_to(static_directory)
        except ValueError:
            pass
        else:
            raise BackupError("バックアップ保存先を静的ファイル領域には設定できません。")
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved = target.resolve(strict=True)
        if config.mount_point is not None:
            resolved.relative_to(config.mount_point.resolve(strict=True))
        if not resolved.is_dir():
            raise BackupError("バックアップ保存先を利用できません。")
        if not os.access(resolved, os.W_OK | os.X_OK):
            raise BackupError("バックアップ保存先に書き込めません。")
    except (OSError, ValueError) as error:
        raise BackupError("バックアップ保存先を利用できません。") from error
    return resolved


def backup_target_available(config: BackupConfig) -> tuple[bool, str | None]:
    if not config.enabled:
        return False, "保存先が未設定です。"
    try:
        config.validate()
        target = config.backup_directory
        assert target is not None
        static_directory = (Path(__file__).parents[1] / "static").resolve()
        try:
            target.resolve(strict=False).relative_to(static_directory)
        except ValueError:
            pass
        else:
            return False, "静的ファイル領域は保存先にできません。"
        if config.mount_point is not None:
            mount_point = config.mount_point.expanduser()
            if not mount_point.exists() or not mount_point.is_dir() or not os.path.ismount(mount_point):
                return False, "保存先が接続されていません。"
            root = mount_point.resolve(strict=True)
            target.resolve(strict=False).relative_to(root)
            if target.exists() and not target.is_dir():
                return False, "保存先を利用できません。"
            probe = target if target.exists() else target.parent
            while not probe.exists() and probe != root:
                probe = probe.parent
            probe.resolve(strict=True).relative_to(root)
            if not os.access(probe, os.W_OK | os.X_OK):
                return False, "保存先に書き込めません。"
        elif config.allow_unmounted:
            probe = target
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            if not os.access(probe, os.W_OK | os.X_OK):
                return False, "保存先に書き込めません。"
        return True, None
    except (BackupError, OSError, ValueError):
        return False, "保存先を利用できません。"


def _sqlite_snapshot(database_path: Path, output_path: Path) -> None:
    try:
        source = sqlite3.connect(database_path, timeout=30)
        destination = sqlite3.connect(output_path)
        try:
            source.backup(destination, pages=256, sleep=0.05)
        finally:
            destination.close()
            source.close()
        _database_integrity(output_path)
    except (sqlite3.Error, OSError) as error:
        raise BackupError("データベースのバックアップに失敗しました。") from error


def _app_version() -> str:
    try:
        from importlib.metadata import version

        return version("office-terminal")
    except Exception:
        return "0.1.0"


def create_backup(
    database_path: Path,
    config: BackupConfig,
    *,
    now: datetime | None = None,
    operations_config_path: Path | None = None,
) -> Path:
    if not database_path.is_file():
        raise BackupError("データベースファイルがありません。")
    target_directory = validate_backup_directory(config)
    saved_at = (now or datetime.now(JST)).astimezone(JST)
    file_name = f"{BACKUP_PREFIX}{saved_at:%Y%m%dT%H%M%S%f}-{uuid4().hex[:8]}.zip"
    final_path = target_directory / file_name
    operations_file = operations_config_path or settings_path()
    configuration_bytes = json.dumps(
        _portable_config_document(operations_file, config),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ).encode()

    with tempfile.TemporaryDirectory(prefix=".office-terminal-backup-", dir=target_directory) as working_dir:
        working = Path(working_dir)
        snapshot = working / DATABASE_MEMBER
        _sqlite_snapshot(database_path, snapshot)
        manifest = {
            "format_version": BACKUP_FORMAT_VERSION,
            "saved_at": saved_at.isoformat(timespec="seconds"),
            "application_version": _app_version(),
            "files": {
                DATABASE_MEMBER: _file_sha256(snapshot),
                CONFIG_MEMBER: hashlib.sha256(configuration_bytes).hexdigest(),
            },
        }
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode()
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".office-terminal-backup-", suffix=".tmp", dir=target_directory
        )
        os.close(descriptor)
        temporary_archive = Path(temporary_name)
        published = False
        try:
            os.chmod(temporary_archive, 0o600)
            with zipfile.ZipFile(temporary_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot, DATABASE_MEMBER)
                archive.writestr(CONFIG_MEMBER, configuration_bytes)
                archive.writestr(MANIFEST_MEMBER, manifest_bytes)
            _verify_archive(temporary_archive)
            _database_integrity(snapshot)
            with temporary_archive.open("rb") as archive_file:
                os.fsync(archive_file.fileno())
            os.replace(temporary_archive, final_path)
            published = True
            _fsync_directory(target_directory)
        except Exception:
            try:
                temporary_archive.unlink()
            except FileNotFoundError:
                pass
            if published:
                try:
                    final_path.unlink()
                except OSError:
                    logger.exception("Could not remove backup after publication failure")
            raise

    try:
        prune_backups(target_directory, now=saved_at, retention_days=config.retention_days)
    except OSError:
        logger.exception("Backup succeeded but retention cleanup failed")
    return final_path


def _verify_archive(archive_path: Path, *, extract_directory: Path | None = None) -> tuple[dict, BackupConfig]:
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            entries = archive.infolist()
            names = {entry.filename for entry in entries}
            expected = {DATABASE_MEMBER, CONFIG_MEMBER, MANIFEST_MEMBER}
            if (
                names != expected
                or len(entries) != len(expected)
                or any(entry.file_size > MAX_ARCHIVE_MEMBER_BYTES for entry in entries)
                or any(
                    entry.file_size > MAX_METADATA_MEMBER_BYTES
                    for entry in entries
                    if entry.filename != DATABASE_MEMBER
                )
            ):
                raise BackupValidationError("バックアップの内容を検証できません。")
            manifest = json.loads(archive.read(MANIFEST_MEMBER))
            if not isinstance(manifest, dict) or manifest.get("format_version") != BACKUP_FORMAT_VERSION:
                raise BackupValidationError("対応していないバックアップ形式です。")
            file_hashes = manifest.get("files")
            if not isinstance(file_hashes, dict) or set(file_hashes) != {DATABASE_MEMBER, CONFIG_MEMBER}:
                raise BackupValidationError("バックアップのチェックサム情報が正しくありません。")
            database_output = extract_directory / DATABASE_MEMBER if extract_directory is not None else None
            config_output = extract_directory / CONFIG_MEMBER if extract_directory is not None else None
            if _archive_member_sha256(archive, DATABASE_MEMBER, database_output) != file_hashes[DATABASE_MEMBER]:
                raise BackupValidationError("バックアップのチェックサム検証に失敗しました。")
            if _archive_member_sha256(archive, CONFIG_MEMBER, config_output) != file_hashes[CONFIG_MEMBER]:
                raise BackupValidationError("バックアップのチェックサム検証に失敗しました。")
            config_bytes = config_output.read_bytes() if config_output is not None else archive.read(CONFIG_MEMBER)
            config_document = json.loads(config_bytes)
            backup_config = BackupConfig.from_dict(config_document.get("backup") if isinstance(config_document, dict) else None)
            if not isinstance(manifest.get("saved_at"), str) or not isinstance(
                manifest.get("application_version"), str
            ):
                raise BackupValidationError("バックアップの情報が正しくありません。")
    except BackupValidationError:
        raise
    except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise BackupValidationError("バックアップが破損しているか、形式が正しくありません。") from error

    if extract_directory is not None:
        _database_integrity(extract_directory / DATABASE_MEMBER)
    return manifest, backup_config


def verify_backup(archive_path: Path) -> tuple[dict, BackupConfig]:
    with tempfile.TemporaryDirectory(prefix=".office-terminal-verify-") as directory:
        return _verify_archive(archive_path, extract_directory=Path(directory))


def prune_backups(directory: Path, *, now: datetime, retention_days: int = 30) -> list[Path]:
    cutoff = now - timedelta(days=retention_days)
    candidates: list[tuple[Path, datetime]] = []
    for candidate in directory.iterdir():
        if not candidate.is_file() or not BACKUP_PATTERN.fullmatch(candidate.name):
            continue
        try:
            manifest, _ = verify_backup(candidate)
            saved_at = datetime.fromisoformat(manifest["saved_at"]).astimezone(JST)
        except (BackupError, OSError, KeyError, ValueError):
            logger.warning("Skipping invalid managed backup during retention cleanup: %s", candidate.name)
            continue
        candidates.append((candidate, saved_at))

    candidates.sort(key=lambda item: item[1], reverse=True)
    if not candidates:
        return []
    keep_latest = candidates[0][0]
    deleted: list[Path] = []
    for candidate, saved_at in candidates:
        if candidate != keep_latest and saved_at < cutoff:
            candidate.unlink()
            deleted.append(candidate)
    return deleted


class RuntimeLock:
    def __init__(self, path: Path):
        self.path = path
        self.file = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = self.path.open("a+")
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            self.file = None
            return False
        except Exception:
            self.file.close()
            self.file = None
            raise
        return True

    def release(self) -> None:
        if self.file is not None:
            file_handle = self.file
            self.file = None
            try:
                fcntl.flock(file_handle.fileno(), fcntl.LOCK_UN)
            finally:
                file_handle.close()


def acquire_backup_lock(lock_path: Path) -> RuntimeLock:
    lock = RuntimeLock(lock_path)
    try:
        if not lock.acquire():
            raise BackupBusyError("別のバックアップ処理が実行中です。")
    except OSError as error:
        raise BackupError("バックアップ実行状態を確認できません。") from error
    return lock


class BackupManager:
    def __init__(
        self,
        database_path: Path,
        config: BackupConfig,
        *,
        state_file: Path | None = None,
        operations_config_file: Path | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], object] = asyncio.sleep,
    ):
        self.database_path = database_path
        self.config = config
        self.state_file = state_file or state_path()
        self.operations_config_file = operations_config_file or settings_path()
        self.clock = clock or (lambda: datetime.now(JST))
        self.sleeper = sleeper
        self.backup_lock_path = self.state_file.with_name("backup.lock")
        self.thread_lock = threading.Lock()
        self.stop_event = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.backup_task: asyncio.Task | None = None

    def _read_state(self) -> dict:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8")) if self.state_file.exists() else {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            logger.exception("Could not read backup state")
            return {}
        return data if isinstance(data, dict) else {}

    def _write_state(self, state: dict) -> None:
        _atomic_write(self.state_file, json.dumps(state, ensure_ascii=False, indent=2).encode())

    def status(self, *, now: datetime | None = None) -> dict:
        current = (now or self.clock()).astimezone(JST)
        enabled, availability_message = backup_target_available(self.config)
        state = self._read_state()
        schedule = time.fromisoformat(self.config.schedule_time)
        next_run = datetime.combine(current.date(), schedule, JST)
        if next_run <= current or state.get("last_auto_date") == current.date().isoformat():
            next_run += timedelta(days=1)
        retry_at = state.get("retry_at")
        if retry_at:
            try:
                retry = datetime.fromisoformat(retry_at).astimezone(JST)
                if retry < next_run:
                    next_run = retry
            except ValueError:
                logger.warning("Ignoring malformed backup retry time")
        return {
            "enabled": enabled,
            "message": availability_message,
            "running": bool(state.get("running")),
            "last_attempt_at": state.get("last_attempt_at"),
            "last_success_at": state.get("last_success_at"),
            "next_run_at": next_run.isoformat(timespec="minutes") if self.config.enabled else None,
            "last_error": state.get("last_error"),
        }

    def run_backup(self, *, automatic: bool = False) -> Path:
        with self.thread_lock:
            lock = acquire_backup_lock(self.backup_lock_path)
            try:
                current = self.clock().astimezone(JST)
                state = self._read_state()
                state.update(
                    {
                        "running": True,
                        "last_attempt_at": current.isoformat(timespec="seconds"),
                        "last_error": None,
                    }
                )
                try:
                    self._write_state(state)
                except OSError as error:
                    raise BackupError("バックアップ実行状態を保存できません。") from error
                try:
                    output = create_backup(
                        self.database_path,
                        self.config,
                        now=current,
                        operations_config_path=self.operations_config_file,
                    )
                except Exception as error:
                    state = self._read_state()
                    state["running"] = False
                    state["last_error"] = (
                        str(error) if isinstance(error, BackupError) else "バックアップに失敗しました。"
                    )
                    if automatic:
                        state["retry_at"] = (current + timedelta(minutes=self.config.retry_minutes)).isoformat(
                            timespec="seconds"
                        )
                    self._write_state(state)
                    logger.exception("Backup failed")
                    raise

                state = self._read_state()
                state.update(
                    {
                        "running": False,
                        "last_success_at": current.isoformat(timespec="seconds"),
                        "last_error": None,
                        "retry_at": None,
                    }
                )
                if automatic:
                    state["last_auto_date"] = current.date().isoformat()
                self._write_state(state)
                return output
            finally:
                lock.release()

    async def start(self) -> None:
        self.stop_event = asyncio.Event()
        state = self._read_state()
        if state.get("running"):
            state["running"] = False
            try:
                self._write_state(state)
            except OSError:
                logger.exception("Could not reset interrupted backup status")
        self.task = asyncio.create_task(self._scheduler_loop())

    async def stop(self) -> None:
        self.stop_event.set()
        scheduler_error = None
        if self.task is not None:
            task = self.task
            self.task = None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                if asyncio.current_task() and asyncio.current_task().cancelling():
                    scheduler_error = asyncio.CancelledError()
            except Exception as error:
                scheduler_error = error
        if self.backup_task is not None:
            backup_task = self.backup_task
            try:
                await asyncio.shield(backup_task)
            except asyncio.CancelledError:
                if asyncio.current_task() and asyncio.current_task().cancelling():
                    await asyncio.shield(backup_task)
                    scheduler_error = asyncio.CancelledError()
                else:
                    raise
            finally:
                if backup_task.done():
                    self.backup_task = None
        if scheduler_error is not None:
            raise scheduler_error

    async def _scheduler_loop(self) -> None:
        while not self.stop_event.is_set():
            current = self.clock().astimezone(JST)
            state = self._read_state()
            try:
                schedule = time.fromisoformat(self.config.schedule_time)
            except ValueError:
                logger.exception("Invalid automatic backup schedule")
                await self.sleeper(self.config.retry_minutes * 60)
                continue
            today = current.date().isoformat()
            run_at = datetime.combine(current.date(), schedule, JST)
            due = current >= run_at and state.get("last_auto_date") != today
            retry_at = state.get("retry_at")
            if retry_at:
                try:
                    retry_due = current >= datetime.fromisoformat(retry_at).astimezone(JST)
                except ValueError:
                    retry_due = True
                if not retry_due:
                    await self.sleeper(min(60, self.config.retry_minutes * 60))
                    continue
            if not self.config.enabled:
                try:
                    await self.sleeper(60)
                except asyncio.CancelledError:
                    raise
                continue
            if due or retry_at:
                if self.stop_event.is_set():
                    return
                self.backup_task = asyncio.create_task(
                    asyncio.to_thread(self.run_backup, automatic=True)
                )
                try:
                    await asyncio.shield(self.backup_task)
                except BackupError:
                    if self.stop_event.is_set():
                        return
                    try:
                        await self.sleeper(self.config.retry_minutes * 60)
                    except asyncio.CancelledError:
                        raise
                except Exception:
                    if self.stop_event.is_set():
                        return
                    logger.exception("Unexpected automatic backup failure")
                    try:
                        await self.sleeper(self.config.retry_minutes * 60)
                    except asyncio.CancelledError:
                        raise
                finally:
                    if self.backup_task is not None and self.backup_task.done():
                        self.backup_task = None
                continue
            upcoming = run_at if current < run_at else run_at + timedelta(days=1)
            try:
                await self.sleeper(max(1, min(60, (upcoming - current).total_seconds())))
            except asyncio.CancelledError:
                raise


def _check_application_stopped(lock_path: Path) -> RuntimeLock:
    lock = RuntimeLock(lock_path)
    try:
        if not lock.acquire():
            raise BackupError("アプリを停止してから復元してください。")
    except OSError as error:
        raise BackupError("アプリの稼働状態を確認できません。") from error
    return lock


def restore_backup(
    archive_path: Path,
    database_path: Path,
    *,
    config_file: Path | None = None,
    lock_path: Path | None = None,
    execution_lock_path: Path | None = None,
    confirmed: bool = False,
    confirmer: Callable[[str], str] | None = None,
) -> dict:
    if not archive_path.is_file():
        raise BackupError("指定されたバックアップファイルがありません。")
    config_destination = config_file or settings_path()
    runtime_lock = _check_application_stopped(lock_path or runtime_lock_path())
    backup_lock = None
    try:
        backup_lock = acquire_backup_lock(
            execution_lock_path or state_path().with_name("backup.lock")
        )
        manifest, backup_config = verify_backup(archive_path)
        message = (
            f"復元元: {archive_path}\n"
            f"上書き対象DB: {database_path}\n"
            f"上書き対象設定: {config_destination}\n"
            "現在のDBと設定を退避して置き換えます。続行するには RESTORE と入力してください: "
        )
        if not confirmed and (confirmer or input)(message).strip() != "RESTORE":
            raise BackupError("復元をキャンセルしました。")

        database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        config_destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        current_config_document = _read_config_document(config_destination)
        previous_database = database_path.with_name(
            f".{database_path.name}.pre-restore-{datetime.now(JST):%Y%m%dT%H%M%S%f}-{uuid4().hex[:8]}"
        )
        previous_config = config_destination.with_name(
            f".{config_destination.name}.pre-restore-{datetime.now(JST):%Y%m%dT%H%M%S%f}-{uuid4().hex[:8]}"
        )
        had_database = database_path.exists()
        had_config = config_destination.exists()

        with tempfile.TemporaryDirectory(prefix=".office-terminal-restore-", dir=database_path.parent) as directory:
            staging = Path(directory)
            _verify_archive(archive_path, extract_directory=staging)
            staged_database = staging / DATABASE_MEMBER
            staged_config = json.loads((staging / CONFIG_MEMBER).read_bytes())
            staged_config["restore_epoch"] = uuid4().hex
            staged_config_bytes = json.dumps(staged_config, ensure_ascii=False, indent=2).encode()

            if had_database:
                snapshot = staging / "previous.sqlite3"
                _sqlite_snapshot(database_path, snapshot)
                shutil.copy2(snapshot, previous_database)
                os.chmod(previous_database, 0o600)
                _fsync_file(previous_database)
            if had_config:
                shutil.copy2(config_destination, previous_config)
                os.chmod(previous_config, 0o600)
                _fsync_file(previous_config)
            else:
                _atomic_write(previous_config, json.dumps(current_config_document).encode())

            staged_target = database_path.with_name(f".{database_path.name}.restore-{uuid4().hex}.tmp")
            shutil.copyfile(staged_database, staged_target)
            os.chmod(staged_target, 0o600)
            _database_integrity(staged_target)
            _fsync_file(staged_target)
            staged_settings = config_destination.with_name(f".{config_destination.name}.restore-{uuid4().hex}.tmp")
            _atomic_write(staged_settings, staged_config_bytes)
            try:
                os.replace(staged_target, database_path)
                os.replace(staged_settings, config_destination)
                _fsync_directory(database_path.parent)
                if config_destination.parent != database_path.parent:
                    _fsync_directory(config_destination.parent)
            except Exception:
                logger.exception("Restore replacement failed; rolling back")
                if previous_database.exists():
                    os.replace(previous_database, database_path)
                elif not had_database:
                    database_path.unlink(missing_ok=True)
                if previous_config.exists():
                    if had_config:
                        os.replace(previous_config, config_destination)
                    else:
                        previous_config.unlink()
                        config_destination.unlink(missing_ok=True)
                staged_target.unlink(missing_ok=True)
                staged_settings.unlink(missing_ok=True)
                raise BackupError("復元に失敗しました。退避データからの復旧を確認してください。")

        return {
            "manifest": manifest,
            "backup_config": backup_config,
            "previous_database": previous_database if previous_database.exists() else None,
            "previous_config": previous_config if previous_config.exists() else None,
        }
    finally:
        try:
            if backup_lock is not None:
                backup_lock.release()
        finally:
            runtime_lock.release()
