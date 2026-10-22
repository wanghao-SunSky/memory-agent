"""相对时间换算的参考日历：把 occurred_at 附近的日期预先算好交给模型，避免模型自己做日期算术出错。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def fmt(d: date) -> str:
    return f"{d.isoformat()}({WEEKDAYS[d.weekday()]})"


def reference_calendar(ref: datetime) -> dict:
    d = ref.date()
    monday = d - timedelta(days=d.weekday())
    cal = {
        "参考时间": ref.isoformat(),
        "今天": fmt(d),
        "明天": fmt(d + timedelta(days=1)),
        "后天": fmt(d + timedelta(days=2)),
        "昨天": fmt(d - timedelta(days=1)),
        "前天": fmt(d - timedelta(days=2)),
        "本周(周一开始)": [fmt(monday + timedelta(days=i)) for i in range(7)],
        "下周": [fmt(monday + timedelta(days=7 + i)) for i in range(7)],
        "上周": [fmt(monday - timedelta(days=7 - i)) for i in range(7)],
        "一周前": fmt(d - timedelta(days=7)),
        "两周前": fmt(d - timedelta(days=14)),
        "三周前": fmt(d - timedelta(days=21)),
        "一周后": fmt(d + timedelta(days=7)),
        "两周后": fmt(d + timedelta(days=14)),
        "接下来7天": [fmt(d + timedelta(days=i)) for i in range(1, 8)],
    }
    return cal
