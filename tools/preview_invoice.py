# -*- coding: utf-8 -*-
"""
Локальный предпросмотр счёта: собирает счёт настоящим кодом бота
(DocumentBuilder.build_invoice) на тестовых данных, ничего не деплоя.

Вариант бланка — общая настройка TEMPLATE_VARIANT (как в боте); ключ --variant
выставляет эту переменную окружения перед сборкой.

Примеры:
    python3 tools/preview_invoice.py                 # текущий вариант из настроек
    python3 tools/preview_invoice.py --variant v2    # проверить бланк v2
    python3 tools/preview_invoice.py --variant v2 --type direct --no-pdf
"""
import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "_ПРЕДПРОСМОТР"

# Тестовая сделка. Меняйте свободно — на боевые данные не влияет.
BASE = {
    "buyer_name":   "Иванов Иван Иванович",
    "company_name": "Иванов Иван Иванович",
    "car_model":    "Zeekr 001",
    "car_year":     "2024",
    "car_vin":      "LB37A2Z27RX123456",
    "car_price":    "3 250 000",
    "currency":     "RUB",
}

BANK_DIRECT = {
    "account_type":     "direct_rf",
    "account_number":   "40702810100000123456",
    "account_currency": "RUB",
    "bank_name":        'АО "ТБанк"',
    "bank_bic":         "044525974",
    "bank_corr_acc":    "30101810145250000974",
}

BANK_CORR = {
    "account_type":     "corr",
    "account_number":   "1290000000123456",
    "account_currency": "RUB",
    "bank_name":        'ОАО "Оптима Банк"',
    "bank_bic":         "128001",
    "bank_corr_acc":    "30111810400000000123",
    "corr_bank_name":   'АО "Банк-корреспондент"',
    "corr_bank_bic":    "044525000",
    "corr_bank_acc":    "30111810900000000456",
}


def to_pdf(xlsx: Path):
    """Конвертация через LibreOffice во временную папку — чтобы служебные
    .tmp и .~lock не оставались рядом с готовыми файлами."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        print("  PDF пропущен: LibreOffice не найден")
        return None
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", tmp, str(xlsx)],
            check=False, capture_output=True, timeout=180,
            # как в doc_builder.convert_to_pdf: суммы в русском формате
            env={**os.environ, "LC_ALL": "ru_RU.UTF-8", "LANG": "ru_RU.UTF-8"},
        )
        made = Path(tmp) / (xlsx.stem + ".pdf")
        if not made.exists():
            return None
        dst = xlsx.with_suffix(".pdf")
        shutil.copy(made, dst)
        return dst


async def build(kind: str, make_pdf: bool) -> None:
    import doc_builder

    data = dict(BASE)
    data.update(BANK_DIRECT if kind == "direct" else BANK_CORR)

    builder = doc_builder.DocumentBuilder()
    path = Path(await builder.build_invoice(data, "TEST-001", "04.09.2026",
                                            commission_pct=1.0))
    OUT_DIR.mkdir(exist_ok=True)
    dst = OUT_DIR / f"Счёт_превью_{kind}.xlsx"
    shutil.copy(path, dst)
    print(f"  XLSX: {dst.name}")
    if make_pdf:
        pdf = to_pdf(dst)
        if pdf:
            print(f"  PDF:  {pdf.name}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", choices=["direct", "corr", "both"], default="both",
                    help="direct — прямой счёт в РФ, corr — через банк-корреспондент")
    ap.add_argument("--variant", help="вариант бланка: v1, v2 … (по умолчанию — как в настройках)")
    ap.add_argument("--no-pdf", action="store_true")
    args = ap.parse_args()

    os.environ.setdefault("TEMPLATES_DIR", str(ROOT / "templates"))
    if args.variant:
        os.environ["TEMPLATE_VARIANT"] = args.variant

    import doc_builder
    print(f"Доступные варианты бланка: {doc_builder.template_variants()}")
    print(f"Собираю вариантом: {doc_builder.template_variant()}\n")

    kinds = ["direct", "corr"] if args.type == "both" else [args.type]
    for kind in kinds:
        print(f"[{kind}]")
        asyncio.run(build(kind, not args.no_pdf))

    print(f"\nГотово. Файлы в папке: {OUT_DIR}")


if __name__ == "__main__":
    main()
