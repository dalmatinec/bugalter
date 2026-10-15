"""Отчёт о работе тегами: «#фасовка05 20 #доставка 3 вчера».

Тег — название работы без пробелов и знаков, число после тега — сколько сделано (без числа значит 1).
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
