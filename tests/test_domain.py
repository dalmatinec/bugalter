import datetime as dt
from decimal import Decimal

import pytest

from bugalter.domain.dates import parse_date, parse_range, semi_monthly_bounds
from bugalter.domain.errors import ValidationError
from bugalter.domain.money import fmt_money, parse_decimal, round_to_step

D = Decimal


@pytest.mark.parametrize(
    "raw, expected",
    [("3", D("3")), ("3,5", D("3.5")), ("1 200,50", D("1200.50")), ("0.001", D("0.001")), ("+7", D("7"))],
)
def test_parse_decimal_ok(raw, expected):
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "1e5", "NaN", "inf", "1,2,3", "--1", "1.2.3"])
def test_parse_decimal_rejects_garbage(raw):
    with pytest.raises(ValidationError):
        parse_decimal(raw)


def test_parse_decimal_scale_is_not_silently_rounded():
    assert parse_decimal("1.255", max_scale=3) == D("1.255")
    with pytest.raises(ValidationError):
        parse_decimal("1.2555", max_scale=3)
    with pytest.raises(ValidationError):
        parse_decimal("2.5", max_scale=0)
    assert parse_decimal("2.50", max_scale=1) == D("2.5")  # хвостовой ноль не считается


def test_parse_decimal_sign_and_zero():
    with pytest.raises(ValidationError):
        parse_decimal("-1")
    with pytest.raises(ValidationError):
        parse_decimal("0")
    assert parse_decimal("0", allow_zero=True) == 0


def test_rounding_example_from_design():
    # Фасовка 1,255 кг × 12,35 = 15,49925 → 15,50
    assert round_to_step(D("1.255") * D("12.35"), D("0.01")) == D("15.50")
    assert round_to_step(D("2.5"), D("1"), "HALF_EVEN") == D("2")
    assert round_to_step(D("2.5"), D("1"), "HALF_UP") == D("3")
    assert round_to_step(D("10.07"), D("0.05")) == D("10.05")
    assert round_to_step(D("-0.005"), D("0.01")) == D("-0.01")


def test_fmt_money():
    assert fmt_money(D("23670"), "USD") == "23 670,00 USD"
    assert fmt_money(D("-144"), "USD") == "−144,00 USD"


def test_semi_monthly_bounds():
    assert semi_monthly_bounds(dt.date(2026, 10, 15), 15) == (dt.date(2026, 10, 1), dt.date(2026, 10, 15))
    assert semi_monthly_bounds(dt.date(2026, 10, 16), 15) == (dt.date(2026, 10, 16), dt.date(2026, 10, 31))
    assert semi_monthly_bounds(dt.date(2026, 2, 20), 15) == (dt.date(2026, 2, 16), dt.date(2026, 2, 28))


def test_parse_dates_and_ranges():
    today = dt.date(2026, 10, 26)
    assert parse_date("вчера", today) == dt.date(2026, 10, 25)
    assert parse_date("14.10", today) == dt.date(2026, 10, 14)
    assert parse_date("31.12", today) == dt.date(2025, 12, 31)
    assert parse_range("неделя", today) == (dt.date(2026, 10, 26), today)
    assert parse_range("месяц", today) == (dt.date(2026, 10, 1), today)
    assert parse_range("прошлый_месяц", today) == (dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert parse_range("01.10-15.10", today) == (dt.date(2026, 10, 1), dt.date(2026, 10, 15))
    assert parse_range("2026-10-01..2026-10-15", today) == (dt.date(2026, 10, 1), dt.date(2026, 10, 15))
    with pytest.raises(ValidationError):
        parse_date("32.10", today)


@pytest.mark.parametrize(
    "raw, expected", [("1", [1]), ("1,2", [1, 2]), ("", []), ("111, 222", [111, 222]), ("5;6", [5, 6])]
)
def test_owner_ids_from_env(monkeypatch, raw, expected):
    from bugalter.config import Config

    monkeypatch.setenv("OWNER_TELEGRAM_IDS", raw)
    assert Config().owner_telegram_ids == expected


def test_owner_ids_reject_garbage(monkeypatch):
    from pydantic import ValidationError as PydanticValidationError

    from bugalter.config import Config

    monkeypatch.setenv("OWNER_TELEGRAM_IDS", "@ivan")
    with pytest.raises(PydanticValidationError):
        Config()
