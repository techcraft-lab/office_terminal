import argparse
import sys
from pathlib import Path

from office_terminal.services.backup_service import (
    BackupError,
    BackupManager,
    load_config,
    restore_backup,
    runtime_lock_path,
    settings_path,
    state_path,
)
from office_terminal.services.employee_import_service import (
    EmployeeImportError,
    import_employee_csv,
)
from office_terminal.storage.database import get_database_path, initialize_database
from office_terminal.storage.repository import add_employee

DEVELOPMENT_EMPLOYEES = [
    ("DEV-001", "開発用 山田"),
    ("DEV-002", "開発用 佐藤"),
    ("DEV-003", "開発用 鈴木"),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="office_terminal の管理コマンド")
    parser.add_argument(
        "command",
        choices=[
            "add-development-employees",
            "backup-now",
            "restore-backup",
            "import-employees",
            "serve",
        ],
    )
    parser.add_argument("file_path", nargs="?")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--display-ip")
    args = parser.parse_args()

    if args.command == "serve":
        import uvicorn

        from office_terminal.main import create_app

        application = create_app(
            listen_host=args.host,
            listen_port=args.port,
            display_ip=args.display_ip,
        )
        uvicorn.run(application, host=args.host, port=args.port)
        return

    database_path = get_database_path()
    try:
        if args.command == "add-development-employees":
            initialize_database(database_path)
            for employee_id, display_name in DEVELOPMENT_EMPLOYEES:
                add_employee(database_path, employee_id, display_name)
            print(f"開発用社員を登録しました: {database_path}")
            return

        if args.command == "backup-now":
            config = load_config()
            manager = BackupManager(
                database_path,
                config,
                state_file=state_path(),
                operations_config_file=settings_path(),
            )
            backup_path = manager.run_backup()
            print(f"バックアップを作成しました: {backup_path}")
            return

        if args.command == "import-employees":
            if not args.file_path:
                parser.error("import-employees にはCSVファイルを指定してください。")
            try:
                added, updated, unchanged = import_employee_csv(
                    Path(args.file_path).expanduser(),
                    database_path,
                )
            except EmployeeImportError as error:
                print(f"社員CSVの取込に失敗しました: {error}", file=sys.stderr)
                raise SystemExit(1) from error
            print(
                "社員CSVを取り込みました: "
                f"追加 {added}件、更新 {updated}件、変更なし {unchanged}件"
            )
            return

        if not args.file_path:
            parser.error("restore-backup にはバックアップファイルを指定してください。")
        restore_info = restore_backup(
            Path(args.file_path).expanduser(),
            database_path,
            config_file=settings_path(),
            lock_path=runtime_lock_path(),
        )
        print(f"バックアップを復元しました: {restore_info['manifest']['saved_at']}")
        print("アプリを再起動してください。復元前のブラウザ下書きは確認後に破棄されます。")
    except BackupError as error:
        print(f"バックアップ処理に失敗しました: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
