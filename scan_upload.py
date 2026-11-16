"""
scan_upload.py — загрузка скана через бота: тип по имени файла и фото → PDF.

Без Telegram и Drive, только чистые функции, чтобы их можно было тестировать.

• auto_scan_type — если файл уже назван «Подп_ДКП_ТС_…», «Скан_АД_…» и т.п.,
  тип берётся из имени (тот же разбор, что строит статус «Сканы»), и бот не
  задаёт вопрос. Если в имени номер ДРУГОЙ сделки — автоопределение отключается
  и возвращается предупреждение: подписанный документ не должен молча уйти не в
  ту папку.
• images_to_pdf — фото страниц (одно или несколько) склеиваются в один PDF.
"""

import os
import re

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}

# Номер сделки — ровно 9 цифр (280626001), не часть более длинного числа.
_CONTRACT_RE = re.compile(r"(?<!\d)\d{9}(?!\d)")


def is_image_name(filename: str) -> bool:
    return os.path.splitext(filename or "")[1].lower() in IMAGE_EXTS


def contract_numbers_in(filename: str) -> set:
    return set(_CONTRACT_RE.findall(filename or ""))


def auto_scan_type(filename: str, deal_num: str, allowed_codes) -> tuple:
    """
    (тип, предупреждение).

    Тип — код (str) для обычного файла или кортеж кодов для общего файла
    («Подп_АГ+ДКП+Акт_…»).

    (code, None)  — тип надёжно определён по имени, можно грузить без вопроса;
    (None, None)  — по имени тип не определить, спрашиваем кнопками;
    (None, текст) — имя указывает на другую сделку, спрашиваем и предупреждаем.
    """
    from agent import scan_codes_of, scan_codes_sorted  # отложенно: agent тяжёлый

    codes = scan_codes_of(filename)
    if not codes or not codes <= set(allowed_codes):
        return None, None
    nums = contract_numbers_in(filename)
    if nums and str(deal_num) not in nums:
        other = ", ".join(sorted(nums))
        return None, f"в имени файла номер {other}, а загружаем в сделку {deal_num}"
    if len(codes) == 1:
        return next(iter(codes)), None
    return tuple(scan_codes_sorted(codes)), None


def images_to_pdf(paths: list, out_path: str, max_side: int = 2200,
                  quality: int = 85) -> str:
    """Склеивает изображения в один PDF (по странице на картинку)."""
    from PIL import Image, ImageOps

    pages = []
    for p in paths:
        with Image.open(p) as im:
            im = ImageOps.exif_transpose(im)      # поворот с телефона
            if im.mode != "RGB":
                im = im.convert("RGB")
            if max(im.size) > max_side:
                im.thumbnail((max_side, max_side))
            pages.append(im.copy())
    if not pages:
        raise ValueError("нет изображений")
    pages[0].save(out_path, "PDF", save_all=True, append_images=pages[1:],
                  resolution=150.0, quality=quality)
    return out_path


def deal_summary(deal: dict, num: str) -> str:
    """Короткая сводка «сделка найдена» для проверки номера человеком.

    Обычный текст, без Markdown: ФИО и модель машины могут содержать «_», «*»
    и ломали бы разметку."""
    from agent import _calc_total_amount, _fmt_money  # отложенно: agent тяжёлый

    deal = deal or {}
    buyer = deal.get("buyer_name") or "—"
    car   = deal.get("car_model") or "—"
    vin   = deal.get("car_vin") or "—"
    cur   = (deal.get("currency") or "руб").strip()
    total = _calc_total_amount(deal)
    total_s = f"{_fmt_money(total)} {cur}" if total else "—"
    return "\n".join([
        f"✅ Сделка найдена: {num}",
        f"👤 Покупатель: {buyer}",
        f"🚗 {car} · VIN {vin}",
        f"💰 Сумма договора: {total_s}",
    ])
