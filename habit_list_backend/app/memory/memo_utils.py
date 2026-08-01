"""直接把 app.html 里 JS 的备忘识别移植成 Python：
detectMemo / extractDueText / guessImportance / guessOffset。

JS 版是权威（已经用户验证），这里 Python 版行为必须 1:1。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MEMO_HINTS = [
    re.compile(r"提醒我|叫我|别忘了|记得|到时候|到时"),
    re.compile(r"记一下|记下来|备忘|托你|托付|放备忘"),
    re.compile(r"明天|后天|大后天|今晚|今天下午|今天早上|上午|下午|晚上|早上|夜里|凌晨"),
    re.compile(r"下周|这周|本周|周一|周二|周三|周四|周五|周六|周日|周末"),
    re.compile(
        r"(早上|上午|中午|下午|晚上|夜里|凌晨)?\s*"
        r"([一二三四五六七八九十]|两|十几?|二十[一二三四五六七八九]?|[012]?\d)\s*"
        r"(点|点钟|点半|:30|:15|:45)(之前|之前弄好|前交|前发|之前搞完)?"
    ),
    re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]"),
    re.compile(r"交周报|发周报|交房租|还信用卡|还钱|付.*款|买.*药|买药|接.*人|送机|接机|开会|面试|体检|复诊"),
]

DUE_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"(明天|后天|大后天|今晚|今天|今天下午|今天早上|今天上午|本周|这周|下周|周末)"
        r"\s*(早上|上午|中午|下午|晚上|夜里|凌晨)?"
        r"\s*([一二三四五六七八九十]|两|十几?|二十[一二三四五六七八九]?|[012]?\d)?"
        r"\s*(点|点钟|点半|:30|:15|:45)?(之前|之前弄好|前交|前发|之前搞完)?"
    ),
    re.compile(
        r"(周一|周二|周三|周四|周五|周六|周日)"
        r"\s*(早上|上午|中午|下午|晚上|夜里|凌晨)?"
        r"\s*([一二三四五六七八九十]|两|十几?|二十[一二三四五六七八九]?|[012]?\d)?"
        r"\s*(点|点钟|点半|:30|:15|:45)?"
    ),
    re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]"),
    re.compile(
        r"(早上|上午|中午|下午|晚上|夜里|凌晨)"
        r"\s*([一二三四五六七八九十]|两|十几?|二十[一二三四五六七八九]?|[012]?\d)"
        r"\s*(点|点钟|点半|:30|:15|:45)"
    ),
    re.compile(
        r"(明天|后天|大后天|今晚|今天|今天下午|今天早上|本周|这周|下周|周末|周一|周二|周三|周四|周五|周六|周日)"
        r"\s*(早上|上午|中午|下午|晚上|夜里|凌晨)?"
    ),
    re.compile(r"(今天|今晚|今天下午|今天早上|今天上午|早上|上午|中午|下午|晚上|夜里|凌晨)"),
]


def extract_due_text(t: str) -> str:
    for p in DUE_PATTERNS:
        m = p.search(t or "")
        if m:
            return re.sub(r"\s+", "", m.group(0))
    return ""


def guess_importance(t: str, due_text: str) -> str:
    today_like = bool(
        re.search(r"今天|今晚|现在|马上|立刻|尽快|必须|一定|一定要|deadline|最后一天|逾期", t)
        or re.search(r"今天|今晚|现在|马上|立刻|下午|上午|早上|晚上|中午|凌晨", due_text)
    )
    if today_like:
        if re.search(r"必须|一定|一定得|赶|要交|得交|逾期|最后|立刻|马上", t):
            return "red"
        return "yellow"
    if re.search(r"明天|后天", due_text) and re.search(r"交|发|要给|汇报|开会|面试|体检|还", t):
        return "yellow"
    if re.search(r"重要|要紧|不能忘|一定要", t):
        return "yellow"
    return "green"


def guess_offset(due_text: str, t: str) -> int:
    if re.search(r"下周|周一|周二|周三|周四", due_text):
        return 7
    if re.search(r"本周|这周|周末|周五|周六|周日", due_text):
        return 4
    if re.search(r"大后天", due_text):
        return 3
    if re.search(r"后天", due_text):
        return 2
    if re.search(r"明天", due_text):
        return 1
    if re.search(r"今天|今晚|现在|马上|立刻|下午|上午|早上|晚上|中午|凌晨", due_text):
        return 0
    return 30


_STRIP_RE1 = re.compile(r"(请?你?)?(千万?|一定)?(提醒我|叫我|记得|别忘了|记一下|记下来|帮我记|到时候|到时)[，。,\.\s]*")
_STRIP_RE2 = re.compile(r"[，。,\.\s]+$")
_STRIP_RE3 = re.compile(r"，(了|哈|啊|哦|呢)\s*$")


def _clean_text(t: str) -> str:
    s = t
    s = _STRIP_RE1.sub("", s)
    s = _STRIP_RE3.sub("", s)
    s = _STRIP_RE2.sub("", s)
    return s or t


@dataclass
class MemoDetectResult:
    hit: bool
    due_text: str
    importance: str
    offset: int
    clean_text: str
    matched_rules: list[int]


def detect_memo(text: str) -> MemoDetectResult:
    """识别一条输入是不是备忘，返回命中情况。"""
    matched: list[int] = []
    for i, r in enumerate(MEMO_HINTS):
        if r.search(text or ""):
            matched.append(i)
    if not matched:
        return MemoDetectResult(False, "", "green", 30, text or "", [])
    due = extract_due_text(text)
    imp = guess_importance(text, due)
    off = guess_offset(due, text)
    clean = _clean_text(text)
    return MemoDetectResult(True, due, imp, off, clean, matched)
