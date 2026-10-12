from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from office_terminal.storage import repository

ITEM_DEFINITIONS = [
    {
        "code": "air_conditioner",
        "label": "エアコン",
        "description": "電源を切った",
        "reason_options": ["使用中", "停止できない", "確認できない"],
    },
    {
        "code": "windows",
        "label": "窓の施錠",
        "description": "すべての窓を施錠した",
        "reason_options": ["施錠できない", "開放が必要", "確認できない"],
    },
    {
        "code": "lights",
        "label": "電灯",
        "description": "消灯した",
        "reason_options": ["使用中", "消灯できない", "確認できない"],
    },
    {
        "code": "equipment",
        "label": "電気機器",
        "description": "停止対象の機器の電源を切った",
        "note": "常時稼働するPCなどは、停止対象から除外してください。",
        "reason_options": ["使用中", "停止できない", "確認できない"],
    },
    {
        "code": "everyone_left",
        "label": "全員退室",
        "description": "点検者以外、全員が退室した",
        "reason_options": ["在室者あり", "退室予定者を待機中", "確認できない"],
    },
]

RESULT_LABELS = {
    "confirmed": "確認済み",
    "abnormal": "異常あり",
    "not_done": "未実施",
}


@dataclass
class InspectionSubmission:
    idempotency_key: str
    employee_id: str
    results: dict[str, str]
    reasons: dict[str, str]
    note: str


class InspectionValidationError(Exception):
    def __init__(
        self,
        errors: dict[str, str],
        attendance_conflicts: list[dict[str, str]] | None = None,
    ):
        self.errors = errors
        self.attendance_conflicts = attendance_conflicts
        super().__init__("点検入力に不備があります")


def validate_and_save_inspection(
    database_path: Path,
    submission: InspectionSubmission,
    recorded_at: datetime,
) -> tuple[int, bool, str]:
    errors: dict[str, str] = {}
    try:
        UUID(submission.idempotency_key)
    except (ValueError, AttributeError):
        errors["idempotency_key"] = "画面を再読み込みしてから、もう一度お試しください。"

    employee_id = submission.employee_id.strip()
    employee = (
        repository.get_active_employee(database_path, employee_id)
        if employee_id
        else None
    )
    if employee is None:
        errors["employee_id"] = "有効な点検者を選択してください。"

    if submission.note:
        errors["note"] = "全体備考は利用できません。"

    expected_codes = {item["code"] for item in ITEM_DEFINITIONS}
    if set(submission.results) != expected_codes:
        errors["results"] = "5項目すべてを選択してください。"

    has_attention = False
    validated_results: list[dict[str, str]] = []
    for item in ITEM_DEFINITIONS:
        code = item["code"]
        result = submission.results.get(code, "")
        reason = submission.reasons.get(code, "")
        if reason not in ("", *item["reason_options"]):
            errors[f"reason_{code}"] = "項目に対応する理由を選択してください。"
        if result not in RESULT_LABELS:
            errors[f"result_{code}"] = "結果を選択してください。"
            continue
        if result != "confirmed":
            has_attention = True
            if not reason:
                errors[f"reason_{code}"] = "異常あり・未実施の場合は理由を選択してください。"
        validated_results.append(
            {
                "code": code,
                "result": result,
                "reason": reason if result != "confirmed" else "",
            }
        )

    if errors:
        raise InspectionValidationError(errors)

    assert employee is not None
    try:
        inspection_id, duplicate, saved_status = repository.save_inspection(
            database_path,
            idempotency_key=submission.idempotency_key,
            employee_id=employee["employee_id"],
            recorded_at=recorded_at,
            overall_status="attention" if has_attention else "complete",
            results=validated_results,
            note="",
        )
    except repository.InspectionAttendanceConflict as error:
        raise InspectionValidationError(
            {
                "result_everyone_left":
                    "点検者以外に在室・離席中の社員が登録されています。"
                    "「全員退室」は「異常あり」または「未実施」を選んでください。"
            },
            attendance_conflicts=error.employees,
        ) from error
    except repository.InactiveInspectionEmployee as error:
        raise InspectionValidationError(
            {"employee_id": "有効な点検者を選択してください。"}
        ) from error
    return inspection_id, duplicate, saved_status
