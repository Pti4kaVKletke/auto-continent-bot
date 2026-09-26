# -*- coding: utf-8 -*-
"""Вспомогательные расчёты документов: сумма в валюте, сверка курсов, номер ДКП,
сумма прописью, покупатель-ИП."""
import pytest

import doc_builder
from doc_builder import (AmountMismatchError, DocumentBuilder, _check_amounts,
                         amount_to_words_plain, amount_to_words_rub, dkp_number_from,
                         order_amount)


def test_order_amount_rounds_to_cents():
    assert order_amount("4 271 000", "74,40", "150626002") == 57405.91
    assert order_amount(4073640, 82.80) == 49198.55


def test_order_amount_missing_data():
    assert order_amount("", "80") is None
    assert order_amount("100", "") is None


def test_check_amounts_ok_within_rouble():
    _check_amounts(4073640, [("49198,55", "82,80", "поручение")], "Акт")


def test_check_amounts_each_pair_separately():
    # История 050826001: сумму по курсу поручения нельзя умножать на фактический курс.
    price = 4073640
    ok_pairs = [("49800", "81,80", "поручение"), ("49198,55", "82,80", "факт")]
    _check_amounts(price, ok_pairs, "Акт")
    with pytest.raises(AmountMismatchError):
        _check_amounts(price, [("49800", "82,80", "перепутаны курсы")], "Акт")


def test_check_amounts_skips_empty():
    _check_amounts(4073640, [("", "82,80", "нет суммы"), ("49198,55", "", "нет курса")], "Акт")


@pytest.mark.parametrize("data, expected", [
    ({"car_vin": "LVGEU76A1TG062177"}, "062177"),
    ({"car_vin": "lbv21gt02t1c66483"}, "C66483"),
    ({"car_vin": "LVGEU76A1TG062177", "Номер ДКП": "012309"}, "012309"),  # ручной ввод важнее
    ({}, "FALLBACK"),
])
def test_dkp_number(data, expected):
    assert dkp_number_from(data, fallback="FALLBACK") == expected


def test_words_rub():
    assert amount_to_words_rub(2869000).startswith("Два миллиона восемьсот шестьдесят девять тысяч")
    assert "21 копейка" in amount_to_words_rub(1001.21) or "21 копейк" in amount_to_words_rub(1001.21)


def test_words_plain_currency_cents():
    s = amount_to_words_plain(54730.47)
    assert s.startswith("Пятьдесят четыре тысячи семьсот тридцать")
    assert "47" in s


@pytest.mark.parametrize("n, words", [
    (1, "Один"), (2, "Два"), (21000, "Двадцать одна тысяча"), (1000000, "Один миллион"),
])
def test_words_numbers(n, words):
    assert amount_to_words_rub(n).startswith(words)


# ── Покупатель-ИП ────────────────────────────────────────────────────────

BUYER = {
    "buyer_name": "Иванов Иван Иванович", "buyer_initials": "Иванов И.И.",
    "buyer_birth_date": "01.01.1980", "buyer_address": "г. Москва, ул. Тестовая, д. 1",
    "passport_series": "4500", "passport_number": "123456",
    "passport_issued_by": "ОВД Тестовое", "passport_issued_date": "01.01.2005",
    "passport_code": "770-001",
}


def _legal(**kw):
    b = DocumentBuilder.__new__(DocumentBuilder)
    return b._resolve_buyer_legal_fields({**BUYER, **kw})


def test_buyer_individual():
    d = _legal()
    assert d["buyer_full_details"].startswith("Гражданин(ка) Российской Федерации Иванов Иван Иванович")
    assert d["buyer_signature_inn_line"] == ""
    assert d["buyer_initials"] == "Иванов И.И."


def test_buyer_ip():
    d = _legal(buyer_type="ИП", buyer_inn="180804663178")
    assert d["buyer_full_details"].startswith("Индивидуальный предприниматель Иванов Иван Иванович")
    assert "ИНН 180804663178" in d["buyer_full_details"]
    assert d["buyer_signature_name_line"] == "ИП Иванов Иван Иванович"
    assert d["buyer_signature_inn_line"] == "ИНН 180804663178"
    assert d["buyer_initials"] == "ИП Иванов И.И."


def test_buyer_ip_no_double_prefix_and_ogrnip():
    d = _legal(buyer_type="ИП", buyer_name="ИП Иванов Иван Иванович",
               buyer_initials="ИП Иванов И.И.", buyer_inn="ИНН 180804663178\nОГРНИП 318028000012345")
    assert "ИП ИП" not in d["buyer_signature_name_line"]
    assert d["buyer_initials"] == "ИП Иванов И.И."
    assert d["buyer_signature_inn_line"] == "ИНН 180804663178, ОГРНИП 318028000012345"


def test_buyer_bank_details_lines():
    d = _legal(buyer_bank_details="р/с 40802810000000000001\nАО «Альфа-Банк»\nк/с 301\nБИК 044525593\nлишнее")
    assert d["buyer_bank_line1"] == "р/с 40802810000000000001"
    assert d["buyer_bank_line2"] == "АО «Альфа-Банк»"     # регистр внутри «ёлочек» не ломается
    assert d["buyer_bank_line4"] == "БИК 044525593, лишнее"
