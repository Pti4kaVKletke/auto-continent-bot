# -*- coding: utf-8 -*-
"""
Регрессия по реальным сделкам: цифры всех сделок журнала (без персональных
данных) и то, что бот по ним считал на момент снимка. Если правка кода меняет
хоть копейку в старой сделке — тест падает: пересобранный документ разошёлся бы
с подписанным.

Снимок делается скриптом tools/make_golden.py из бэкапа журнала.
"""
import json
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from make_golden import compute  # noqa: E402

DATA = json.loads((Path(__file__).parent / "data" / "golden_deals.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", DATA, ids=[c["input"]["Номер договора"] for c in DATA])
def test_deal_numbers_unchanged(case):
    assert compute(case["input"]) == case["expected"]
