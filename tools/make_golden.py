# -*- coding: utf-8 -*-
"""
Снимок «эталонных» расчётов по реальным сделкам для tests/test_golden_deals.py.

Берёт бэкап журнала (xlsx из папки «Бэкапы журнала» на Drive), оставляет
ТОЛЬКО цифры сделки — номер договора, цену, процент, курсы, суммы, платежи —
и записывает, что по ним сейчас считает бот. ФИО, паспорта, адреса, VIN в файл
не попадают.

Перегенерировать нужно только когда расчёт меняется НАМЕРЕННО (и после
проверки, что старые сделки при этом не поехали):

    python3 tools/make_golden.py "путь/к/journal_backup_ГГГГММДД_ЧЧММСС.xlsx"
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DB_PATH", str(Path(tempfile.gettempdir()) / "golden_tmp.db"))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

OUT = ROOT / "tests" / "data" / "golden_deals.json"

# Только эти поля журнала попадают в файл (никаких персональных данных).
NUMERIC_FIELDS = [
    "Номер договора", "Статус", "car_price", "Комиссия %", "exchange_rate",
    "cash_amount", "Фактический курс", "Сумма выдана (USD)", "Сумма Договора",
    "Сумма Комиссии", "Платежи",
]


def compute(deal: dict) -> dict:
    """То, что бот считает по сделке. Этот же код вызывает тест."""
    import agent
    import money
    from doc_builder import order_amount

    num = deal.get("Номер договора", "")
    legacy = money.is_legacy(num)
    pct = agent._num(deal.get("Комиссия %", 0))
    base = agent._settlement_base(deal)
    commission = money.commission(base, pct, legacy)
    total = agent._calc_total_amount(deal)
    received = sum(p["amount"] for p in agent._parse_payments(deal.get("Платежи", "")))
    return {
        "total": round(total, 2),
        "received": round(received, 2),
        "remainder": round(total - received, 2),
        "base": round(base, 2),
        "commission_on_base": commission,
        "total_on_base": money.add(base, commission, legacy),
        "order_usd": order_amount(deal.get("car_price"), deal.get("exchange_rate"), num),
        "paid_usd": order_amount(base, deal.get("Фактический курс"), num),
    }


def main(path: str) -> None:
    import openpyxl
    from gsheets_service import COLUMNS

    ws = openpyxl.load_workbook(path, data_only=True).active
    deals = []
    for row in ws.iter_rows(min_row=3, values_only=True):
        if not row or not row[0]:
            continue
        full = {COLUMNS[i]: ("" if v is None else str(v)) for i, v in enumerate(row[:len(COLUMNS)])}
        # номер как число «170726006.0» → строка без дробной части
        n = full["Номер договора"]
        if n.endswith(".0"):
            n = n[:-2]
        full["Номер договора"] = n.zfill(9) if n.isdigit() else n
        deal = {k: full.get(k, "") for k in NUMERIC_FIELDS}
        deals.append({"input": deal, "expected": compute(deal)})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(deals, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Сделок: {len(deals)} → {OUT}")


if __name__ == "__main__":
    main(sys.argv[1])
