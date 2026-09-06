"""
Разброс подписи и печати в документах.

Зачем: подпись и факсимиле печати вставлены прямо в шаблоны .docx как
плавающие картинки с жёсткими координатами. Без обработки во всех
документах компании они стоят пиксель в пиксель одинаково — это заметно
при сличении двух экземпляров и выглядит хуже, чем живой оттиск.

Модуль подмешивает в каждый документ:
  * сдвиг картинки по X и Y от базовой точки шаблона;
  * наклон (штатный атрибут a:xfrm/@rot, единица — 1/60000 градуса);
  * масштаб (у подписи; печать всегда одного размера);
  * «чернила» — прозрачность, лёгкое размытие и неравномерная
    непропечатка оттиска (через Pillow, картинка переписывается в part);
  * подпись — случайный росчерк из набора assets/signatures/*.png.

Какая картинка подпись, а какая печать, задаётся alt-текстом в самом
шаблоне (в Word: правой кнопкой по картинке → «Замещающий текст» → поле
«Описание»): ровно `signature` или ровно `stamp`. Картинки с любым другим
или пустым alt-текстом не трогаются вообще — логотипы и штрихкоды можно
оставлять плавающими, им ничего не будет.

ПОВТОРНАЯ СБОРКА ОДНОГО ДОКУМЕНТА ОБЯЗАНА ДАВАТЬ ТОТ ЖЕ ВИД. Иначе у
контрагента на руках и в архиве окажутся два внешне разных экземпляра
одного подписанного акта. Держится это двумя механизмами:

1. Розыгрыш идёт от seed, посчитанного по имени документа
   («Акт_050826001»), а не от времени.
2. Что именно выпало, `apply` возвращает строкой-спеком; вызывающий код
   пишет её в журнал (колонка «Штамп и подпись») и при следующей сборке
   передаёт обратно. Тогда ничего не разыгрывается — значения берутся
   из спека. Это защищает и от того, чего seed не переживает: пополнения
   набора подписей и правки коридоров в PROFILES.

Выключается переменной окружения SIGN_JITTER=0.
"""

import io
import os
import hashlib
import logging
import random
from pathlib import Path

logger = logging.getLogger(__name__)

ENABLED = os.environ.get("SIGN_JITTER", "1") != "0"

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

EMU_MM = 36000          # 1 мм в EMU
DEG = 60000             # 1 градус в единицах @rot

# Ближе этого к краю листа картинку не подпускаем. Коридоры разброса
# одни на все шаблоны, а базовые точки в них разные: в расписке печать
# стоит левее, чем в акте, и крайний сдвиг влево выносил её на 5 мм от
# края бумаги. Считается по полям секции, поэтому работает в любом шаблоне.
EDGE_MM = 12

# Коридоры разброса (мм).
#
# По горизонтали: печать ±20 мм, подпись ±5 мм — подпись должна остаться
# на своей линии и не налезать на фамилию рядом.
#
# По вертикали печать ходит на 5 мм вверх и 6 вниз: после перевода шаблонов
# на A4 (лист стал на 17,6 мм выше) под оттиском в акте осталось 9–15 мм до
# края страницы. Двигаете печать в шаблоне или меняете формат листа —
# проверьте этот зазор заново.
#
# Печать НЕ масштабируется (scale = 0): живой оттиск всегда одного размера,
# меняется только то, куда его приложили.
PROFILES = {
    "signature": {
        "dx_mm": (-5.0, 5.0), "dy_mm": (-1.6, 1.6),
        # Потолок ОБЩЕГО ухода от точки шаблона по вертикали, вместе с
        # поправкой на пропорции росчерка из набора: дальше подпись
        # проваливается под черту.
        "dy_limit_mm": 3.0,
        "rot_deg": 2.0,
        "scale": 0.035,
        "alpha": (0.86, 1.0),
        "blur": (0.0, 0.4),
        "mottle": 0.0,
    },
    "stamp": {
        "dx_mm": (-20.0, 20.0), "dy_mm": (-5.0, 6.0),
        # Живой оттиск ставится в НАСТОЯЩИЙ размер печати, а не в размер
        # рисованного факсимиле из шаблона: факсимиле рисовалось 38,8 мм,
        # реальная печать ~40 мм, и на бумаге разница видна.
        "ink_mm": 40.0,
        # ±30°: оттиск от руки ровно почти никогда не ложится. На габариты
        # это не влияет — сам оттиск круглый, поворачивается только кольцо
        # текста, поэтому зазор до края листа остаётся прежним.
        "rot_deg": 30.0,
        "scale": 0.0,
        "alpha": (0.78, 0.97),
        "blur": (0.0, 0.6),
        "mottle": 0.45,
    },
}

NO_FILE = "-"           # в спеке: взята картинка из шаблона, не из набора


def _q(ns, tag):
    return f"{{{ns}}}{tag}"


# ─── СПЕК: что выпало этому документу ──────────────────────────────────
# Одна строка на документ, лежит в журнале. Пример:
#   signature file=sig_17 dx=-3.240 dy=0.910 rot=1.420 k=1.02130 a=0.9340 b=0.120;
#   stamp file=- dx=12.440 dy=-2.100 rot=-18.400 k=1.00000 a=0.9120 b=0.310 m=734512
# Читается глазами, разбирается без библиотек, переживает копирование
# ячейки. Единицы: dx/dy — мм, rot — градусы, k — множитель размера,
# a — множитель непрозрачности, b — радиус размытия, m — seed непропечатки.

_FMT = {"dx": "%.3f", "dy": "%.3f", "rot": "%.3f", "k": "%.5f",
        "a": "%.4f", "b": "%.3f"}


def parse_spec(spec: str) -> dict:
    """Строка из журнала → {'stamp': {...}, 'signature': {...}}."""
    out = {}
    for chunk in (spec or "").split(";"):
        parts = chunk.split()
        if len(parts) < 2 or parts[0] not in PROFILES:
            continue
        d = {}
        for token in parts[1:]:
            if "=" not in token:
                continue
            k, v = token.split("=", 1)
            if k == "file":
                d[k] = v
            elif k == "m":
                d[k] = int(v)
            else:
                try:
                    d[k] = float(v)
                except ValueError:
                    pass
        out[parts[0]] = d
    return out


def format_spec(drawn: dict) -> str:
    """{'stamp': {...}, ...} → строка для журнала."""
    chunks = []
    for kind in ("signature", "stamp"):
        d = drawn.get(kind)
        if not d:
            continue
        tokens = [kind, f"file={d.get('file', NO_FILE)}"]
        for key in ("dx", "dy", "rot", "k", "a", "b"):
            if key in d:
                tokens.append(f"{key}=" + _FMT[key] % d[key])
        if "m" in d:
            tokens.append(f"m={d['m']}")
        chunks.append(" ".join(tokens))
    return "; ".join(chunks)


# Знаков после запятой при записи в журнал. Розыгрыш округляется сразу до
# этой точности, иначе первый экземпляр документа и пересобранный по спеку
# разошлись бы на доли микрона — невидимо, но байты файла уже не те.
_ROUND = {"dx": 3, "dy": 3, "rot": 3, "k": 5, "a": 4, "b": 3}


# ─── ЖУРНАЛЬНАЯ ЯЧЕЙКА: несколько документов одной сделки ──────────────
# В колонке «Штамп и подпись» лежат спеки всех документов сделки, по
# строке на документ: «Акт: signature ... ; stamp ...». Ключ — имя
# документа без номера сделки, то есть «Акт», «Расписка», «Отчёт_агента»,
# «Счёт». У каждого документа свой розыгрыш: их и подписывают по
# отдельности, в разные дни.


def ledger_key(doc_key: str) -> str:
    """«Отчёт_агента_050826001» → «Отчёт_агента»."""
    head = doc_key.rsplit("_", 1)
    return head[0] if len(head) == 2 and head[1].isdigit() else doc_key


def ledger_get(text: str, doc_key: str) -> str:
    """Спек нужного документа из журнальной ячейки."""
    key = ledger_key(doc_key)
    for line in (text or "").splitlines():
        name, _, spec = line.partition(":")
        if name.strip() == key:
            return spec.strip()
    return ""


def ledger_set(text: str, doc_key: str, spec: str) -> str:
    """Журнальная ячейка с обновлённой строкой этого документа.
    Строки других документов сохраняются как есть."""
    key = ledger_key(doc_key)
    lines = [l for l in (text or "").splitlines() if l.strip()]
    out, replaced = [], False
    for line in lines:
        name, _, _rest = line.partition(":")
        if name.strip() == key:
            out.append(f"{key}: {spec}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}: {spec}")
    return "\n".join(out)


def _draw(kind: str, rnd: random.Random, pool: list) -> dict:
    """Розыгрыш всех величин для одной картинки."""
    p = PROFILES[kind]
    d = {
        "file": rnd.choice(pool).name if pool else NO_FILE,
        "dx":  rnd.uniform(*p["dx_mm"]),
        "dy":  rnd.uniform(*p["dy_mm"]),
        "rot": rnd.uniform(-p["rot_deg"], p["rot_deg"]),
        "k":   1 + rnd.uniform(-p["scale"], p["scale"]) if p["scale"] else 1.0,
        "a":   rnd.uniform(*p["alpha"]),
        "b":   rnd.uniform(*p["blur"]),
    }
    for key, digits in _ROUND.items():
        d[key] = round(d[key], digits)
    if p["mottle"] > 0:
        d["m"] = rnd.randrange(1_000_000)
    return d


def classify(anchor) -> str | None:
    """Роль картинки по её alt-тексту: 'stamp', 'signature' или None.

    Alt-текст задаётся в шаблоне (в Word: «Замещающий текст» → «Описание»)
    и должен быть ровно `stamp` или `signature` — регистр и пробелы вокруг
    не важны. Всё остальное (логотипы, штрихкоды, картинка с пустым
    alt-текстом) не трогается. Ключ — имя профиля в PROFILES.
    """
    docpr = anchor.find(_q(WP, "docPr"))
    if docpr is None:
        return None
    for attr in ("descr", "title"):
        alt = (docpr.get(attr) or "").strip().lower()
        if alt in PROFILES:
            return alt
    return None


def _pool(kind: str) -> list:
    sub = "signatures" if kind == "signature" else "stamps"
    d = Path(__file__).parent / "assets" / sub
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.suffix.lower() in (".png", ".webp"))


def _pick(pool: list, name: str):
    """Файл набора по имени из спека. Нет такого — None: значит набор
    почистили после выдачи документа, и повторить его уже нечем."""
    for p in pool:
        if p.name == name:
            return p
    return None


def _smooth_noise(size, rnd, cells: int, blur: float):
    """Гладкий шум 0..1 нужной крупности — на нём держится и непропечатка
    факсимиле, и уникализация края живого оттиска."""
    from PIL import Image, ImageFilter
    w, h = size
    small = Image.new("L", (cells, cells))
    small.putdata([rnd.randrange(256) for _ in range(cells * cells)])
    img = small.resize((w, h), Image.BICUBIC)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    return img


def _uniquify(alpha, rnd):
    """Слабое шевеление края живого оттиска.

    Набор конечен, и один и тот же скан рано или поздно попадётся дважды.
    Здесь порог бинаризации гуляет вдоль контура на доли пикселя: сам
    оттиск остаётся собой, но два документа с одной базой не совпадают
    при наложении (расходятся примерно по 2% площади). Заменять этим
    живую фактуру нельзя — только шевелить: амплитуда вдвое слабее той,
    что понадобилась бы нарисованному факсимиле.
    """
    from PIL import Image, ImageChops, ImageFilter
    soft = alpha.filter(ImageFilter.GaussianBlur(0.7))
    n = _smooth_noise(alpha.size, rnd, 220, 0.6)
    a = np_from(soft); noise = np_from(n)
    out = (a - (128 + (noise / 255.0 * 2 - 1) * 34)) * 1.5 + 140
    return np_to(out, alpha.size)


def np_from(img):
    import numpy as np
    return np.asarray(img).astype("float32")


def np_to(arr, size):
    import numpy as np
    from PIL import Image
    return Image.fromarray(np.clip(arr, 0, 255).astype("uint8"))


def _mul(a, b):
    from PIL import ImageChops
    return ImageChops.multiply(a, b)


def _ink(blob: bytes, kind: str, d: dict, from_pool: bool = False) -> bytes | None:
    """Прозрачность, размытие и непропечатка по разыгранным значениям.
    None — если Pillow нет или картинка не поддалась: тогда остаётся
    оригинал."""
    try:
        from PIL import Image, ImageFilter
    except Exception:
        return None

    p = PROFILES[kind]
    try:
        im = Image.open(io.BytesIO(blob))
        im.load()
        if im.mode != "RGBA":
            im = im.convert("RGBA")
        alpha = im.getchannel("A")

        blur = float(d.get("b", 0))
        if blur > 0.05:
            alpha = alpha.filter(ImageFilter.GaussianBlur(blur))

        # Поле «m» — seed фактуры чернил. Что именно оно задаёт, зависит от
        # источника картинки: у живого оттиска из набора уже есть своя
        # неровность, ему нужна только уникализация края; рисованному
        # факсимиле нужна имитация непропечатки.
        if "m" in d:
            mr = random.Random(int(d["m"]))
            if from_pool:
                alpha = _uniquify(alpha, mr)
            elif p["mottle"] > 0:
                # Низкочастотный шум: мелкая случайная картинка, растянутая
                # на весь оттиск. Даёт пятна непропечатки, а не «снег».
                w, h = im.size
                small = Image.new("L", (mr.randint(5, 9), mr.randint(4, 7)))
                lo = int(255 * (1 - p["mottle"]))
                small.putdata([mr.randint(lo, 255) for _ in range(small.width * small.height)])
                alpha = _mul(alpha, small.resize((w, h), Image.BICUBIC))

        a_factor = float(d.get("a", 1.0))
        if a_factor < 0.999:
            alpha = alpha.point(lambda v: int(v * a_factor))

        im.putalpha(alpha)
        out = io.BytesIO()
        im.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception as e:
        logger.warning(f"sign_jitter: не удалось обработать картинку ({kind}): {e}")
        return None


def _ink_box(blob: bytes):
    """Где внутри картинки лежат сами чернила — в долях от её размера:
    (left, top, right, bottom, ширина, высота, отношение сторон картинки).

    Нужно, чтобы вариант из набора встал ровно туда же, где стоит росчерк
    шаблонной картинки: поля вокруг чернил у скана свои, а у шаблона свои,
    и если равнять рамки, а не чернила, подпись уезжает от своей линии.
    """
    try:
        from PIL import Image
        with Image.open(io.BytesIO(blob)) as im:
            im = im.convert("RGBA")
            w, h = im.size
            bb = im.getchannel("A").point(lambda v: 255 if v > 20 else 0).getbbox()
        if not bb or not w or not h:
            return None
        iw, ih = bb[2] - bb[0], bb[3] - bb[1]
        if iw <= 0 or ih <= 0:
            return None
        return {"l": bb[0] / w, "t": bb[1] / h, "r": bb[2] / w, "b": bb[3] / h,
                "w": iw / w, "h": ih / h, "ar": w / h}
    except Exception:
        return None


def _fit(tpl_blob: bytes, new_blob: bytes, cx: int, cy: int, kind: str = ""):
    """Рамка и поправка к позиции для картинки из набора.

    Высота росчерка берётся из профиля (`ink_mm`, настоящий размер печати)
    либо приравнивается к высоте росчерка из шаблона: подписи
    у человека разной ширины, но примерно одной высоты, и это тот размер,
    которым нельзя рисковать — раздутая по высоте подпись налезет на
    строку выше. Ширина следует из пропорций скана.

    Дальше картинка сдвигается так, чтобы САМИ ЧЕРНИЛА встали туда, где
    стояли чернила шаблона: по горизонтали совмещаются их середины, по
    вертикали — нижние края (подпись «лежит» на своей линии). Равнять по
    краям рамки нельзя: у скана поля вокруг росчерка другие, и подпись
    уходит вниз под черту.

    Возвращает (ncx, ncy, dx, dy).
    """
    t, n = _ink_box(tpl_blob), _ink_box(new_blob)
    if not t or not n:
        return None
    ink_mm = PROFILES.get(kind, {}).get("ink_mm")
    ink_h = ink_mm * EMU_MM if ink_mm else cy * t["h"]
    ncy = int(ink_h / n["h"])
    ncx = int(ncy * n["ar"])
    if ncx <= 0 or ncy <= 0:
        return None
    dx = int(cx * (t["l"] + t["r"]) / 2 - ncx * (n["l"] + n["r"]) / 2)
    dy = int(cy * t["b"] - ncy * n["b"])
    return ncx, ncy, dx, dy


def _h_limits(doc):
    """(min, max) для posOffset по горизонтали — чтобы картинка не вылезла
    к краю листа. Отсчёт от левого поля: positionH в шаблонах привязан
    к колонке. Не получилось прочитать секцию — ограничений нет."""
    try:
        sec = doc.sections[0]
        left = int(sec.left_margin)
        edge = EDGE_MM * EMU_MM
        return edge - left, int(sec.page_width) - edge - left
    except Exception:
        return None


def apply(doc, doc_key: str, spec: str | None = None) -> str:
    """Разбрасывает подпись и печать в документе.

    doc      — открытый python-docx Document (до save);
    doc_key  — стабильный ключ документа, например «Акт_050826001»;
    spec     — строка из журнала: что выпало при первой сборке. Передана —
               значения берутся из неё, ничего не разыгрывается.

    Возвращает спек (ту же строку, если она была передана и подошла) —
    его нужно сохранить в журнал при первой сборке документа.
    """
    if not ENABLED:
        return spec or ""

    saved = parse_spec(spec)
    rnd = random.Random(int(hashlib.md5(doc_key.encode("utf-8")).hexdigest()[:12], 16))
    drawn = {}
    limits = _h_limits(doc)

    for anchor in doc.element.body.iter(_q(WP, "anchor")):
        ext = anchor.find(_q(WP, "extent"))
        if ext is None:
            continue
        try:
            cx, cy = int(ext.get("cx")), int(ext.get("cy"))
        except (TypeError, ValueError):
            continue

        kind = classify(anchor)
        if kind is None:      # не подпись и не печать — не наше дело
            continue

        pool = _pool(kind)
        d = saved.get(kind) or _draw(kind, rnd, pool)
        drawn[kind] = d

        # ── картинка: вариант из набора + чернила ──────────────────────
        # Делается первой: у варианта из набора свои пропорции и свои поля
        # вокруг росчерка, поэтому рамку и точку привязки надо пересчитать
        # до сдвига и масштаба.
        base_dx = base_dy = 0
        blip = next(iter(anchor.iter(_q(A, "blip"))), None)
        if blip is not None:
            rid = blip.get(_q(R, "embed"))
            part = doc.part.related_parts.get(rid) if rid else None
            if part is not None:
                blob = part.blob
                name = d.get("file", NO_FILE)
                path = _pick(pool, name) if name != NO_FILE else None
                if name != NO_FILE and path is None:
                    logger.warning(
                        f"sign_jitter: {doc_key} — файла {name} нет в наборе "
                        f"{kind}, документ соберётся с картинкой из шаблона"
                    )
                if path is not None:
                    variant = path.read_bytes()
                    fit = _fit(blob, variant, cx, cy, kind)
                    if fit:
                        cx, cy, base_dx, base_dy = fit
                    blob = variant
                part._blob = _ink(blob, kind, d, path is not None) or blob

        # ── сдвиг ──────────────────────────────────────────────────────
        ncx0 = int(cx * float(d["k"]))      # ширина после масштаба — для
                                            # проверки правого края листа
        base = {"dx": base_dx, "dy": base_dy}
        for tag, key in ((_q(WP, "positionH"), "dx"), (_q(WP, "positionV"), "dy")):
            pos = anchor.find(tag)
            if pos is None:
                continue
            off = pos.find(_q(WP, "posOffset"))
            if off is None or not off.text:
                continue
            try:
                pos0 = int(off.text)
            except ValueError:
                continue
            # отрицательный posOffset допустим: картинка уходит выше/левее
            # точки привязки, как и живой оттиск, приложенный не туда
            shift = base[key] + int(float(d[key]) * EMU_MM)
            lim = PROFILES[kind].get(f"{key}_limit_mm")
            if lim:
                shift = max(-int(lim * EMU_MM), min(shift, int(lim * EMU_MM)))
            val = pos0 + shift
            if key == "dx" and limits:
                lo_lim, hi_lim = limits
                val = max(lo_lim, min(val, hi_lim - ncx0))
            off.text = str(val)

        # ── масштаб (у печати отключён: scale = 0) ─────────────────────
        ncx, ncy = int(cx * float(d["k"])), int(cy * float(d["k"]))
        ext.set("cx", str(ncx))
        ext.set("cy", str(ncy))

        # ── наклон + тот же размер внутри графики ──────────────────────
        for xfrm in anchor.iter(_q(A, "xfrm")):
            xfrm.set("rot", str(int(float(d["rot"]) * DEG) % (360 * DEG)))
            aext = xfrm.find(_q(A, "ext"))
            if aext is not None:
                aext.set("cx", str(ncx))
                aext.set("cy", str(ncy))

    if not drawn:
        return spec or ""
    out = format_spec(drawn)
    logger.info(f"sign_jitter: {doc_key} — {out}"
                + ("" if saved else " (розыгрыш, спек нужно сохранить)"))
    return out


# ─── XLSX: счёт ────────────────────────────────────────────────────────
# Счёт собирается openpyxl, картинки в нём сидят на «одноклеточном» якоре
# (xdr:oneCellAnchor: колонка, строка и смещение в EMU). Поворота openpyxl
# не умеет вовсе, поэтому файл правится после сохранения — прямо в его
# XML, как и .docx. Alt-текст задаётся в Excel так же: правой кнопкой по
# картинке → «Замещающий текст» → `stamp` или `signature`.

XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
EMU_PX = 9525


def _col_widths_emu(ws) -> dict:
    """Ширины колонок в EMU. Excel держит их в символах шрифта:
    px = width * 7 + 5 (та же формула, что в doc_builder._anchor_box_px)."""
    w = {}
    for dim in ws.column_dimensions.values():
        if dim.width is None:
            continue
        for i in range(dim.min or 1, (dim.max or dim.min or 1) + 1):
            w[i] = int((dim.width * 7 + 5) * EMU_PX)
    return w


def _shift_anchor(idx: int, off: int, delta: int, size_of) -> tuple:
    """Сдвиг якоря на delta EMU. Смещение внутри клетки не может быть
    отрицательным, поэтому при уходе влево/вверх переходим на клетку
    назад и добираем её размер."""
    off += delta
    while off < 0 and idx > 0:
        idx -= 1
        off += size_of(idx + 1)     # size_of ждёт 1-based номер
    return idx, max(0, off)


def apply_xlsx(path, doc_key: str, spec: str | None = None) -> str:
    """То же, что apply(), но для готового .xlsx (счёт).

    path — файл на диске, правится на месте. Возвращает спек.
    """
    if not ENABLED:
        return spec or ""

    import re
    import shutil
    import zipfile
    import openpyxl

    path = str(path)
    saved = parse_spec(spec)
    rnd = random.Random(int(hashlib.md5(doc_key.encode("utf-8")).hexdigest()[:12], 16))
    drawn = {}

    try:
        ws = openpyxl.load_workbook(path).active
        widths = _col_widths_emu(ws)
        def_w = int(((ws.sheet_format.defaultColWidth or 8.43) * 7 + 5) * EMU_PX)
        def_h = (ws.sheet_format.defaultRowHeight or 15.0)
        col_size = lambda i: widths.get(i, def_w)
        row_size = lambda i: int((ws.row_dimensions[i].height or def_h) * 96 / 72 * EMU_PX)

        zin = zipfile.ZipFile(path)
        names = zin.namelist()
        parts = {n: zin.read(n) for n in names}
        zin.close()

        for dname in [n for n in names if re.match(r"xl/drawings/drawing\d+\.xml$", n)]:
            xml = parts[dname].decode("utf-8")
            rels_name = dname.replace("drawings/", "drawings/_rels/") + ".rels"
            rels = parts.get(rels_name, b"").decode("utf-8")
            rid_to_media = dict(re.findall(r'Id="([^"]+)"[^>]*?Target="([^"]+)"', rels))

            def fix(m):
                block = m.group(0)
                alt = re.search(r'<xdr:cNvPr[^>]*?descr="([^"]*)"', block)
                kind = (alt.group(1).strip().lower() if alt else "")
                if kind not in PROFILES:
                    return block

                pool = _pool(kind)
                d = saved.get(kind) or _draw(kind, rnd, pool)
                drawn[kind] = d
                fit_ext = fit_off = None

                # чернила и вариант из набора
                rid = re.search(r'r:embed="([^"]+)"', block)
                if rid:
                    target = rid_to_media.get(rid.group(1), "")
                    media = "xl/" + target.replace("../", "")
                    if media in parts:
                        blob = parts[media]
                        name = d.get("file", NO_FILE)
                        pick = _pick(pool, name) if name != NO_FILE else None
                        if name != NO_FILE and pick is None:
                            logger.warning(
                                f"sign_jitter: {doc_key} — файла {name} нет в наборе "
                                f"{kind}, счёт соберётся с картинкой из шаблона"
                            )
                        if pick is not None:
                            variant = pick.read_bytes()
                            e = re.search(r'<xdr:ext cx="(\d+)" cy="(\d+)"/>', block)
                            if e:
                                fit = _fit(blob, variant,
                                           int(e.group(1)), int(e.group(2)), kind)
                                if fit:
                                    fit_ext, fit_off = fit[:2], fit[2:]
                            blob = variant
                        parts[media] = _ink(blob, kind, d, pick is not None) or blob

                # размер
                ext = re.search(r'<xdr:ext cx="(\d+)" cy="(\d+)"/>', block)
                if ext:
                    cx, cy = int(ext.group(1)), int(ext.group(2))
                    if fit_ext:
                        cx, cy = fit_ext
                    ncx, ncy = int(cx * float(d["k"])), int(cy * float(d["k"]))
                    block = block.replace(ext.group(0), f'<xdr:ext cx="{ncx}" cy="{ncy}"/>')
                    block = re.sub(r'(<a:ext cx=")\d+(" cy=")\d+(")',
                                   rf'\g<1>{ncx}\g<2>{ncy}\g<3>', block)

                # положение
                fr = re.search(
                    r"<xdr:from><xdr:col>(\d+)</xdr:col><xdr:colOff>(-?\d+)</xdr:colOff>"
                    r"<xdr:row>(\d+)</xdr:row><xdr:rowOff>(-?\d+)</xdr:rowOff></xdr:from>",
                    block)
                if fr:
                    col, coff, row, roff = (int(g) for g in fr.groups())
                    bdx, bdy = fit_off if fit_off else (0, 0)
                    sx = bdx + int(float(d["dx"]) * EMU_MM)
                    sy = bdy + int(float(d["dy"]) * EMU_MM)
                    lim = PROFILES[kind].get("dy_limit_mm")
                    if lim:
                        sy = max(-int(lim * EMU_MM), min(sy, int(lim * EMU_MM)))
                    col, coff = _shift_anchor(col, coff, sx, col_size)
                    row, roff = _shift_anchor(row, roff, sy, row_size)
                    block = block.replace(
                        fr.group(0),
                        f"<xdr:from><xdr:col>{col}</xdr:col><xdr:colOff>{coff}</xdr:colOff>"
                        f"<xdr:row>{row}</xdr:row><xdr:rowOff>{roff}</xdr:rowOff></xdr:from>")

                # наклон
                rot = str(int(float(d["rot"]) * DEG) % (360 * DEG))
                if "<a:xfrm>" in block:
                    block = block.replace("<a:xfrm>", f'<a:xfrm rot="{rot}">', 1)
                else:
                    block = re.sub(r'<a:xfrm[^>]*>', f'<a:xfrm rot="{rot}">', block, count=1)
                return block

            xml = re.sub(r"<xdr:(oneCellAnchor|twoCellAnchor|absoluteAnchor).*?</xdr:\1>",
                         fix, xml, flags=re.S)
            parts[dname] = xml.encode("utf-8")

        if not drawn:
            return spec or ""

        tmp = path + ".jitter"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for n in names:
                zout.writestr(n, parts[n])
        shutil.move(tmp, path)
    except Exception as e:
        logger.warning(f"sign_jitter: счёт {doc_key} без разброса — {e}")
        return spec or ""

    out = format_spec(drawn)
    logger.info(f"sign_jitter: {doc_key} — {out}"
                + ("" if saved else " (розыгрыш, спек нужно сохранить)"))
    return out
