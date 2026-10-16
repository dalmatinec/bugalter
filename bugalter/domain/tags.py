"""Отчёт о работе в группе сотрудника.

Тегами: «#фасовка05 20 #доставка 3 вчера». Тег это название работы без пробелов и знаков,
число после тега значит сколько сделано (без числа значит 1).

Без решёток: [товар] работа [вес] [количество], одной строкой или столбиком.
Если работа считается по весу (единица кг, г или л), цена задаётся за 1 единицу веса
и бот сам умножает: «Крам факт 0.5 13» это 13 шт по 0,5, то есть 6,5 по цене работы «Факт».
Товар (Крам, Трюфель) это просто подпись, для него не нужна отдельная работа.
Если для какого-то веса цена особая, заводится работа с весом в названии («Факт 0.5»), она важнее.

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
    qty: str = "1"  # штук
    label: str | None = None  # как написал сотрудник, для сообщения «не нашёл работу»
    weight: str | None = None  # вес одной штуки для работы по весу
    product: str | None = None  # подпись: какой товар


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


def _num(text: str) -> str | None:
    match = re.fullmatch(rf"({_NUM})(?:шт|штук|кг|г|гр|л)?\.?", text, re.IGNORECASE)
    return match.group(1) if match else None


def _weight_tag(weight: str) -> str:
    return _tag(weight)


def _one_line(line: str, known: dict[str, bool], qty_only: bool = False) -> tuple[TagItem | None, str, bool]:
    """Строка целиком: «[товар] работа [вес] [количество]» или «... количествоХвес».

    known: тег работы -> считается ли она по весу. qty_only: одиночное число это штуки (строка столбика).
    Возвращает (позиция или None, название для строк ниже, количество не указано).
    """
    words = line.split()
    for start in range(len(words)):
        for end in range(len(words), start, -1):
            work_name = " ".join(words[start:end])
            tag = _tag(work_name)
            if tag not in known:
                continue
            # Товар может стоять и после работы: «Готовые крам 5х05».
            tail = words[end:]
            after = 0
            while after < len(tail) and _qty_line(tail[after]) is None and _num(tail[after]) is None:
                after += 1
            if after and not tail[after:]:
                continue  # «доставка будет позже»: слова после работы без чисел, это не отчёт
            extra = (_tag(" ".join(words[:start])), _tag(" ".join(tail[:after])))
            item = _resolve(tag, work_name, tail[after:], known, qty_only, extra)
            if item is None:
                continue
            item.product = " ".join(words[:start] + tail[:after]) or None
            item.label = line
            implicit = not tail[after:]
            return item, line, implicit
    # Название, которого нет в работах, и в конце «5х05»: работа «название 05».
    if len(words) > 1 and (parsed := _qty_line(words[-1])) and parsed[1] is not None:
        name = " ".join(words[:-1])
        label = f"{name} {parsed[1]}"
        return TagItem(_tag(label), parsed[0], label), name, False
    return None, line, True


def _resolve(
    tag: str, name: str, tail: list[str], known: dict[str, bool], qty_only: bool, extra=("", "")
) -> TagItem | None:
    """Хвост после названия работы: ничего, «13», «0.5 13», «13х0.5».

    extra: товар до и после названия работы, чтобы найти работу вроде «Готовые крам 0.5».
    """
    weighted = known[tag]
    before, after = extra

    def with_weight(weight: str, qty: str) -> TagItem | None:
        w = _weight_tag(weight)
        for exact in (tag + after + w, before + tag + w, tag + w):
            if exact in known:  # особая цена для этого веса
                return TagItem(exact, qty, f"{name} {weight}")
        if weighted:
            return TagItem(tag, qty, f"{name} {weight}", weight=weight)
        return TagItem(exact, qty, f"{name} {weight}")  # такой работы нет, покажем «не нашёл»

    if not tail:
        return TagItem(tag, "1", name)
    if len(tail) == 1:
        parsed = _qty_line(tail[0])
        if parsed is None:
            return None
        qty, weight = parsed
        if weight is not None:
            return with_weight(weight, qty)
        if weighted and not qty_only:
            return with_weight(qty, "1")  # «Факт 0.5»: одна штука весом 0,5
        return TagItem(tag, qty, name)
    if len(tail) == 2:
        weight, qty = _num(tail[0]), _qty_line(tail[1])
        if weight is None or qty is None or qty[1] is not None:
            return None
        return with_weight(weight, qty[0])
    return None


def parse_block_report(text: str, known=None) -> TagReport:
    """Отчёт без решёток, одной строкой или столбиком. known: теги существующих работ.

    «Крам факт 1 13»          13 шт работы «Крам факт 1» (без числа в конце значит 1)
    «Готовые крам 5х05»       5 шт работы «Готовые крам 0.5»
    «Готовые крам» и ниже «5х05», «10х1»: то же самое столбиком.
    """
    if known is None:
        known = {}
    elif not isinstance(known, dict):
        known = dict.fromkeys(known, False)
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
            synthetic = f"{header} {qty}х{weight}" if weight else f"{header} {qty}"
            item, _, _ = _one_line(synthetic, known, qty_only=True)
            if item is None:
                label = f"{header} {weight}" if weight else header
                item = TagItem(_tag(label), qty, label)
            if header_item is not None:
                report.items.remove(header_item)
                header_item = None
            report.items.append(item)
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


@dataclass
class GoodsLine:
    name: str  # как написано: «Крам»
    weight: str | None  # «05», «1» или None
    qty: str
    text: str


def parse_goods_list(text: str) -> tuple[list[GoodsLine], list[str]]:
    """Список сданного товара: «Крам 05 13», «Трюфель 1 17» (товар, вес, штук) или «Крам 13»."""
    lines, other = [], []
    for raw in text.splitlines():
        line = raw.strip().strip(_QUOTES).strip()
        if not line:
            continue
        words = line.split()
        nums = 0
        while nums < len(words) and _num(words[-1 - nums]) is not None:
            nums += 1
        name = " ".join(words[: len(words) - nums])
        tail = words[len(words) - nums :]
        if not name or nums not in (1, 2):
            other.append(line)
            continue
        weight, qty = (tail[0], tail[1]) if nums == 2 else (None, tail[0])
        lines.append(GoodsLine(name, weight, _num(qty), line))
    return lines, other
