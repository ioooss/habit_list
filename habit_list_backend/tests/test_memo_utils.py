"""memo_utils 单元测试（和 app.html 行为 1:1 对齐）。"""
from __future__ import annotations

from app.memory.memo_utils import detect_memo, extract_due_text, guess_importance, guess_offset


def _hit(text: str):
    r = detect_memo(text)
    return r.hit, r.due_text, r.importance, r.offset, r.clean_text


def test_plain_confide_not_hit():
    hit, *_ = _hit("今天一个人吃饭有点孤单。")
    assert hit is False


def test_remind_me_tomorrow_afternoon_3pm():
    hit, due, imp, off, clean = _hit("明天下午三点提醒我交周报")
    assert hit is True
    assert "明天" in due and "下午" in due
    assert imp in {"yellow", "red"}
    assert off == 1
    assert "交周报" in clean
    assert "提醒我" not in clean


def test_tonight_remind_urgent():
    hit, due, imp, off, clean = _hit("今晚8点必须给妈打电话，别忘了啊")
    assert hit is True
    assert "今晚" in due
    assert imp == "red"
    assert off == 0
    assert "打电话" in clean


def test_pure_pm_slot_counts_as_today():
    hit, due, imp, off, clean = _hit("下午开周会，记一下")
    assert hit is True
    assert "下午" in due
    assert imp == "yellow"
    assert off == 0


def test_next_tuesday():
    hit, due, imp, off, clean = _hit("下周二去体检")
    assert hit is True
    assert "周二" in due
    assert off in {4, 7}


def test_date_month_day():
    hit, due, imp, off, clean = _hit("8月15号要交房租")
    assert hit is True
    assert "8月15号" in due or "交房租" in clean
