"""Точная арифметика денег и количеств. Только Decimal, никаких float."""

import re
from decimal import (
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    Decimal,
    InvalidOperation,
)

from .errors import ValidationError

ROUNDING_MODES = {
    "HALF_UP": ROUND_HALF_UP,
    "HALF_EVEN": ROUND_HALF_EVEN,
    "DOWN": ROUND_DOWN,
    "UP": ROUND_UP,
    "FLOOR": ROUND_FLOOR,
    "CEILING": ROUND_CEILING,
}

_NUMBER_RE = re.compile(r"^[+-]?\d+(\.\d+)?$")
MAX_DIGITS_BEFORE_POINT = 12


def parse_decimal(
    raw: str,
    *,
    max_scale: int | None = None,
    allow_zero: bool = False,
    allow_negative: bool = False,
    what: str = "число",
) -> Decimal:
    """Разбирает число из ввода пользователя: «3,5», «1 200.50», «1 200,5».

    Лишние знаки после запятой не округляются молча — это ошибка ввода.
    """
    text = (raw or "").strip().replace(" ", "").replace(" ", "").replace("_", "")
    text = text.replace(",", ".")
    if not _NUMBER_RE.match(text):
        raise ValidationError(f"Не понимаю {what}: «{raw}». Пример: 12 или 3,5")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - регулярка уже отсекла мусор
        raise ValidationError(f"Не понимаю {what}: «{raw}»") from exc
    integer_part = text.lstrip("+-").split(".")[0].lstrip("0")
    if len(integer_part) > MAX_DIGITS_BEFORE_POINT:
        raise ValidationError(f"Слишком большое значение: {raw}")
    if max_scale is not None:
        exponent = value.normalize().as_tuple().exponent
        scale = -exponent if isinstance(exponent, int) and exponent < 0 else 0
        if scale > max_scale:
            allowed = "только целое число" if max_scale == 0 else f"не больше {max_scale} знаков после запятой"
            raise ValidationError(f"Слишком точное значение {raw}: допускается {allowed}")
    if value < 0 and not allow_negative:
        raise ValidationError(f"Значение не может быть отрицательным: {raw}")
    if value == 0 and not allow_zero:
        raise ValidationError(f"Значение должно быть больше нуля: {raw}")
    return value


def currency_step(minor_units: int) -> Decimal:
    return Decimal(1).scaleb(-minor_units)


def round_to_step(value: Decimal, step: Decimal, mode: str = "HALF_UP") -> Decimal:
    """Округляет value до кратного step (0.01, 1, 0.5 …) по выбранному режиму."""
    if step <= 0:
        raise ValidationError("Шаг округления должен быть больше нуля")
    rounding = ROUNDING_MODES.get(mode.upper())
    if rounding is None:
        raise ValidationError(f"Неизвестный режим округления: {mode}")
    units = (value / step).quantize(Decimal(1), rounding=rounding)
    result = units * step
    exponent = step.normalize().as_tuple().exponent
    scale = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    return result.quantize(Decimal(1).scaleb(-scale))


def scale_of(value: Decimal) -> int:
    exponent = value.normalize().as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def _group(digits: str) -> str:
    parts = []
    while len(digits) > 3:
        parts.insert(0, digits[-3:])
        digits = digits[:-3]
    parts.insert(0, digits)
    return " ".join(parts)


def fmt_number(value: Decimal, places: int | None = None) -> str:
    """1234567.5 → «1 234 567,50» (при places=2). Без places — без хвостовых нулей."""
    if places is None:
        value = value.normalize()
        places = scale_of(value)
    quantized = value.quantize(Decimal(1).scaleb(-places)) if places else value.quantize(Decimal(1))
    sign = "−" if quantized < 0 else ""
    text = f"{abs(quantized):f}"
    integer, _, fraction = text.partition(".")
    result = _group(integer)
    if fraction:
        result += "," + fraction
    return sign + result


def fmt_money(value: Decimal, currency: str, minor_units: int = 2) -> str:
    return f"{fmt_number(value, minor_units)} {currency}"


def fmt_signed_money(value: Decimal, currency: str, minor_units: int = 2) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{fmt_money(value, currency, minor_units)}"


def fmt_qty(value: Decimal, unit: str) -> str:
    return f"{fmt_number(value)} {unit}"
