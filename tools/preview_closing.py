# -*- coding: utf-8 -*-
"""
Локальный предпросмотр закрывающих документов: расписка, акт и отчёт агента
собираются настоящим кодом бота (DocumentBuilder) на тестовой сделке.

Проверяет главное: суммы считаются от фактически поступивших средств, а при
полной оплате остаются прежними.

    python3 tools/preview_closing.py                 # оба случая
    python3 tools/preview_closing.py --received 4577000
"""
import argparse
import asyncio
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "_ПРЕДПРОСМОТР"

PRICE = 4633490.0
PCT = 1.0

DEAL = {
    "buyer_name": "Вдовкин Станислав Евгеньевич",
    "buyer_birth_date": "01.01.1980",
    "buyer_address": "г. Москва, ул. Тестовая, д. 1",
    "buyer_initials": "Вдовкин С.Е.",
    "passport_series": "4500", "passport_number": "123456",
    "passport_issued_by": "ОВД Тестовое", "passport_issued_date": "01.01.2005",
    "passport_code": "770-001",
    "seller_name": "Жолдошбеков Арген Дуйшонбекович",
    "seller_birth_date": "02.02.1985",
    "seller_address": "г. Бишкек, ул. Тестовая, д. 2",
    "seller_initials": "Жолдошбеков А.Д.",
    "seller_id_number": "ID1234567", "seller_id_issued_by": "МКК КР",
    "seller_id_issued_date": "03.03.2015",
    "car_model": "BMW X3", "car_year": "2025", "car_vin": "LBV21GT02T1C66483",
    "car_price": str(PRICE), "currency": "рублей", "cash_currency": "долларов США",
    "exchange_rate": "82,00", "Фактический курс": "82,80",
    "Дата поступления": "01.07.2026", "Дата расчёта": "05.07.2026",
}


def base_of(received: float) -> float:
    """То же, что _settlement_base в agent.py, но без импорта всего бота."""
    if received >= round(PRICE * (1 + PCT / 100), 2) - 0.01:
        return PRICE
    return round(received / (1 + PCT / 100), 2)


async def build(received: float, tag: str) -> None:
    import doc_builder

    base = base_of(received)
    data = dict(DEAL)
    data["Сумма выдана (USD)"] = f"{doc_builder.order_amount(base, 82.80):.2f}".replace(".", ",")
    data["cash_amount"] = f"{doc_builder.order_amount(PRICE, 82.00):.2f}".replace(".", ",")

    builder = doc_builder.DocumentBuilder()
    OUT_DIR.mkdir(exist_ok=True)

    made = {
        "Расписка": await builder.build_receipt(
            data, f"TEST-{tag}", "01.07.2026", "05.07.2026", PCT,
            settlement_price=base),
        "Акт": await builder.build_act(
            data, f"TEST-{tag}", "01.07.2026", "05.07.2026", PCT,
            settlement_price=base),
        "Отчёт": await builder.build_report(
            data, f"TEST-{tag}", "01.07.2026", "05.07.2026", "01.07.2026",
            "05.07.2026", PCT, settlement_price=base),
    }

    com = round(base * PCT / 100, 2)
    print(f"[{tag}] поступило {received:,.2f} -> база {base:,.2f} + комиссия {com:,.2f}"
          f" = {base + com:,.2f}".replace(",", " "))
    for name, path in made.items():
        dst = OUT_DIR / f"{name}_превью_{tag}.docx"
        shutil.copy(path, dst)
        print(f"    {dst.name}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--received", type=float,
                    help="сколько денег поступило (по умолчанию — оба случая)")
    args = ap.parse_args()

    os.environ.setdefault("TEMPLATES_DIR", str(ROOT / "templates"))

    if args.received:
        asyncio.run(build(args.received, "факт"))
    else:
        asyncio.run(build(round(PRICE * 1.01, 2), "полная"))
        asyncio.run(build(4577000.0, "недоплата"))

    print(f"\nГотово. Файлы в папке: {OUT_DIR}")


if __name__ == "__main__":
    main()
