"""Отчёт о работе в группе сотрудника.

Тегами: «#фасовка05 20 #доставка 3 вчера». Тег это название работы без пробелов и знаков,
число после тега значит сколько сделано (без числа значит 1).

Столбиком: строка с названием, под ней строки «количество х вес»:
    Готовые крам
    5х05
    10х1
Это 5 шт работы «Готовые крам 0.5» и 10 шт работы «Готовые крам 1».

Слово вроде «вчера» или дата ДД.ММ относится ко всему отчёту, остальной текст становится комментарием.
"""

import re
from dataclasses import dataclass, field

from .dates import looks_like_date

_TAG = re.compile(r"^#(\w+)")
_QTY = re.compile(r"^[xх×*]?(\d+(?:[.,]\d+)?)(?:шт|кг|ч)?$", re.IGNORECASE)


@dataclass
class TagItem:
    tag: str
    qty: str = "1"
    label: str | None = None  # как написал сотрудник, для сообщения «не нашёл работу»


@dataclass
class TagReport:
    items: list[TagItem] = field(default_factory=list)
    date_token: str | None = None
    comment: str | None = None


def parse_tag_report(text: str) -> TagReport:
    report = TagReport()
    rest: list[str] = []
    tokens = text.split()
    i = 0
    while i < len(tokens):
        match = _TAG.match(tokens[i])
        if match:
            item = TagItem(match.group(1))
            qty = _QTY.match(tokens[i + 1]) if i + 1 < len(tokens) else None
            if qty:
                item.qty = qty.group(1)
                i += 1
            report.items.append(item)
        else:
            rest.append(tokens[i])
        i += 1
    report.date_token = next((t for t in rest if looks_like_date(t)), None)
    report.comment = " ".join(t for t in rest if t != report.date_token) or None
    return report


_LINE_QTY = re.compile(r"^(\d+(?:[.,]\d+)?)\s*(?:шт)?\s*(?:[xх×*]\s*(\d+(?:[.,]\d+)?)\s*(?:кг|г|л)?)?$", re.IGNORECASE)
_QUOTES = "\"'«»“”„`"


def _tag(text: str) -> str:
    return "".join(ch for ch in text.lower().replace("ё", "е") if ch.isalnum())


def parse_block_report(text: str) -> TagReport:
    """Отчёт столбиком: название, под ним строки «5х05» (количество х вес) или просто «5»."""
    report = TagReport()
    rest: list[str] = []
    header: str | None = None
    for raw in text.splitlines():
        line = raw.strip().strip(_QUOTES).strip()
        if not line:
            continue
        match = _LINE_QTY.match(line.replace(" ", "")) if header else None
        if match:
            qty, weight = match.group(1), match.group(2)
            label = f"{header} {weight}" if weight else header
            report.items.append(TagItem(_tag(label), qty, label))
        elif looks_like_date(line) and report.date_token is None:
            report.date_token = line
        else:
            if header is not None and not any(item.label and item.label.startswith(header) for item in report.items):
                rest.append(header)  # строка без количеств под ней: это не название, а комментарий
            header = line
    if header is not None and not any(item.label and item.label.startswith(header) for item in report.items):
        rest.append(header)
    report.comment = " ".join(rest) or None
    return report
