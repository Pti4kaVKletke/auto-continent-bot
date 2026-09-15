# -*- coding: utf-8 -*-
"""
scanify.py — прогоняет уже готовый PDF (напечатанный текст + вклеенные
через sign_jitter подпись/печать) через единый "скан"-фильтр, чтобы весь
лист выглядел одной фактурой — как один снимок со сканера, а не текст
с приклеенной сверху картинкой.

Зачем это отдельный шаг. sign_jitter и sign_pdf уже неплохо имитируют
САМИ подпись и печать (живые сканы, разброс, уникализация края) — но
после LibreOffice текст документа остаётся идеально ровным векторным
слоем, а подпись/печать — растровой картинкой рядом. Даже безупречно
состаренная печать всё равно резче/чище по DPI, чем буквы и линии
таблиц вокруг — глаз считывает "два слоя", не разбирая почему. Настоящий
скан устроен наоборот: там абсолютно всё — печатный текст наравне с
подписью — проходит через одну и ту же деградацию: оптику, шум матрицы,
лёгкое размытие, сжатие. Поэтому здесь ничего не "улучшается" точечно —
вся уже готовая страница целиком рендерится в растр и портится одним
проходом.

Детерминизм — тем же приёмом, что и sign_jitter: параметры сеются от
doc_key (`md5(doc_key) → random.Random`), поэтому пересборка того же
документа даёт визуально тот же результат. Отдельной записи в журнал
(колонка BI) не нужно — здесь нет расходных наборов картинок, только
числа, и они не "заканчиваются" и не меняются при правках кода — в
отличие от sign_jitter, спек в журнале тут не нужен для воспроизводимости.

Выключатель: SCANIFY=0.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import random

logger = logging.getLogger(__name__)

# ─── Параметры скан-эффекта ────────────────────────────────────────────
# Числа сознательно скромные: цель не "состарить" документ до неразборчивости,
# а убрать разницу в резкости между печатным текстом (вектор) и вклеенной
# подписью/печатью (растр из sign_jitter). Ориентир по разрешению — тот же
# профиль, которым в компании снимают контрольные листы (NAPS2, 300 dpi);
# здесь взято чуть ниже, 250 dpi, — так убирается "цифровая" резкость текста,
# не теряя читаемость мелкого шрифта в реквизитах.

RENDER_DPI          = 250
ROTATE_DEG          = (-0.35, 0.35)   # общий перекос листа в сканере
BLUR_RADIUS         = (0.35, 0.55)    # снимает векторную резкость текста
NOISE_SIGMA         = (3.0, 6.0)      # шум матрицы/бумаги (шкала 0-255)
BRIGHTNESS_JITTER   = (0.97, 1.03)    # общая яркость листа гуляет чуть-чуть
VIGNETTE_STRENGTH   = (0.0, 0.05)     # едва заметное затемнение к краю листа
JPEG_QUALITY        = (82, 90)        # пересжатие — эффект сканера/почты


def _rnd(doc_key: str) -> random.Random:
    seed = int(hashlib.md5(f"{doc_key}|scan".encode("utf-8")).hexdigest()[:12], 16)
    return random.Random(seed)


def _draw(rnd: random.Random) -> dict:
    """Один розыгрыш параметров на ВЕСЬ документ (не на страницу) —
    так и выглядит один сеанс сканирования: настройки сканера не
    меняются от листа к листу."""
    return {
        "rotate":     round(rnd.uniform(*ROTATE_DEG), 3),
        "blur":       round(rnd.uniform(*BLUR_RADIUS), 3),
        "noise":      round(rnd.uniform(*NOISE_SIGMA), 2),
        "brightness": round(rnd.uniform(*BRIGHTNESS_JITTER), 4),
        "vignette":   round(rnd.uniform(*VIGNETTE_STRENGTH), 4),
        "quality":    int(round(rnd.uniform(*JPEG_QUALITY))),
        # свой int-сид для numpy-шума страницы — чтобы каждая страница
        # получила разный, но воспроизводимый узор шума
        "noise_seed": rnd.randrange(2**31),
    }


def _scan_page(img, params: dict, page_index: int):
    """img — PIL.Image страницы (RGB). Возвращает обработанное PIL.Image."""
    from PIL import Image, ImageFilter
    import numpy as np

    img = img.convert("RGB")
    w, h = img.size

    # Лёгкий общий перекос листа. expand=False + белая подложка: угол
    # маленький, обрезка по краю незаметна, а вот "рамка" от expand=True
    # сразу выдала бы обработку.
    if abs(params["rotate"]) > 0.01:
        img = img.rotate(params["rotate"], resample=Image.BICUBIC,
                          fillcolor=(255, 255, 255))

    # Снимает "цифровую" резкость закруглений шрифта и линий таблиц —
    # именно она выдаёт вектор рядом с растровой подписью/печатью.
    img = img.filter(ImageFilter.GaussianBlur(params["blur"]))

    arr = np.asarray(img).astype(np.float32)

    # Общая яркость + едва заметная виньетка по краям — лист со сканера
    # почти никогда не выходит идеально ровным по свету.
    arr *= params["brightness"]
    if params["vignette"] > 0:
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        cx, cy = w / 2.0, h / 2.0
        r = np.sqrt(((xx - cx) / cx) ** 2 + ((yy - cy) / cy) ** 2)
        shade = 1.0 - params["vignette"] * np.clip(r - 0.6, 0, None)
        arr *= shade[..., None]

    # Шум матрицы/бумаги — ОДНА текстура сразу для текста, подписи и
    # печати; свой сид на страницу, чтобы страницы не повторяли узор.
    rng = np.random.default_rng(params["noise_seed"] + page_index)
    noise = rng.normal(0.0, params["noise"], size=arr.shape[:2])
    arr += noise[..., None]

    arr = np.clip(arr, 0, 255).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


def scanify_pdf(pdf_path: str, doc_key: str, out_path: str | None = None) -> str | None:
    """
    Рендерит каждую страницу pdf_path в растр, прогоняет через единый
    скан-фильтр (детерминированный от doc_key) и пересобирает PDF из
    полученных картинок — по странице на лист, тех же физических
    размеров, что у оригинала (важно для печати "в реальном размере").

    Возвращает путь к новому PDF (по умолчанию исходный путь — файл
    перезаписывается) или None при ошибке / если выключено SCANIFY=0.
    """
    if os.environ.get("SCANIFY", "1") == "0":
        return None

    try:
        import fitz  # pymupdf
        from PIL import Image
    except Exception as e:
        logger.warning(f"scanify: pymupdf/Pillow недоступны — {e}")
        return None

    out_path = out_path or pdf_path
    rnd = _rnd(doc_key)
    params = _draw(rnd)

    try:
        src = fitz.open(pdf_path)
        dst = fitz.open()
        zoom = RENDER_DPI / 72.0
        mat = fitz.Matrix(zoom, zoom)

        for i, page in enumerate(src):
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            img = _scan_page(img, params, i)

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=params["quality"],
                      optimize=True, subsampling=1)
            buf.seek(0)

            # Физический размер страницы — как у оригинала (в points),
            # чтобы печать "в реальном размере" продолжала совпадать
            # с бумагой так же, как выверено для подписи/печати.
            rect = page.rect
            new_page = dst.new_page(width=rect.width, height=rect.height)
            new_page.insert_image(new_page.rect, stream=buf.getvalue())

        dst.save(out_path, garbage=4, deflate=True)
        dst.close()
        src.close()
        logger.info(
            f"scanify: {doc_key} → dpi={RENDER_DPI} rot={params['rotate']} "
            f"blur={params['blur']} noise={params['noise']} q={params['quality']}"
        )
        return out_path
    except Exception as e:
        logger.error(f"scanify: не удалось обработать {pdf_path}: {e}", exc_info=True)
        return None
