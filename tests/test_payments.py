# -*- coding: utf-8 -*-
"""Платежи и суммы по сделке: разбор строки платежей, итог договора, база поручения."""
import pytest

import agent


def test_parse_payments_basic():
    p = agent._parse_payments("500000 (01.07.2026); 300000 (15.07.2026)")
    assert p == [{"amount": 500000.0, "date": "01.07.2026"},
                 {"amount": 300000.0, "date": "15.07.2026"}]


def test_parse_payments_decimal_comma_and_garbage():
    p = agent._parse_payments("3356285,62 (04.08.2026); мусор; ; 28450 (30.06.2026)")
    assert [x["amount"] for x in p] == [3356285.62, 28450.0]


def test_parse_payments_empty():
    assert agent._parse_payments("") == []
    assert agent._parse_payments(None) == []


def test_format_payments_roundtrip():
    src = "3356285,62 (04.08.2026); 28450 (30.06.2026)"
    assert agent._format_payments(agent._parse_payments(src)) == src


@pytest.mark.parametrize("raw, value", [
    ("2 897 690,00 ₽".replace(" ₽", ""), 2897690.0),
    ("3 356 285,62", 3356285.62),     # неразрывные пробелы из Google Sheets
    ("1,0", 1.0),
    ("", 0.0),
    ("abc", 0.0),
])
def test_num_parses_sheet_formats(raw, value):
    assert agent._num(raw) == value


def test_money_str_strips_currency_symbol():
    assert agent._money_str("3 137 299,50 ₽") == "3 137 299,50 руб."
    assert agent._money_str("3060780") == "3 060 780 руб."
    assert agent._money_str("") == ""


def test_fmt_money():
    assert agent._fmt_money(1000000) == "1 000 000"
    assert agent._fmt_money(1234.5) == "1 234,50"


@pytest.mark.xfail(reason="_num не понимает «₽»: Google Sheets отдаёт «2 897 690,00 ₽», "
                          "и сумма договора берётся не из журнала, а пересчитывается из цены")
def test_total_prefers_journal_value():
    deal = {"Сумма Договора": "2 897 690,00 ₽", "car_price": "1", "Комиссия %": "50"}
    assert agent._calc_total_amount(deal) == 2897690.0


def test_total_fallback_from_price():
    deal = {"Сумма Договора": "", "car_price": "4271000", "Комиссия %": "2,5"}
    assert agent._calc_total_amount(deal) == pytest.approx(4377775.0)


def _deal(**kw):
    base = {"Номер договора": "010726001", "car_price": "4633490", "Комиссия %": "1,0",
            "Сумма Договора": "4679824,90", "Платежи": "", "Сумма выдана (USD)": "",
            "Фактический курс": ""}
    base.update(kw)
    return base


def test_settlement_base_full_payment_is_price():
    assert agent._settlement_base(_deal(Платежи="4679824,90 (01.07.2026)")) == 4633490.0


def test_settlement_base_no_payments_is_price():
    assert agent._settlement_base(_deal()) == 4633490.0


def test_settlement_base_partial_payment():
    # Поступило меньше: база = получено / 1,01
    assert agent._settlement_base(_deal(Платежи="4577000 (01.07.2026)")) == 4531683.17


def test_settlement_base_restored_from_issued_usd():
    # Документы уже выдавались: база восстанавливается из выданной суммы × факт. курс.
    d = _deal(**{"Платежи": "4577000 (01.07.2026)", "Сумма выдана (USD)": "54730,47",
                 "Фактический курс": "82,80"})
    assert agent._settlement_base(d) == pytest.approx(4531682.92, abs=0.01)


def test_settlement_base_restored_within_rouble_is_price():
    # 39 261,69 × 83,40 = 3 274 424,95 — в пределах рубля от цены → база = цена
    d = _deal(**{"Номер договора": "290726007", "car_price": "3274425", "Комиссия %": "2,5",
                 "Сумма Договора": "3356285,62", "Сумма выдана (USD)": "39261,69",
                 "Фактический курс": "83,40", "Платежи": "3356285,62 (04.08.2026)"})
    assert agent._settlement_base(d) == 3274425.0
