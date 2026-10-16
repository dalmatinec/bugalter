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


_NUM = r"\d+(?:[.,]\d+)?"
# «5», «x5», «5шт»: сколько штук.
_QTY_ONLY = re.compile(rf"^[xх×*]?({_NUM})(?:шт|штук)?\.?$", re.IGNORECASE)
# «5х05», «5 x 0,5», «5шт х 1кг»: количество х вес.
_QTY_WEIGHT = re.compile(rf"^({_NUM})(?:шт|штук)?[xх×*]({_NUM})(?:кг|г|гр|л)?$", re.IGNORECASE)
_QUOTES = "\"'«»“”„`"


def _tag(text: str) -> str:
    return "".join(ch for ch in text.lower().replace("ё", "е") if ch.isalnum())


def _qty_line(text: str) -> tuple[str, str | None] | None:
    compact = text.replace(" ", "")
    if match := _QTY_WEIGHT.match(compact):
        return match.group(1), match.group(2)
    if match := _QTY_ONLY.match(compact):
        return match.group(1), None
    return None


def _one_line(line: str, known: set[str]) -> tuple[TagItem | None, str, bool]:
    """Строка целиком: «Крам факт 1 13», «Крам факт 1», «Готовые крам 5х05».

    Возвращает (позиция или None, название для строк ниже, количество не указано).
    """
    words = line.split()
    # Самое длинное начало строки, совпадающее с названием работы; дальше количество.
    for k in range(len(words), 0, -1):
        name = " ".join(words[:k])
        if _tag(name) not in known:
            continue
        tail = " ".join(words[k:])
        if not tail:
            return TagItem(_tag(name), "1", name), name, True
        parsed = _qty_line(tail)
        if parsed and parsed[1] is None:
            return TagItem(_tag(name), parsed[0], name), name, False
        if parsed:
            label = f"{name} {parsed[1]}"
            return TagItem(_tag(label), parsed[0], label), name, False
    # Название, которого нет в работах, и в конце «5х05»: работа «название 05».
    if len(words) > 1 and (parsed := _qty_line(words[-1])) and parsed[1] is not None:
        name = " ".join(words[:-1])
        label = f"{name} {parsed[1]}"
        return TagItem(_tag(label), parsed[0], label), name, False
    return None, line, True


def parse_block_report(text: str, known: set[str] | frozenset[str] = frozenset()) -> TagReport:
    """Отчёт без решёток, одной строкой или столбиком. known: теги существующих работ.

    «Крам факт 1 13»          13 шт работы «Крам факт 1» (без числа в конце значит 1)
    «Готовые крам 5х05»       5 шт работы «Готовые крам 0.5»
    «Готовые крам» и ниже «5х05», «10х1»: то же самое столбиком.
    """
    report = TagReport()
    rest: list[str] = []
    header: str | None = None
    header_item: TagItem | None = None  # «Название» без числа: 1 шт, если ниже нет строк с количеством
    header_used = False

    def close_header():
        if header is not None and not header_used and header_item is None:
            rest.append(header)  # строка без количеств: это комментарий

    for raw in text.splitlines():
        line = raw.strip().strip(_QUOTES).strip()
        if not line:
            continue
        parsed = _qty_line(line) if header is not None else None
        if parsed:
            qty, weight = parsed
            label = f"{header} {weight}" if weight else header
            if header_item is not None:
                report.items.remove(header_item)
                header_item = None
            report.items.append(TagItem(_tag(label), qty, label))
            header_used = True
            continue
        if report.date_token is None and looks_like_date(line):
            report.date_token = line
            continue
        item, name, implicit = _one_line(line, known)
        close_header()
        header, header_used = name, False
        header_item = item if item is not None and implicit else None
        if item is not None:
            report.items.append(item)
            header_used = not implicit
    close_header()
    report.comment = " ".join(rest) or None
    return report
