# -*- coding: utf-8 -*-
"""Округление денег (money.py): старое правило для сделок до 26.09.2026, новое — после."""
import pytest

import money


@pytest.mark.parametrize("number, legacy", [
    ("250926001", True),      # 25.09.2026 — последний день старого правила
    ("260926001", False),     # 26.09.2026 — первый день нового
    ("290726007", True),
    ("170726006.0", True),    # номер, прочитанный из таблицы как число
    ("20626001", True),       # потерян ведущий ноль (020626001)
    ("011026001", False),
    ("", False),              # номер не распознан → новое правило
    ("TEST-001", False),
])
def test_is_legacy(number, legacy):
    assert money.is_legacy(number) is legacy


def test_half_kopeck_old_vs_new():
    # Сделка 290726007: 3 274 425 × 2,5% = 81 860,625 — ровно полкопейки.
    assert money.commission(3274425, 2.5, legacy=True) == 81860.62   # как в подписанных документах
    assert money.commission(3274425, 2.5, legacy=False) == 81860.63  # как у банка и в Excel


def test_commission_and_total_simple():
    assert money.commission(4271000, 2.5) == 106775.0
    assert money.add(4271000, 106775.0) == 4377775.0
    assert money.commission(2869000, 1.0) == 28690.0


def test_div_mul_rates():
    assert money.div(4271000, 74.4) == 57405.91
    assert money.mul(39261.69, 83.40) == 3274424.95


def test_base_from_received():
    # Недоплата: база + база × 1% = получено
    base = money.base_from_received(4577000, 1.0)
    assert base == 4531683.17
    assert money.add(base, money.commission(base, 1.0)) == 4577000.0


@pytest.mark.parametrize("legacy", [True, False])
def test_rounding_is_to_kopecks(legacy):
    for v in (money.commission(1234567, 1.3, legacy), money.div(1000000, 81.37, legacy),
              money.mul(12345.67, 80.01, legacy)):
        assert round(v, 2) == v
