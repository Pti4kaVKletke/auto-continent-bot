"""sign_pdf.py — подпись ЧУЖИХ документов: печать и подпись в готовый PDF.

Чем отличается от `sign_jitter`. Там документ собирает сам бот: в шаблоне
уже лежат картинки с alt-текстом `stamp`/`signature`, и всё, что нужно, —
подменить их живыми сканами и слегка развести. Здесь документ приходит от
контрагента: ни шаблона, ни разметки, ни даже гарантии, что подпись вообще
предусмотрена там, где мы думаем. Поэтому модуль делает два дела:

1. НАХОДИТ места подписи по тексту и линиям страницы (`find_slots`);
2. КЛАДЁТ туда наш росчерк и оттиск (`sign`), тем же розыгрышем и тем же
   форматом спека, что и `sign_jitter` — чернила, наборы, seed и запись
   в журнал общие, здесь только своя геометрия.

Вход всегда PDF. .docx/.xlsx прогоняются через LibreOffice до вызова
(`doc_builder.convert_to_pdf`): координаты всё равно берутся из готовой
вёрстки, а не из разметки Word.

ВАЖНО: автоопределение не считается достаточным основанием, чтобы выдать
подписанный файл. Разметка договоров у контрагентов произвольная, а цена
ошибки — наш оттиск поперёк чужих реквизитов. Штатный порядок: `find_slots`
→ `render_page` с рамкой предполагаемого места → человек подтверждает или
двигает (`nudge`) → `sign`.
"""
import hashlib
import io
import logging
import math
import random
import re

import sign_jitter as sj

logger = logging.getLogger(__name__)


def _fitz():
    """PyMuPDF. Импортируется внутри функций: без него модуль всё равно
    должен подгружаться (бот стартует, просто подпись чужих PDF не работает)."""
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        import fitz
        return fitz


PT_MM = 72.0 / 25.4          # 1 мм в пунктах PDF

# Высота ЧЕРНИЛ, а не рамки картинки. 16,5 мм — замер по образцу, который
# Илья подписал от руки 07.09.2026 (единственный неперекрытый печатью
# росчерк вышел 30×17,1 мм). В шаблонах росчерк меньше — 25,6×12,6 мм, — но
# там он рисованный и сидит на своей строке; на чужом документе живая
# подпись крупнее. Печать берёт свой размер из PROFILES['stamp']['ink_mm']
# = 40 мм: замер того же образца дал внешний диаметр 39,7 мм.
SIG_INK_MM = 16.5

# Где стоит подпись относительно найденной линии. По образцу низ чернил
# заходит на 2 мм НИЖЕ линии: единственный неперекрытый печатью росчерк дал
# +2,2 мм, два других (−3,2 и −2,3) занижены — их низ скрыт оттиском.
# Проверка обратной сборкой: при 0,5 мм бот ставил росчерк на 3 мм выше,
# чем от руки. Итоговые 4 мм — на 2 мм ниже замера: Илья посмотрел на
# готовый результат и сказал, что свою роспись поставил бы ещё ниже (на
# бумаге низ росчерка частично уходит под саму линию, и в замере это не
# видно). Середина чернил — на середине линии плюс пара мм вправо;
# росчерк шире линии и свешивается с правого конца на 4–6 мм, так и надо.
SIG_BELOW_MM = 4.0
SIG_RIGHT_MM = 2.0

# Основной случай: рядом нашлась метка «М.П.» — печать ставится ПО НЕЙ.
# Замеры образца от 07.09.2026 (три расклада): центр оттиска относительно
# ЦЕНТРА метки — по горизонтали +0,1 / +14,0 / +0,2 мм, по вертикали
# +7,2 / −4,4 / +1,3 мм. То есть в норме печать садится ровно на метку, а
# разброс живой руки примерно ±10 мм вбок и −5…+7 мм по высоте.
#
# База смещена на +4 мм вправо и +1 мм вниз НЕ случайно: коридоры розыгрыша
# в sign_jitter несимметричны (dx от −14 до +6, dy от −5 до +6), и такая
# база превращает их в примерно симметричные ±10 и −4…+7 вокруг метки —
# ровно то, что намерено по образцу.
STAMP_MP_DX_MM = 4.0
STAMP_MP_DY_MM = 1.0
# Коридор для центра оттиска вокруг центра метки.
MP_SPAN_MM = 16.0

# Запасной случай: метки «М.П.» в документе нет, пляшем от линии подписи.
# По тем же замерам центр оттиска лежал на 15,5 и 11,1 мм ниже линии и на
# четверти её длины от левого конца.
STAMP_BELOW_MM = 12.0
STAMP_AT_LINE = 0.25

# Насколько картинке позволено вылезать за границы своей колонки. Печать
# 40 мм шире типовой линии подписи (20–45 мм), так что свисать она будет
# всегда; допуск подобран так, чтобы у розыгрыша остался ход, а оттиск не
# заезжал в соседний блок реквизитов.
OVER_MM = 10.0

MIN_LINE_MM = 12.0           # короче — это не линия для подписи, а рамка ячейки
MAX_LINE_MM = 110.0

# ─── Кто здесь мы ───────────────────────────────────────────────────────
# Своя колонка реквизитов ищется по этим строкам. Значения подставляет
# вызывающий из карточки компании (company.py): наименование, оба ИНН,
# фамилия директора. Здесь — запасные, на случай вызова без карточки.
DEFAULT_HINTS = [
    "авто континент", "колотовкин", "01905202610324", "9909768607",
]

# Маркеры чужой стороны: если в блоке есть только они, место не наше.
FOREIGN = re.compile(
    r"заказчик|покупател|поставщик|продавец|принципал|клиент|арендатор", re.I)

RE_UNDERSCORES = re.compile(r"_{4,}")
# «М.П.», «МП», «М. П.». Хвостовой `\b` не годится: после точки границы
# слова нет. Вместо него — запрет буквы следом, чтобы не ловить «МПа».
RE_MP = re.compile(r"\bМ\s*\.?\s*П\s*\.?(?![А-Яа-яA-Za-z])", re.I)
RE_SIGN = re.compile(r"подпис", re.I)
RE_POST = re.compile(
    r"директор|руководител|генеральн|исполнител|агент|представител", re.I)


# ─── Разбор страницы ────────────────────────────────────────────────────

def _lines(page):
    """Строки текста страницы: [{'text', 'x0','y0','x1','y1'}]."""
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(s["text"] for s in line["spans"]).strip()
            if not text:
                continue
            x0, y0, x1, y1 = line["bbox"]
            out.append({"text": text, "x0": x0, "y0": y0, "x1": x1, "y1": y1})
    return out


def _rules(page):
    """Горизонтальные линейки страницы: [(x0, x1, y)].

    Берутся и нарисованные линии (`get_drawings`), и «линии» из подчёркиваний
    — в договорах из Word чаще именно второе.
    """
    found = []
    lo, hi = MIN_LINE_MM * PT_MM, MAX_LINE_MM * PT_MM

    for d in page.get_drawings():
        for item in d["items"]:
            if item[0] == "l":
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) < 1.5 and lo <= abs(p2.x - p1.x) <= hi:
                    found.append((min(p1.x, p2.x), max(p1.x, p2.x),
                                  (p1.y + p2.y) / 2))
            elif item[0] == "re":
                r = item[1]
                if r.height < 2.5 and lo <= r.width <= hi:
                    found.append((r.x0, r.x1, (r.y0 + r.y1) / 2))

    for w in page.get_text("words"):
        x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
        if RE_UNDERSCORES.search(text) and lo <= (x1 - x0) <= hi:
            found.append((x0, x1, y1 - 1.0))

    # Соседние отрезки одной линии (Word рвёт подчёркивание на слова)
    found.sort(key=lambda t: (round(t[2], 1), t[0]))
    merged = []
    for x0, x1, y in found:
        if merged and abs(merged[-1][2] - y) < 2 and x0 - merged[-1][1] < 6:
            px0, px1, py = merged[-1]
            merged[-1] = (px0, max(px1, x1), (py + y) / 2)
        else:
            merged.append((x0, x1, y))
    return [m for m in merged if lo <= m[1] - m[0] <= hi]


def _column(x, width):
    return "left" if x < width / 2 else "right"


def _context(lines, x0, x1, y, width):
    """Текст вокруг линии и границы её колонки.

    Возвращает (текст, 'left'/'right', cx0, cx1): 70 пунктов вверх и 25 вниз,
    только своя половина листа. Границы колонки нужны потом как коридор для
    печати — по ним видно, где начинается чужой блок реквизитов.
    """
    col = _column((x0 + x1) / 2, width)
    near, cx0, cx1 = [], x0, x1
    for ln in lines:
        if not (y - 70 <= ln["y1"] and ln["y0"] <= y + 25):
            continue
        if _column((ln["x0"] + ln["x1"]) / 2, width) != col and width > 300:
            # узкие страницы на колонки не делим
            if not (ln["x0"] < x1 and ln["x1"] > x0):
                continue
        near.append(ln["text"])
        cx0, cx1 = min(cx0, ln["x0"]), max(cx1, ln["x1"])
    return " ".join(near), col, cx0, cx1


def _marks(page):
    """Все метки «М.П.» страницы с их рамками.

    Считается по СИМВОЛАМ (`rawdict`), а не по словам: в PDF из LibreOffice
    «М.П.» приходит четырьмя отдельными «словами», а точки в этом шрифте
    вообще отдаются как пробелы, поэтому ни `get_text("words")`, ни
    `search_for("М.П.")` метку не находят. Символьная строка ищется
    регуляркой, рамка — объединение рамок попавших в неё символов.
    """
    out = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            chars = [c for s in line.get("spans", []) for c in s.get("chars", [])]
            if not chars:
                continue
            text = "".join(c["c"] for c in chars)
            for m in RE_MP.finditer(text):
                part = chars[m.start():m.end()]
                if not part:
                    continue
                out.append({
                    "x0": min(c["bbox"][0] for c in part),
                    "y0": min(c["bbox"][1] for c in part),
                    "x1": max(c["bbox"][2] for c in part),
                    "y1": max(c["bbox"][3] for c in part),
                })
    return out


def _mp_box(marks, x0, x1, y, width):
    """Рамка ближайшей метки «М.П.» — от неё пляшет печать.

    Ищется в своей колонке, от 12 пунктов выше линии до 55 ниже: в
    договорах М.П. почти всегда стоит строкой под подписью, реже — в той же
    строке справа. Нет метки — None, тогда печать встанет по линии подписи.
    """
    col = _column((x0 + x1) / 2, width)
    best = None
    for mp in marks:
        if not (y - 12 <= mp["y1"] and mp["y0"] <= y + 55):
            continue
        mx = (mp["x0"] + mp["x1"]) / 2
        # Метка в ОДНОЙ СТРОКЕ с подписью («Директор ____ /Иванов/  М.П.»)
        # засчитывается, даже если по середине листа она попала в чужую
        # половину: это та же строка подписи, а не соседняя колонка.
        same_row = abs((mp["y0"] + mp["y1"]) / 2 - y) < 6 * PT_MM \
            and abs(mx - x1) < 90 * PT_MM
        if not same_row and _column(mx, width) != col and width > 300:
            continue
        d = abs(mp["y0"] - y)
        if best is None or d < best[0]:
            best = (d, mp)
    return best[1] if best else None


def find_slots(pdf_path, hints=None, min_score=3.0, max_slots=12):
    """Места, куда просится наша подпись.

    Каждое — dict: page (с нуля), x0/x1/y линии в пунктах, label для меню,
    score (чем выше, тем увереннее), our (нашли ли рядом наши реквизиты).
    Отсортированы по странице и месту на ней; ниже min_score отбрасываются.
    """
    fitz = _fitz()

    hints = [h.lower() for h in (hints or DEFAULT_HINTS) if h]
    slots = []
    with fitz.open(pdf_path) as doc:
        for pno, page in enumerate(doc):
            width = page.rect.width
            lines = _lines(page)
            page_text = " ".join(ln["text"] for ln in lines).lower()
            ours_here = any(h in page_text for h in hints)

            marks = _marks(page)
            for x0, x1, y in _rules(page):
                ctx, col, cx0, cx1 = _context(lines, x0, x1, y, width)
                low = ctx.lower()
                score = 0.0
                our = any(h in low for h in hints)
                if our:
                    score += 4
                elif ours_here:
                    score += 0.5
                if RE_SIGN.search(ctx):
                    score += 2
                if RE_MP.search(ctx):
                    score += 2
                if RE_POST.search(ctx):
                    score += 1.5
                if FOREIGN.search(ctx) and not our:
                    score -= 3
                # линия в самом низу страницы — обычно как раз реквизиты
                if y > page.rect.height * 0.6:
                    score += 0.5
                if score < min_score:
                    continue
                title = next((ln["text"] for ln in lines
                              if ln["y1"] < y and y - ln["y1"] < 40
                              and RE_POST.search(ln["text"])), "")
                slots.append({
                    "page": pno, "x0": x0, "x1": x1, "y": y,
                    "col": col, "cx0": cx0, "cx1": cx1,
                    "mp": _mp_box(marks, x0, x1, y, width),
                    "score": round(score, 1), "our": our,
                    "label": f"стр. {pno + 1}" + (f" · {title[:40]}" if title else ""),
                })

    # два кандидата на одну линию (нарисованная + подчёркивание) → лучший
    slots.sort(key=lambda s: (s["page"], s["y"], -s["score"]))
    clean = []
    for s in slots:
        if clean and clean[-1]["page"] == s["page"] \
                and abs(clean[-1]["y"] - s["y"]) < 12 \
                and clean[-1]["col"] == s["col"]:
            if s["score"] > clean[-1]["score"]:
                clean[-1] = s
            continue
        clean.append(s)
    clean.sort(key=lambda s: (-s["score"], s["page"], s["y"]))
    return clean[:max_slots]


def nudge(slot, dx_mm=0.0, dy_mm=0.0):
    """Сдвинутая копия места — под кнопки «⬅️➡️⬆️⬇️» в боте."""
    s = dict(slot)
    s["x0"] += dx_mm * PT_MM
    s["x1"] += dx_mm * PT_MM
    s["cx0"] = s.get("cx0", s["x0"]) + dx_mm * PT_MM
    s["cx1"] = s.get("cx1", s["x1"]) + dx_mm * PT_MM
    s["y"] += dy_mm * PT_MM
    # метку М.П. двигаем вместе с местом, иначе печать останется на месте
    if s.get("mp"):
        mp = dict(s["mp"])
        mp["x0"] += dx_mm * PT_MM
        mp["x1"] += dx_mm * PT_MM
        mp["y0"] += dy_mm * PT_MM
        mp["y1"] += dy_mm * PT_MM
        s["mp"] = mp
    return s


# ─── Вставка ────────────────────────────────────────────────────────────

def _prepared(kind, d, height_mm):
    """PNG, обрезанный по чернилам и повёрнутый, плюс его размер в мм.

    Обрезка нужна, чтобы поля вокруг росчерка не участвовали в размере:
    у каждого скана они свои, а высота чернил должна быть одна и та же.
    """
    from PIL import Image

    pool = sj._pool(kind)
    name = d.get("file", sj.NO_FILE)
    path = sj._pick(pool, name) if name != sj.NO_FILE else None
    if path is None:
        logger.warning("sign_pdf: файла %s нет в наборе %s", name, kind)
        return None
    blob = sj._ink(path.read_bytes(), kind, d, from_pool=True) or path.read_bytes()

    im = Image.open(io.BytesIO(blob))
    im.load()
    if im.mode != "RGBA":
        im = im.convert("RGBA")
    bb = im.getchannel("A").point(lambda v: 255 if v > 20 else 0).getbbox()
    if bb:
        im = im.crop(bb)
    w, h = im.size
    mm_px = height_mm / h                      # мм на пиксель до поворота

    rot = float(d.get("rot", 0))
    if abs(rot) > 0.01:
        im = im.rotate(rot, resample=Image.BICUBIC, expand=True)

    out = io.BytesIO()
    im.save(out, format="PNG", optimize=True)
    return out.getvalue(), im.width * mm_px, im.height * mm_px, w * mm_px, h * mm_px


def _put(page, png, w_mm, h_mm, cx_pt, cy_pt):
    fitz = _fitz()
    w, h = w_mm * PT_MM, h_mm * PT_MM
    rect = fitz.Rect(cx_pt - w / 2, cy_pt - h / 2, cx_pt + w / 2, cy_pt + h / 2)
    page.insert_image(rect, stream=png, overlay=True, keep_proportion=False)


def sign(pdf_path, out_path, slots, doc_key, spec=None,
         with_stamp=True, with_signature=True):
    """Кладёт подпись и печать в каждое из мест и сохраняет новый PDF.

    doc_key — стабильный ключ документа (имя файла без расширения годится).
    spec    — строка из журнала: что выпало в прошлый раз. Передана —
              ничего не разыгрывается, документ пересобирается тем же видом.
              Возвращается спек, который нужно сохранить.

    У каждого места свой розыгрыш (ключ `doc_key#N`): в одном договоре
    подпись на приложении и подпись под основным текстом ставились по
    очереди и совпадать пиксель в пиксель не должны.
    """
    fitz = _fitz()

    if not sj.ENABLED:
        return spec or ""

    doc = fitz.open(pdf_path)
    ledger = spec or ""
    try:
        for i, slot in enumerate(slots):
            key = f"{doc_key}#{i}"
            saved = sj.parse_spec(sj.ledger_get(ledger, key))
            rnd = random.Random(
                int(hashlib.md5(key.encode("utf-8")).hexdigest()[:12], 16))
            page = doc[slot["page"]]
            drawn = {}

            kinds = []
            if with_signature:
                kinds.append("signature")
            if with_stamp:
                kinds.append("stamp")

            for kind in kinds:
                pool = sj._pool(kind)
                if not pool:
                    logger.warning("sign_pdf: набор %s пуст", kind)
                    continue
                d = saved.get(kind) or sj._draw(kind, rnd, pool)
                drawn[kind] = d

                base_mm = (sj.PROFILES[kind].get("ink_mm") or SIG_INK_MM)
                prep = _prepared(kind, d, base_mm * float(d.get("k", 1.0)))
                if not prep:
                    continue
                png, w_mm, h_mm, ink_w, ink_h = prep

                mid = (slot["x0"] + slot["x1"]) / 2
                mp = slot.get("mp")
                if kind == "signature":
                    cx = mid + (SIG_RIGHT_MM + float(d["dx"])) * PT_MM
                    cy = slot["y"] + (SIG_BELOW_MM - ink_h / 2
                                      + float(d["dy"])) * PT_MM
                elif mp:
                    # Печать пляшет от метки М.П., а не от подписи: метка для
                    # того и напечатана, и по образцу оттиск садится ровно
                    # на неё.
                    cx = (mp["x0"] + mp["x1"]) / 2 \
                        + (STAMP_MP_DX_MM + float(d["dx"])) * PT_MM
                    cy = (mp["y0"] + mp["y1"]) / 2 \
                        + (STAMP_MP_DY_MM + float(d["dy"])) * PT_MM
                else:
                    # Метки нет — от линии подписи, на четверти её длины.
                    cx = slot["x0"] + (slot["x1"] - slot["x0"]) * STAMP_AT_LINE \
                        + float(d["dx"]) * PT_MM
                    cy = slot["y"] + (STAMP_BELOW_MM + float(d["dy"])) * PT_MM

                half_w, half_h = w_mm * PT_MM / 2, h_mm * PT_MM / 2

                # Не выпускаем из СВОЕЙ колонки: рядом стоит чужой блок
                # реквизитов, и при крайнем сдвиге влево (-14 мм у печати)
                # оттиск ложился на фамилию контрагента. Коридор — границы
                # колонки, посчитанные при поиске, плюс небольшой допуск:
                # свисать оттиск обязан, он шире любой линии подписи.
                if kind == "stamp" and mp:
                    # Печать привязана к метке — и коридор считаем от неё:
                    # так у розыгрыша остаётся весь его ход, а в соседнюю
                    # колонку оттиск не уходит при любом раскладе. Границы
                    # колонки для этого не годятся: в таблице реквизитов
                    # строка бывает общей на обе стороны, и «своя» колонка
                    # получается во весь лист.
                    mpc = (mp["x0"] + mp["x1"]) / 2
                    lo = mpc - MP_SPAN_MM * PT_MM
                    hi = mpc + MP_SPAN_MM * PT_MM
                else:
                    lo = min(slot["cx0"], slot["x0"]) - OVER_MM * PT_MM + half_w
                    hi = max(slot["cx1"], slot["x1"]) + OVER_MM * PT_MM - half_w
                cx = max(lo, min(cx, hi)) if lo <= hi else (lo + hi) / 2

                # и не за край листа
                edge = sj.EDGE_MM * PT_MM
                cx = max(edge + half_w, min(cx, page.rect.width - edge - half_w))
                cy = max(half_h, min(cy, page.rect.height - half_h))

                _put(page, png, w_mm, h_mm, cx, cy)

            if drawn:
                ledger = sj.ledger_set(ledger, key, sj.format_spec(drawn))

        doc.save(out_path, garbage=3, deflate=True)
    finally:
        doc.close()

    logger.info("sign_pdf: %s — мест %d", doc_key, len(slots))
    return ledger


# ─── Предпросмотр ───────────────────────────────────────────────────────

def render_page(pdf_path, page_no=0, dpi=110, box=None):
    """PNG страницы для отправки в чат. box — место, которое надо обвести
    (для показа «вот сюда поставлю»)."""
    fitz = _fitz()

    with fitz.open(pdf_path) as doc:
        page = doc[page_no]
        if box:
            r = fitz.Rect(box["x0"] - 4 * PT_MM, box["y"] - 46 * PT_MM,
                          box["x1"] + 4 * PT_MM, box["y"] + 4 * PT_MM)
            page.draw_rect(r & page.rect, color=(0.9, 0.25, 0.2), width=1.2)
        return page.get_pixmap(dpi=dpi).tobytes("png")


def page_count(pdf_path):
    fitz = _fitz()
    with fitz.open(pdf_path) as doc:
        return doc.page_count
