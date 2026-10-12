import calendar
from datetime import date, timedelta
from typing import Collection, Mapping


def visible_month_range(year: int, month: int) -> tuple[date, date]:
    """Return the full calendar grid range as [start, end), including adjacent months."""
    if not 1 <= month <= 12:
        raise ValueError("month must be between 1 and 12")
    weeks = calendar.Calendar(firstweekday=calendar.MONDAY).monthdatescalendar(year, month)
    return weeks[0][0], weeks[-1][-1] + timedelta(days=1)


def build_month_calendar(
    year: int,
    month: int,
    today: date,
    statuses: Mapping[date, str] | None = None,
    attendance_dates: Collection[date] | None = None,
) -> list[list[dict]]:
    """月曜始まりの月間カレンダーを点検状態・出社記録付きで作る。"""
    if not 1 <= month <= 12:
        raise ValueError("month must be between 1 and 12")

    month_calendar = calendar.Calendar(firstweekday=calendar.MONDAY)
    statuses = statuses or {}
    attendance_dates = attendance_dates or set()
    return [
        [
            {
                "date": day,
                "in_month": day.month == month,
                "is_today": day == today,
                "status": statuses.get(day) if day.month == month else None,
                "has_attendance": day in attendance_dates,
            }
            for day in week
        ]
        for week in month_calendar.monthdatescalendar(year, month)
    ]
