"""
money.py — округление денежных сумм до копеек в одном месте.

Правило (с 26.09.2026): математическое округление «половина вверх» по точной
десятичной арифметике (Decimal). Так считают банк и Excel: 81 860,625 → 81 860,63.

Раньше суммы округлялись float-функцией round(), и ровно-половинная копейка
могла уйти вниз (81 860,625 → 81 860,62). Сверка по журналу 25.09.2026
(74 сделки) нашла один такой случай — 290726007.

Чтобы пересборка документов по СТАРЫМ сделкам давала те же цифры, что в уже
подписанных экземплярах, для сделок с датой договора ДО 26.09.2026 остаётся
прежнее округление (решение Ильи 25.09.2026). Дата берётся из номера договора
(ДДММГГNNN) — он всегда есть там, где считаются суммы.
"""

from datetime import date
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

NEW_ROUNDING_FROM = date(2026, 9, 26)
_Q = Decimal("0.01")


def is_legacy(contract_number) -> bool:
    """True — сделка до 26.09.2026, считаем по-старому. Номер не распознан → новое правило."""
    s = str(contract_number or "").strip()
    if s.endswith(".0"):            # номер прочитан из таблицы как число
        s = s[:-2]
    if len(s) == 8 and s.isdigit():  # потерян ведущий ноль (020626001 → 20626001)
        s = "0" + s
    if len(s) != 9 or not s.isdigit():
        return False
    try:
        d = date(2000 + int(s[4:6]), int(s[2:4]), int(s[0:2]))
    except ValueError:
        return False
    return d < NEW_ROUNDING_FROM


def _dec(x) -> Decimal:
    # str(float) — кратчайшее представление, т.е. то число, которое ввели
    # (4271000.0, 2.5, 82.8), без хвостов двоичной арифметики.
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


def _q(d: Decimal) -> float:
    return float(d.quantize(_Q, rounding=ROUND_HALF_UP))


def commission(price, pct, legacy: bool = False) -> float:
    """Комиссия = цена × % / 100, до копеек."""
    if legacy:
        return round(float(price) * float(pct) / 100, 2)
    return _q(_dec(price) * _dec(pct) / 100)


def add(a, b, legacy: bool = False) -> float:
    """Сумма двух денежных величин, до копеек."""
    if legacy:
        return round(float(a) + float(b), 2)
    return _q(_dec(a) + _dec(b))


def mul(a, b, legacy: bool = False) -> float:
    """Произведение (сумма в валюте × курс), до копеек."""
    if legacy:
        return round(float(a) * float(b), 2)
    return _q(_dec(a) * _dec(b))


def div(a, b, legacy: bool = False) -> float:
    """Частное (рубли / курс), до копеек."""
    if legacy:
        return round(float(a) / float(b), 2)
    return _q(_dec(a) / _dec(b))


def base_from_received(received, pct, legacy: bool = False) -> float:
    """База поручения при неполной оплате: получено / (1 + % / 100)."""
    if legacy:
        return round(float(received) / (1 + float(pct) / 100.0), 2)
    return _q(_dec(received) / (1 + _dec(pct) / 100))
