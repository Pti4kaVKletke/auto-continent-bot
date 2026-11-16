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
    "bill":    "счёт на оплату (выставленный счёт, инвойс) — это НЕ платёжка",
    "invoice": "платёжка: платёжное поручение, чек или квитанция об оплате (подтверждение, что деньги уплачены)",
    "id":      "документы сторон: паспорт, удостоверение личности",
}

# Как человек может назвать документ в ручном вводе диапазонов
_ALIASES = {
    "ag": {"аг", "агдоговор", "агентский", "ад"},
    "dkp": {"дкп"},
    "receipt": {"расп", "расписка"},
    "report": {"отч", "отчет"},
    "act": {"акт"},
    "spec": {"спец", "спецификация"},
    "bill": {"счет", "сч", "инвойс"},
    "invoice": {"плат", "платежка"},
    "id": {"док", "документы"},
}
_ALIAS_TO_CODE = {a: c for c, names in _ALIASES.items() for a in names}


def _norm(word: str) -> str:
    return re.sub(r"[^а-яa-z0-9]", "", (word or "").lower().replace("ё", "е"))


def ranges_from_page_labels(labels, codes):
    """По списку «страница → код» получает {код: [(с, по)]} (страницы с 1).

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
        out[c] = [(pages[0], pages[-1])]
    return out


_WORD_RE = re.compile(r"[А-Яа-яЁёA-Za-z]+|\d+\s*[-–—]\s*\d+|\d+")
# «договор» и «поручение» — просто слова при названии («АГ договор 1-5»,
# «ДКП договор 7»): приложение «Поручение» — часть файла АГ.
_SKIP_WORDS = {"и", "стр", "страница", "страницы", "страниц", "с", "на", "а", "также",
               "договор", "поручение"}


def parse_ranges(text: str, codes, n_pages: int):
    """Разбор ручного ввода → ({код: [(с, по), …]}, ошибка).

    «АГ 1-5, ДКП 7, Плат 6, 8» и «АГ 1-5; ДКП 7; Плат 6 и 8» — одно и то же:
    названия документов начинают новую группу, числа после названия (через
    запятую или «и») относятся к нему. У документа может быть несколько
    диапазонов — когда его страницы идут не подряд."""
    codes = list(codes)
    out, cur, expect_pages = {}, None, False
    tokens = _WORD_RE.findall(text or "")
    if not tokens:
        return None, "Пусто. Пример: АГ 1-4, ДКП 5-6"
    for tok in tokens:
        if tok[0].isdigit():
            if cur is None:
                return None, f"Сначала название документа, потом страницы («{tok}»). Пример: АГ 1-4"
            m = re.match(r"^(\d+)(?:\s*[-–—]\s*(\d+))?$", tok)
            a = int(m.group(1))
            b = int(m.group(2) or a)
            if a < 1 or b < a or b > n_pages:
                return None, f"Страницы {a}–{b}: в файле {n_pages} стр."
            out[cur].append((a, b))
            expect_pages = False
            continue
        word = _norm(tok)
        if word in _SKIP_WORDS:
            continue
        code = _ALIAS_TO_CODE.get(word)
        if not code:
            return None, f"Не знаю документ «{tok}»"
        if code not in codes:
            return None, f"«{tok}» не отмечен среди документов этого файла"
        if code == cur and expect_pages:
            continue                      # «аг договор 1-5»: два слова одного названия
        cur, expect_pages = code, True
        out.setdefault(code, [])

    used = set()
    for code, rngs in out.items():
        if not rngs:
            return None, f"Для «{SCAN_TAG_HINT.get(code, code)}» не указаны страницы"
        for a, b in rngs:
            pages = set(range(a, b + 1))
            if pages & used:
                return None, f"Страницы {a}–{b} пересекаются с другим документом"
            used |= pages
    missing = [c for c in codes if c not in out]
    if missing:
        return None, "Не указаны страницы для: " + ", ".join(SCAN_TAG_HINT.get(c, c) for c in missing)
    return out, None


# Подписи для сообщений об ошибках ввода (метки, как в именах файлов)
SCAN_TAG_HINT = {"ag": "АГ", "dkp": "ДКП", "receipt": "Расп", "report": "Отч",
                 "act": "Акт", "spec": "Спец", "bill": "Счет", "invoice": "Плат",
                 "id": "Док"}


def format_ranges(rngs) -> str:
    """[(1, 5), (8, 8)] → «стр. 1–5, 8»."""
    parts = [str(a) if a == b else f"{a}–{b}" for a, b in rngs]
    return "стр. " + ", ".join(parts)


def prepare_source(pending: dict, out_path: str):
    """Готовит PDF-источник: фото (одно или несколько) → PDF, PDF берётся как есть.

    Возвращает (путь, число страниц)."""
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    from scan_upload import files_to_pdf, images_to_pdf, is_image_name

    filepath = pending["filepath"]
    if len(pending.get("pages") or []) > 1:
        src, _n = files_to_pdf(pending["pages"], out_path)   # фото и PDF вместе
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
        for code, rngs in ranges.items():
            part = fitz.open()
            for a, b in rngs:
                part.insert_pdf(doc, from_page=a - 1, to_page=b - 1)
            p = os.path.join(out_dir, f"part_{tag}_{code}.pdf")
            part.save(p, garbage=3, deflate=True)
            part.close()
            paths[code] = p
    return paths
