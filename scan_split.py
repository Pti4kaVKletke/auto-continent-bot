"""
scan_split.py — разделение общего PDF на отдельные сканы по страницам.

Клиент присылает один файл (АГ + ДКП + расписка…). Бот определяет, на каких
страницах какой документ (Claude смотрит на заголовки), показывает разбивку, и
после подтверждения сохраняет КАЖДЫЙ документ отдельным файлом под обычным
именем. «АГ договор» и приложение «Поручение» — один документ (один файл).

Здесь только чистые функции без Telegram/Drive/Claude — их можно тестировать.
"""

import os
import re

# Как описывать документ модели, определяющей страницы
SPLIT_DESC = {
    "ag":      "агентский договор вместе с приложением «Поручение» (это ОДИН документ)",
    "dkp":     "договор купли-продажи транспортного средства (ДКП)",
    "receipt": "расписка о получении денежных средств",
    "report":  "отчёт агента",
    "act":     "акт (приёма-передачи / оказанных услуг)",
    "spec":    "спецификация (приложение № 2)",
    "invoice": "платёжка: платёжное поручение, чек или квитанция об оплате",
    "id":      "документы сторон: паспорт, удостоверение личности",
}

# Как человек может назвать документ в ручном вводе диапазонов
_ALIASES = {
    "ag": {"аг", "агдоговор", "агентский", "договор", "ад"},
    "dkp": {"дкп"},
    "receipt": {"расп", "расписка"},
    "report": {"отч", "отчет"},
    "act": {"акт"},
    "spec": {"спец", "спецификация"},
    "invoice": {"плат", "платежка"},
    "id": {"док", "документы"},
}
_ALIAS_TO_CODE = {a: c for c, names in _ALIASES.items() for a in names}


def _norm(word: str) -> str:
    return re.sub(r"[^а-яa-z0-9]", "", (word or "").lower().replace("ё", "е"))


def ranges_from_page_labels(labels, codes):
    """По списку «страница → код» получает {код: (с, по)} (страницы с 1).

    None — если разметка неправдоподобна: документ не найден, его страницы идут
    не подряд или встретился код, которого не отмечали. Тогда надёжнее спросить
    человека, чем угадывать."""
    codes = list(codes)
    if not labels:
        return None
    pos = {}
    for i, lab in enumerate(labels, 1):
        if lab == "other":
            continue
        if lab not in codes:
            return None
        pos.setdefault(lab, []).append(i)
    out = {}
    for c in codes:
        pages = pos.get(c)
        if not pages or pages != list(range(pages[0], pages[-1] + 1)):
            return None
        out[c] = (pages[0], pages[-1])
    return out


def parse_ranges(text: str, codes, n_pages: int):
    """Разбор ручного ввода «АГ 1-4, ДКП 5-6, Расп 7» → ({код: (с, по)}, ошибка)."""
    codes = list(codes)
    segs = [s.strip() for s in re.split(r"[;,\n]+", text or "") if s.strip()]
    if not segs:
        return None, "Пусто. Пример: АГ 1-4, ДКП 5-6"
    out, used = {}, set()
    for seg in segs:
        m = re.match(r"^(\S+)\s+(\d+)(?:\s*[-–—]\s*(\d+))?\s*$", seg)
        if not m:
            return None, f"Не понял «{seg}». Формат: АГ 1-4"
        code = _ALIAS_TO_CODE.get(_norm(m.group(1)))
        if not code:
            return None, f"Не знаю документ «{m.group(1)}»"
        if code not in codes:
            return None, f"«{m.group(1)}» не отмечен среди документов этого файла"
        if code in out:
            return None, f"Документ «{m.group(1)}» указан дважды"
        a = int(m.group(2))
        b = int(m.group(3) or a)
        if a < 1 or b < a or b > n_pages:
            return None, f"Страницы {a}–{b}: в файле {n_pages} стр."
        pages = set(range(a, b + 1))
        if pages & used:
            return None, f"Страницы {a}–{b} пересекаются с другим документом"
        used |= pages
        out[code] = (a, b)
    missing = [c for c in codes if c not in out]
    if missing:
        return None, "Не указаны страницы для: " + ", ".join(missing)
    return out, None


def format_range(rng) -> str:
    a, b = rng
    return f"стр. {a}" if a == b else f"стр. {a}–{b}"


def prepare_source(pending: dict, out_path: str):
    """Готовит PDF-источник: фото (одно или несколько) → PDF, PDF берётся как есть.

    Возвращает (путь, число страниц)."""
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    from scan_upload import images_to_pdf, is_image_name

    filepath = pending["filepath"]
    if pending.get("pages"):
        src = images_to_pdf(pending["pages"], out_path)
    elif is_image_name(pending["filename"]):
        src = images_to_pdf([filepath], out_path)
    elif filepath.lower().endswith(".pdf"):
        src = filepath
    else:
        raise ValueError("не PDF и не фото")
    with fitz.open(src) as d:
        return src, d.page_count


def render_pages_jpeg(pdf_path: str, max_pages: int = 30, width: int = 800):
    """Страницы PDF как маленькие JPEG для определения документа. None — страниц слишком много."""
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    out = []
    with fitz.open(pdf_path) as d:
        if d.page_count > max_pages:
            return None
        for page in d:
            zoom = width / max(page.rect.width, 1)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            out.append(pix.tobytes("jpeg"))
    return out


def split_pdf(src: str, ranges: dict, out_dir: str, tag: str = "") -> dict:
    """Режет PDF по диапазонам: {код: путь к файлу}."""
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    paths = {}
    with fitz.open(src) as doc:
        for code, (a, b) in ranges.items():
            part = fitz.open()
            part.insert_pdf(doc, from_page=a - 1, to_page=b - 1)
            p = os.path.join(out_dir, f"part_{tag}_{code}.pdf")
            part.save(p, garbage=3, deflate=True)
            part.close()
            paths[code] = p
    return paths
