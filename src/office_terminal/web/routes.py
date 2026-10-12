import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from office_terminal.services.attendance_service import (
    ATTENDANCE_HISTORY_PAGE_SIZE,
    AttendanceTransitionError,
    change_attendance,
    list_active_attendance,
    list_attendance_history,
    list_current_attendees,
)
from office_terminal.services.access_display_service import resolve_access_display
from office_terminal.services.backup_service import BackupError, load_restore_epoch
from office_terminal.services.weather_service import SOURCE_LABEL
from office_terminal.services.calendar_service import build_month_calendar, visible_month_range
from office_terminal.services.inspection_service import (
    ITEM_DEFINITIONS,
    RESULT_LABELS,
    InspectionValidationError,
    InspectionSubmission,
    validate_and_save_inspection,
)
from office_terminal.storage import repository

logger = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")
TEMPLATES = Jinja2Templates(directory=Path(__file__).parents[1] / "templates")
router = APIRouter()

MENU_ITEMS = [
    {"label": "退室点検", "url": "/inspection", "icon": "✓"},
    {"label": "出社・在席", "url": "/attendees", "icon": "●"},
    {"label": "履歴", "url": "/records", "icon": "▤"},
    {"label": "設定", "url": "/settings", "icon": "⚙"},
]


def database_path(request: Request) -> Path:
    return request.app.state.database_path


def restore_epoch_for_display() -> tuple[str | None, bool]:
    try:
        return load_restore_epoch(), True
    except BackupError:
        logger.exception("Restore epoch is unavailable for display")
        return None, False


def render_inspection(
    request: Request,
    *,
    employees: list[dict],
    form_data: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    attendance_conflicts: list[dict[str, str]] | None = None,
    error_message: str | None = None,
    status_code: int = 200,
):
    restore_epoch, restore_epoch_available = restore_epoch_for_display()
    if not restore_epoch_available and error_message is None:
        error_message = "復元世代を確認できないため保存できません。運用設定を確認してください。"
    return TEMPLATES.TemplateResponse(
        request=request,
        name="inspection.html",
        context={
            "employees": employees,
            "items": ITEM_DEFINITIONS,
            "result_labels": RESULT_LABELS,
            "form_data": form_data or {},
            "errors": errors or {},
            "attendance_conflicts": attendance_conflicts,
            "error_message": error_message,
            "idempotency_key": (form_data or {}).get("idempotency_key", str(uuid4())),
            "draft_revision": (form_data or {}).get("draft_revision", ""),
            "start_date": datetime.now(JST).date().isoformat(),
            "preserve_rendered": bool(errors),
            "restore_epoch": restore_epoch or "",
            "restore_epoch_available": restore_epoch_available,
            "menu_items": MENU_ITEMS,
        },
        status_code=status_code,
    )


def weather_view(request: Request) -> dict:
    service = getattr(request.app.state, "weather_service", None)
    if service is None:
        return {"status": "unavailable", "message": "天気情報を取得できません", "region": "", "source": ""}
    return service.view(datetime.now(JST))


@router.get("/", name="home")
async def home(
    request: Request,
    year: int | None = Query(default=None, ge=2, le=9998),
    month: int | None = Query(default=None, ge=1, le=12),
    saved: str | None = Query(default=None),
    saved_key: str | None = Query(default=None),
    saved_revision: str | None = Query(default=None),
):
    today = datetime.now(JST).date()
    selected_year = today.year if year is None else year
    selected_month = today.month if month is None else month
    previous_year, previous_month = (
        (selected_year - 1, 12) if selected_month == 1 else (selected_year, selected_month - 1)
    )
    next_year, next_month = (
        (selected_year + 1, 1) if selected_month == 12 else (selected_year, selected_month + 1)
    )
    start_date = date(selected_year, selected_month, 1)
    end_date = date(next_year, next_month, 1)
    statuses = repository.get_latest_month_statuses(
        database_path(request),
        start_date,
        end_date,
    )
    calendar_start, calendar_end = visible_month_range(selected_year, selected_month)
    attendance_dates = repository.get_attendance_arrival_dates(
        database_path(request),
        calendar_start,
        calendar_end,
    )
    weeks = build_month_calendar(
        selected_year,
        selected_month,
        today,
        statuses,
        attendance_dates,
    )
    restore_epoch, restore_epoch_available = restore_epoch_for_display()

    return TEMPLATES.TemplateResponse(
        request=request,
        name="home.html",
        context={
            "today": today,
            "now": datetime.now(JST),
            "weeks": weeks,
            "selected_year": selected_year,
            "selected_month": selected_month,
            "previous_year": previous_year,
            "previous_month": previous_month,
            "next_year": next_year,
            "next_month": next_month,
            "menu_items": MENU_ITEMS,
            "attendees": list_current_attendees(database_path(request)),
            "saved": saved if saved in {"complete", "attention"} else None,
            "saved_key": saved_key,
            "saved_revision": saved_revision,
            "weather": weather_view(request),
            "restore_epoch": restore_epoch or "",
            "restore_epoch_available": restore_epoch_available,
        },
    )


@router.get("/menu", name="menu")
async def menu(request: Request):
    access_display = resolve_access_display(
        listen_host=request.app.state.listen_host,
        listen_port=request.app.state.listen_port,
        display_ip=request.app.state.display_ip,
        selected_key=request.query_params.get("candidate"),
    )
    return TEMPLATES.TemplateResponse(
        request=request,
        name="menu.html",
        context={"menu_items": MENU_ITEMS, "access_display": access_display},
    )


@router.get("/records", name="record_history_menu")
async def record_history_menu(request: Request):
    return TEMPLATES.TemplateResponse(
        request=request,
        name="record_history_menu.html",
        context={"menu_items": MENU_ITEMS},
    )


@router.get("/inspection", name="inspection")
async def inspection(request: Request):
    employees = repository.list_active_employees(database_path(request))
    return render_inspection(request, employees=employees)


@router.post("/inspection", name="save_inspection")
async def save_inspection(request: Request):
    form = await request.form()
    form_data = {key: str(value) for key, value in form.multi_items()}
    employees = repository.list_active_employees(database_path(request))
    try:
        current_restore_epoch = load_restore_epoch()
    except BackupError:
        logger.exception("Rejecting inspection because the restore epoch cannot be read")
        return TEMPLATES.TemplateResponse(
            request=request,
            name="restore_conflict.html",
            context={
                "message": "復元世代を確認できないため、点検記録を保存しませんでした。運用設定を確認後、点検画面を再読み込みしてください。",
                "can_start_new": False,
                "menu_items": MENU_ITEMS,
            },
            status_code=503,
        )
    submitted_restore_epoch = form_data.get("restore_epoch")
    expected_restore_epoch = current_restore_epoch or ""
    if submitted_restore_epoch is None or submitted_restore_epoch != expected_restore_epoch:
        logger.warning("Rejecting inspection submitted with a missing or stale restore epoch")
        epoch_message = (
            "画面の復元世代情報を確認できないため、点検記録は保存されませんでした。点検画面を再読み込みしてください。"
            if not submitted_restore_epoch
            else "復元前に開いた点検画面から送信されたため、点検記録は保存されませんでした。送信内容を新しい復元世代へ引き継ぐことはできません。"
        )
        return TEMPLATES.TemplateResponse(
            request=request,
            name="restore_conflict.html",
            context={
                "message": epoch_message,
                "can_start_new": True,
                "current_restore_epoch": expected_restore_epoch,
                "discard_target_available": bool(
                    submitted_restore_epoch
                    and form_data.get("idempotency_key")
                    and form_data.get("draft_revision")
                ),
                "discard_restore_epoch": submitted_restore_epoch or "",
                "discard_idempotency_key": form_data.get("idempotency_key", ""),
                "discard_draft_revision": form_data.get("draft_revision", ""),
                "menu_items": MENU_ITEMS,
            },
            status_code=409,
        )
    submission = InspectionSubmission(
        idempotency_key=form_data.get("idempotency_key", ""),
        employee_id=form_data.get("employee_id", ""),
        results={
            key.removeprefix("result_"): value
            for key, value in form_data.items()
            if key.startswith("result_")
        },
        reasons={
            key.removeprefix("reason_"): value
            for key, value in form_data.items()
            if key.startswith("reason_")
        },
        note=form_data.get("note", ""),
    )
    try:
        _, _, status = validate_and_save_inspection(
            database_path(request),
            submission,
            datetime.now(JST),
        )
    except InspectionValidationError as error:
        if error.attendance_conflicts is not None:
            form_data["result_everyone_left"] = ""
        return render_inspection(
            request,
            employees=employees,
            form_data=form_data,
            errors=error.errors,
            attendance_conflicts=error.attendance_conflicts,
            error_message="入力内容を確認してください。",
            status_code=422,
        )
    except sqlite3.Error:
        logger.exception("Failed to save inspection record")
        return render_inspection(
            request,
            employees=employees,
            form_data=form_data,
            errors={"storage": "点検記録を保存できませんでした。入力内容を保持しています。"},
            error_message="保存に失敗しました。内容を確認して再度お試しください。",
            status_code=503,
        )

    query = urlencode(
        {
            "saved": status,
            "saved_key": submission.idempotency_key,
            "saved_revision": form_data.get("draft_revision", ""),
        }
    )
    return RedirectResponse(url=f"/?{query}", status_code=303)


@router.get("/history", name="history")
async def history(
    request: Request,
    target_date: date | None = Query(default=None),
):
    if target_date is not None:
        return RedirectResponse(url=f"/history/{target_date.isoformat()}", status_code=303)
    return TEMPLATES.TemplateResponse(
        request=request,
        name="history_index.html",
        context={
            "recorded_dates": repository.list_inspection_dates(database_path(request)),
            "today": datetime.now(JST).date(),
        },
    )


@router.get("/attendance/history", name="attendance_history")
@router.get("/pc/attendance/history", name="pc_attendance_history")
async def attendance_history(
    request: Request,
    target_date: date | None = Query(default=None),
    employee_id: str | None = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1, le=1_000_000),
):
    selected_date = target_date or datetime.now(JST).date()
    employee_id = employee_id or None
    records, total_count, employees = list_attendance_history(
        database_path(request),
        target_date=selected_date,
        employee_id=employee_id,
        page=page,
    )
    if employee_id is not None and employee_id not in {
        employee["employee_id"] for employee in employees
    }:
        raise HTTPException(status_code=422, detail="履歴に存在する社員を選択してください。")
    total_pages = max(
        1,
        (total_count + ATTENDANCE_HISTORY_PAGE_SIZE - 1) // ATTENDANCE_HISTORY_PAGE_SIZE,
    )
    if page > total_pages:
        raise HTTPException(status_code=404, detail="指定されたページの履歴はありません。")

    display_records = []
    for record in records:
        changed_at = datetime.fromisoformat(record["changed_at"])
        if changed_at.tzinfo is None:
            changed_at = changed_at.replace(tzinfo=JST)
        else:
            changed_at = changed_at.astimezone(JST)
        display_records.append(
            {
                **record,
                "changed_at_display": changed_at.strftime("%Y-%m-%d %H:%M:%S"),
                "operation_display": (
                    "退勤（点検保存）"
                    if record["operation"] == "退勤"
                    and record["idempotency_key"].startswith("inspection:")
                    else record["operation"]
                ),
            }
        )

    is_pc = request.url.path.startswith("/pc/")
    page_path = "/pc/attendance/history" if is_pc else "/attendance/history"
    return TEMPLATES.TemplateResponse(
        request=request,
        name="attendance_history.html",
        context={
            "target_date": selected_date,
            "employee_id": employee_id or "",
            "employees": employees,
            "records": display_records,
            "total_count": total_count,
            "page": page,
            "total_pages": total_pages,
            "previous_page": page - 1,
            "next_page": page + 1,
            "page_size": ATTENDANCE_HISTORY_PAGE_SIZE,
            "page_path": page_path,
            "is_pc": is_pc,
            "menu_items": MENU_ITEMS,
        },
    )


@router.get("/history/{target_date}", name="daily_history")
async def daily_history(request: Request, target_date: date):
    records = repository.get_daily_inspections(database_path(request), target_date)
    for record in records:
        record["recorded_at_display"] = datetime.fromisoformat(
            record["recorded_at"]
        ).strftime("%H:%M:%S")
        record["item_map"] = {item["item_code"]: item for item in record["items"]}
    return TEMPLATES.TemplateResponse(
        request=request,
        name="daily_history.html",
        context={
            "target_date": target_date,
            "records": records,
            "items": ITEM_DEFINITIONS,
            "result_labels": RESULT_LABELS,
            "menu_items": MENU_ITEMS,
        },
    )


@router.get("/attendees", name="attendees")
@router.get("/pc/attendees", name="pc_attendees")
async def attendees(request: Request):
    restore_epoch, restore_epoch_available = restore_epoch_for_display()
    is_pc = request.url.path.startswith("/pc/")
    return TEMPLATES.TemplateResponse(
        request=request,
        name="attendees.html",
        context={
            "employees": list_active_attendance(database_path(request)),
            "restore_epoch": restore_epoch or "",
            "restore_epoch_available": restore_epoch_available,
            "operation_key": str(uuid4()),
            "message": None,
            "message_kind": "",
            "selected_status": "",
            "is_pc": is_pc,
            "form_action": request.url.path,
            "menu_items": MENU_ITEMS,
        },
    )


@router.post("/attendees", name="change_attendance")
@router.post("/pc/attendees", name="change_pc_attendance")
async def change_attendee_status(request: Request):
    form = await request.form()
    employee_id = str(form.get("employee_id", ""))
    operation = str(form.get("operation", ""))
    submitted_epoch = form.get("restore_epoch")
    operation_key = str(form.get("operation_key", ""))
    try:
        current_epoch = load_restore_epoch() or ""
    except BackupError:
        logger.exception("Rejecting attendance change because the restore epoch cannot be read")
        message = "復元世代を確認できないため、状態を変更しませんでした。画面を再読み込みしてください。"
        status_code = 503
        kind = "error"
    else:
        if submitted_epoch is None or str(submitted_epoch) != current_epoch:
            message = "復元前に開いた画面から送信されたため、状態を変更しませんでした。画面を再読み込みしてください。"
            status_code = 409
            kind = "error"
        else:
            try:
                change = change_attendance(
                    database_path(request),
                    employee_id=employee_id,
                    operation=operation,
                    idempotency_key=operation_key,
                    changed_at=datetime.now(JST),
                )
            except AttendanceTransitionError as error:
                message = str(error)
                status_code = 422
                kind = "error"
            except sqlite3.Error:
                logger.exception("Failed to save attendance status change")
                message = "状態を保存できませんでした。状態と履歴は変更されていません。再読み込みしてご確認ください。"
                status_code = 503
                kind = "error"
            else:
                message = (
                    f"状態を「{change.current_status}」に更新しました。"
                    if not change.duplicate
                    else f"この操作はすでに処理済みです。現在の状態は「{change.current_status}」です。"
                )
                status_code = 200
                kind = "success"

    restore_epoch, restore_epoch_available = restore_epoch_for_display()
    active_employees = list_active_attendance(database_path(request))
    selected_status = next(
        (
            employee["status"]
            for employee in active_employees
            if employee["employee_id"] == employee_id
        ),
        "",
    )
    return TEMPLATES.TemplateResponse(
        request=request,
        name="attendees.html",
        context={
            "employees": active_employees,
            "restore_epoch": restore_epoch or "",
            "restore_epoch_available": restore_epoch_available,
            "operation_key": str(uuid4()),
            "message": message,
            "message_kind": kind,
            "selected_employee_id": employee_id,
            "selected_status": selected_status,
            "is_pc": request.url.path.startswith("/pc/"),
            "form_action": request.url.path,
            "menu_items": MENU_ITEMS,
        },
        status_code=status_code,
    )


@router.get("/api/attendees", name="attendee_status_api")
async def attendee_status_api(request: Request):
    return list_current_attendees(database_path(request))


@router.get("/api/inspection/attendance", name="inspection_attendance_api")
async def inspection_attendance_api(
    request: Request,
    employee_id: str = Query(..., min_length=1),
):
    conflicts = repository.get_active_attendance_conflicts(
        database_path(request),
        employee_id,
    )
    return JSONResponse(
        {"conflicts": conflicts},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/calendar-arrivals", name="calendar_arrival_dates_api")
async def calendar_arrival_dates_api(
    request: Request,
    year: int = Query(..., ge=2, le=9998),
    month: int = Query(..., ge=1, le=12),
):
    start_date, end_date = visible_month_range(year, month)
    dates = repository.get_attendance_arrival_dates(
        database_path(request),
        start_date,
        end_date,
    )
    return JSONResponse(
        {"dates": [day.isoformat() for day in sorted(dates)]},
        headers={"Cache-Control": "no-store"},
    )


def weather_detail(request: Request) -> dict:
    service = getattr(request.app.state, "weather_service", None)
    if service is None:
        return {"status": "unavailable", "message": "天気情報を取得できません", "region": "", "source": SOURCE_LABEL}
    return service.detail_view(datetime.now(JST))


@router.get("/api/weather", name="weather_api")
async def weather_api(request: Request):
    return JSONResponse(weather_view(request), headers={"Cache-Control": "no-store"})


@router.get("/settings", name="settings")
async def settings(request: Request):
    return TEMPLATES.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "title": "設定",
            "backup_status": request.app.state.backup_manager.status(),
            "weather": weather_detail(request),
            "menu_items": MENU_ITEMS,
        },
    )
