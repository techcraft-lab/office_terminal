import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from office_terminal.services.backup_service import (
    BackupConfig,
    BackupManager,
    RuntimeLock,
    load_config,
    runtime_lock_path,
)
from office_terminal.services.access_display_service import (
    InterfaceDiscoveryError,
    discover_network_candidates,
)
from office_terminal.services.weather_service import WeatherService
from office_terminal.storage.database import get_database_path, initialize_database
from office_terminal.web.routes import router

BASE_DIR = Path(__file__).parent
logger = logging.getLogger(__name__)


def create_app(
    database_path: Path | None = None,
    *,
    listen_host: str | None = None,
    listen_port: int | None = None,
    display_ip: str | None = None,
) -> FastAPI:
    resolved_database_path = database_path or get_database_path()
    resolved_listen_host = listen_host or os.environ.get("OFFICE_TERMINAL_LISTEN_HOST")
    configured_port = os.environ.get("OFFICE_TERMINAL_LISTEN_PORT")
    if listen_port is None and configured_port:
        try:
            listen_port = int(configured_port)
        except ValueError:
            logger.error("OFFICE_TERMINAL_LISTEN_PORT must be a valid integer")
    resolved_display_ip = (
        display_ip
        if display_ip is not None
        else os.environ.get("OFFICE_TERMINAL_DISPLAY_IP")
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        try:
            discover_network_candidates()
        except InterfaceDiscoveryError:
            logger.exception("Could not enumerate local IPv4 interfaces at startup")
        runtime_lock = RuntimeLock(runtime_lock_path())
        try:
            runtime_acquired = runtime_lock.acquire()
        except OSError as error:
            logger.exception("Could not create or acquire the application runtime lock; startup is aborted.")
            raise RuntimeError("アプリの稼働ロックを取得できないため起動できません。") from error
        if not runtime_acquired:
            logger.error("Another application process holds the runtime lock; startup is aborted.")
            raise RuntimeError("別のアプリが稼働中のため起動できません。")
        manager = None
        weather = None
        try:
            initialize_database(resolved_database_path)
            try:
                backup_config = load_config()
            except Exception:
                logger.exception("Backup configuration is invalid; backups are disabled.")
                backup_config = BackupConfig()
            manager = BackupManager(resolved_database_path, backup_config)
            application.state.backup_manager = manager
            await manager.start()
            weather = WeatherService.from_environment()
            application.state.weather_service = weather
            try:
                await weather.start()
            except Exception:
                logger.exception("Weather updater could not start; weather is unavailable.")
            yield
        finally:
            try:
                if weather is not None:
                    try:
                        await weather.stop()
                    except Exception:
                        logger.exception("Weather updater did not stop cleanly.")
                if manager is not None:
                    await manager.stop()
            finally:
                runtime_lock.release()

    application = FastAPI(title="事務所 退室点検端末", lifespan=lifespan)
    application.state.database_path = resolved_database_path
    application.state.listen_host = resolved_listen_host
    application.state.listen_port = listen_port
    application.state.display_ip = resolved_display_ip
    application.mount(
        "/static",
        StaticFiles(directory=BASE_DIR / "static"),
        name="static",
    )
    application.include_router(router)
    return application


app = create_app()
