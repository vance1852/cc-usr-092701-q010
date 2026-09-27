"""诊所营业日历：响应时限只在营业时段内累计，升级宽限只在非营业时段累计。

日历以诊所本地墙钟时间配置，内部统一换算为 UTC 计算，因此夏令时跳变也能得到
正确的秒数。模块只处理时间换算，不读取数据库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# 默认营业班次：周一至周六 09:00-18:00（本地时间），周日休息。
DEFAULT_WEEKLY: dict[str, tuple[int, int] | None] = {
    day: (9 * 60, 18 * 60) for day in WEEKDAYS
} | {"sunday": None}

_MAX_SCAN_DAYS = 3660


@dataclass(frozen=True)
class DayOpening:
    open_minute: int
    close_minute: int


@dataclass(frozen=True)
class Calendar:
    """weekly: 星期 -> (开门分钟, 关门分钟) 或 None（全天休息）。

    exceptions: 本地日期 -> ('open', DayOpening) 或 ('closed', 原因)。
    """

    weekly: dict[str, tuple[int, int] | None]
    exceptions: dict[date, tuple[str, object]] = field(default_factory=dict)

    def opening_for(self, day: date) -> DayOpening | None:
        exception = self.exceptions.get(day)
        if exception is not None:
            if exception[0] == "closed":
                return None
            return exception[1]  # type: ignore[return-value]
        window = self.weekly.get(WEEKDAYS[day.weekday()])
        if window is None:
            return None
        return DayOpening(window[0], window[1])

    def _segments(self, day: date, zone: ZoneInfo) -> list[tuple[datetime, datetime, bool]]:
        """返回某一本地日期内按时间排列的 (UTC 起, UTC 止, 是否营业) 分段。"""
        midnight = datetime.combine(day, time.min)
        day_start = midnight.replace(tzinfo=zone).astimezone(UTC)
        day_end = datetime.combine(day + timedelta(days=1), time.min).replace(tzinfo=zone).astimezone(UTC)
        opening = self.opening_for(day)
        if opening is None or day_end <= day_start:
            return [(day_start, day_end, False)]
        open_at = self._localize(day, opening.open_minute, zone)
        close_at = self._localize(day, opening.close_minute, zone)
        if close_at <= open_at:
            return [(day_start, day_end, False)]
        segments: list[tuple[datetime, datetime, bool]] = []
        if open_at > day_start:
            segments.append((day_start, open_at, False))
        segments.append((open_at, close_at, True))
        if day_end > close_at:
            segments.append((close_at, day_end, False))
        return segments

    @staticmethod
    def _localize(day: date, minute: int, zone: ZoneInfo) -> datetime:
        naive = datetime.combine(day, time(minute // 60, minute % 60))
        # 若该墙钟时间因春令时不存在，fold=1 取跳变后一刻。
        localized = naive.replace(tzinfo=zone)
        if localized.astimezone(zone).replace(tzinfo=None) != naive:
            localized = naive.replace(tzinfo=zone, fold=1)
        return localized.astimezone(UTC)

    def is_open_at(self, moment: datetime, zone: ZoneInfo) -> bool:
        moment = moment.astimezone(UTC)
        local = moment.astimezone(zone)
        opening = self.opening_for(local.date())
        if opening is None:
            return False
        open_at = self._localize(local.date(), opening.open_minute, zone)
        close_at = self._localize(local.date(), opening.close_minute, zone)
        return open_at <= moment < close_at

    def business_seconds(self, start: datetime, end: datetime, zone: ZoneInfo) -> int:
        """[start, end) 内落在营业时段的整秒数；结束早于开始时为 0。"""
        start = start.astimezone(UTC)
        end = end.astimezone(UTC)
        if end <= start:
            return 0
        total = 0.0
        day = start.astimezone(zone).date()
        last_day = end.astimezone(zone).date()
        while day <= last_day:
            for seg_start, seg_end, is_open in self._segments(day, zone):
                if not is_open:
                    continue
                lo = max(start, seg_start)
                hi = min(end, seg_end)
                if hi > lo:
                    total += (hi - lo).total_seconds()
            day += timedelta(days=1)
        return int(round(total))

    def advance(self, start: datetime, seconds: int, *, business: bool, zone: ZoneInfo) -> datetime:
        """从 start 起累计 seconds 秒营业（或非营业）时间后落在的时刻。

        business=True 用于响应时限；False 用于跨班的升级宽限。若日历在约十年内
        都安排不出足够的对应时段，抛出 ValueError 防止无限扫描。
        """
        if seconds < 0:
            raise ValueError("累计秒数不能为负")
        current = start.astimezone(UTC)
        if seconds == 0:
            return current
        remaining = float(seconds)
        day = current.astimezone(zone).date()
        for _ in range(_MAX_SCAN_DAYS + 1):
            for seg_start, seg_end, is_open in self._segments(day, zone):
                lo = max(current, seg_start)
                hi = seg_end
                if hi <= lo:
                    continue
                if is_open == business:
                    available = (hi - lo).total_seconds()
                    if remaining <= available:
                        return lo + timedelta(seconds=remaining)
                    remaining -= available
                current = hi
            day += timedelta(days=1)
        raise ValueError("营业日历在可预见范围内没有足够的营业或休息时段")

    def summary(self) -> dict:
        exceptions = []
        for day in sorted(self.exceptions):
            kind, value = self.exceptions[day]
            if kind == "closed":
                exceptions.append({"date": day.isoformat(), "status": "closed", "reason": str(value)})
            else:
                opening: DayOpening = value  # type: ignore[assignment]
                exceptions.append({"date": day.isoformat(), "status": "open",
                                   "open": _minute_text(opening.open_minute),
                                   "close": _minute_text(opening.close_minute), "reason": ""})
        return {
            "weekly_hours": {
                day: ({"open": _minute_text(window[0]), "close": _minute_text(window[1])} if window else None)
                for day, window in self.weekly.items()
            },
            "exceptions": exceptions,
        }


def _minute_text(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"
