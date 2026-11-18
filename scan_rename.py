"""
scan_rename.py — имена файлов, которые присылают при создании сделки.

Паспорт покупателя → «Паспорт_Фамилия_И.О.», ID-карта продавца →
«ID_Фамилия_И.О.», декларация на машину → «Декларация_XXXXXX» (последние 6
знаков VIN; под этим именем её потом находит разбор ИНН продавца).
Тип и владельца документа определяет Claude по первым страницам; если не
уверен — файл сохраняется под прежним именем.

Здесь чистые функции без Drive/Claude — их можно тестировать.
"""

import io
import os
import re

KINDS = ("passport", "declaration", "other")


def _clean(part: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "", part or "")


def initials_from_name(full_name: str) -> str:
    """«Абасов Магомед Магомедарипович» → «Абасов_М.М.»; пусто, если имени нет."""
    words = [w for w in re.split(r"\s+", (full_name or "").strip()) if w]
    if not words:
        return ""
    surname = _clean(words[0])
    ini = "".join(f"{_clean(w)[:1].upper()}." for w in words[1:3] if _clean(w))
    return f"{surname}_{ini}" if ini else surname


def person_tag(initials: str, full_name: str) -> str:
    """«Абасов М.М.» (из журнала) или ФИО → «Абасов_М.М.»."""
    src = (initials or "").strip()
    if src:
        parts = [p for p in re.split(r"\s+", src) if p]
        return "_".join(_clean(p) for p in parts if _clean(p))
    return initials_from_name(full_name)


def vin6(vin: str) -> str:
    v = re.sub(r"[^A-Za-z0-9]", "", str(vin or "")).upper()
    return v[-6:] if len(v) >= 6 else ""


def target_name(kind: str, owner: str, data: dict, ext: str):
    """Новое имя файла без проверки занятости или None (оставить прежнее)."""
    ext = ext or ".pdf"
    if kind == "declaration":
        v = vin6(data.get("car_vin"))
        return f"Декларация_{v}{ext}" if v else None
    if kind == "passport":
        if owner == "seller":
            tag = person_tag(data.get("seller_initials"), data.get("seller_name")).rstrip(".")
            return f"ID_{tag}{ext}" if tag else None
        if owner == "buyer":
            # Инициалы кончаются точкой, её роль берёт точка расширения:
            # «Паспорт_Абасов_М.М.pdf», а не «…М.М..pdf».
            tag = person_tag(data.get("buyer_initials"), data.get("buyer_name")).rstrip(".")
            return f"Паспорт_{tag}{ext}" if tag else None
    return None


def unique_name(name: str, taken: set) -> str:
    base, ext = os.path.splitext(name)
    out, n = name, 1
    while out in taken:
        n += 1
        out = f"{base}_{n}{ext}"
    return out


def classify_prompt(data: dict) -> str:
    return (
        "Определи, что за документ на этих страницах (первые страницы файла).\n"
        "kind: \"passport\" — паспорт или ID-карта (удостоверение личности физлица); "
        "\"declaration\" — таможенная декларация / ТПО на автомобиль; \"other\" — всё остальное "
        "(договор, счёт, СРТС, фото машины и т.д.).\n"
        "owner (только для passport): \"buyer\" — покупатель (заказчик), \"seller\" — продавец, "
        "\"unknown\" — не уверен. Сверяй ФИО в документе с известными:\n"
        f"покупатель: {data.get('buyer_name') or '—'}\n"
        f"продавец: {data.get('seller_name') or '—'}\n"
        "Если не уверен — kind \"other\". Верни ТОЛЬКО JSON: "
        "{\"kind\": \"...\", \"owner\": \"...\"}"
    )


def parse_classification(answer: str):
    """Ответ модели → (kind, owner); ('other', 'unknown') если не разобрать."""
    import json
    m = re.search(r"\{.*\}", answer or "", re.S)
    if not m:
        return "other", "unknown"
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return "other", "unknown"
    kind = obj.get("kind") if obj.get("kind") in KINDS else "other"
    owner = obj.get("owner") if obj.get("owner") in ("buyer", "seller") else "unknown"
    return kind, owner


def preview_jpegs(path: str, max_pages: int = 3, width: int = 1000) -> list:
    """Первые страницы файла как JPEG (PDF или фото) для определения документа."""
    low = path.lower()
    if low.endswith(".pdf"):
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
        out = []
        with fitz.open(path) as d:
            for i, page in enumerate(d):
                if i >= max_pages:
                    break
                zoom = width / max(page.rect.width, 1)
                out.append(page.get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("jpeg"))
        return out
    if low.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")):
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((width, width))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80)
            return [buf.getvalue()]
    return []


def clean_contract_number(raw: str) -> str:
    """Номер договора из ответа модели → безопасная для имени файла строка.
    Пусто, если номера нет («НЕТ», мусор, слишком длинно)."""
    t = (raw or "").strip().strip("\"'«».,;: ").replace("№", "").strip()
    if not t or t.upper() in ("НЕТ", "NONE", "NULL", "-", "—"):
        return ""
    t = re.sub(r"[\\/]+", "-", t)
    t = re.sub(r"[:*?\"<>|\s]+", "", t)
    return t if 1 <= len(t) <= 30 else ""
