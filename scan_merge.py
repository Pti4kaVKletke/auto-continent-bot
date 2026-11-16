"""
scan_merge.py — единый PDF из сканов сделки: «Комплект_<номер>.pdf».

Отдельные сканы остаются источником правды (по ним считается статус «Сканы»),
единый файл — производный: бот пересобирает его по кнопке и ЗАМЕНЯЕТ прежний.
Лежит в той же папке «Сканы»; статус его не считает (agent.is_merged_scan).

Здесь только чистые функции — без Drive и Telegram, чтобы их можно было тестировать.
"""

import io

# Порядок страниц в едином файле. Комплект «Скан_АД» (уже всё внутри) — первым.
MERGE_ORDER = ("bundle", "ag", "spec", "dkp", "receipt", "act", "report")

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")


def _is_pdf(f: dict) -> bool:
    return f.get("mimeType") == "application/pdf" or f.get("name", "").lower().endswith(".pdf")


def _is_image(f: dict) -> bool:
    return (f.get("mimeType", "").startswith("image/")
            or f.get("name", "").lower().endswith(_IMAGE_EXTS))


def plan_merge(files: list, kg: bool = False) -> dict:
    """Что войдёт в единый PDF и чего в комплекте не хватает.

    files — файлы папки «Сканы» ([{id, name, mimeType}]). Берутся подписанные
    документы (и общие файлы «Подп_АГ+ДКП+…»); платёжка, документы сторон,
    прочее и сам прежний единый файл не входят. Повторы (переснятый акт «…_2»)
    входят ВСЕ, рядом друг с другом: бот не знает, какой из них верный.
    """
    from agent import (REQUIRED_SCANS, REQUIRED_SCANS_KG, is_merged_scan,
                       scan_codes_of)

    items, skipped = [], []
    for f in files:
        name = f.get("name", "")
        if is_merged_scan(name):
            continue
        codes = scan_codes_of(name)
        idx = [MERGE_ORDER.index(c) for c in codes if c in MERGE_ORDER]
        if not idx or not (_is_pdf(f) or _is_image(f)):
            skipped.append(name)
            continue
        items.append((min(idx), name, f))
    items.sort(key=lambda t: (t[0], t[1]))

    found = set()
    for _, name, _f in items:
        found |= scan_codes_of(name)
    required = list(REQUIRED_SCANS_KG if kg else REQUIRED_SCANS)
    missing = [] if "bundle" in found else [c for c in required if c not in found]

    # Повторы среди одиночных файлов — чтобы предупредить человека
    count = {}
    for _, name, _f in items:
        codes = scan_codes_of(name)
        if len(codes) == 1:
            c = next(iter(codes))
            count[c] = count.get(c, 0) + 1
    dups = [c for c, n in count.items() if n > 1]

    return {"items": [f for _, _, f in items], "skipped": skipped, "found": found,
            "required": required, "missing": missing, "dups": dups}


def merged_name(num: str, plan: dict) -> str:
    """«Комплект_110826001.pdf» или «Комплект_110826001 (3 из 5, нет Расп ДКП).pdf»."""
    from agent import SCAN_TAGS

    if not plan["missing"]:
        return f"Комплект_{num}.pdf"
    total = len(plan["required"])
    have = total - len(plan["missing"])
    miss = " ".join(SCAN_TAGS.get(c, c) for c in plan["missing"])
    return f"Комплект_{num} ({have} из {total}, нет {miss}).pdf"


def _image_to_pdf_bytes(data: bytes) -> bytes:
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode != "RGB":
            im = im.convert("RGB")
        if max(im.size) > 2200:
            im.thumbnail((2200, 2200))
        buf = io.BytesIO()
        im.save(buf, "PDF", resolution=150.0)
        return buf.getvalue()


def merge_parts(parts: list, out_path: str) -> tuple:
    """Склеивает части [(имя, байты, это_картинка)] в один PDF.

    Возвращает (страниц, [имена, которые не удалось прочитать]).
    """
    try:
        import pymupdf as fitz
    except ImportError:  # старые версии
        import fitz

    out = fitz.open()
    bad = []
    for name, data, is_img in parts:
        try:
            if is_img:
                data = _image_to_pdf_bytes(data)
            src = fitz.open(stream=data, filetype="pdf")
            out.insert_pdf(src)
            src.close()
        except Exception:
            bad.append(name)
    pages = out.page_count
    if pages:
        out.save(out_path, garbage=3, deflate=True)
    out.close()
    return pages, bad
