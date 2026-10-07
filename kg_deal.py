"""kg_deal.py — сделка «Учёт КГ»: авто выкупается на кыргызских номерах и учёте.

С 07.10.2026 (правила — «ПРАВКИ ДЛЯ БОТА — вариант 3.md»). Покупатель из РФ
платит, когда продавец по ДКП ещё не известен. Сделка идёт в два этапа:

  Этап 1 — оплата. Договор contract_template_KG (в Поручении только марка,
           модель, цвет, год и цена — без VIN и продавца) и счёт. После полной
           оплаты статус «Подбор».
  Этап 2 — кнопка «🚗 Внести данные ТС». ТС зарегистрировано на продавца в КР:
           СРТС, ID-карта продавца, доверенность, дата ДКП. Дальше Спецификация
           (Прил. № 2), ДКП, расписка, акт и отчёт — шаблоны *_KG. Статус
           «завершена».

ТПО в документах «Учёт КГ» не используется: основание права продавца — СРТС.
Правило «пустая дата ДКП = дата договора» здесь НЕ действует: пустая дата ДКП
означает, что этап 2 не начат.

В журнале тип сделки — значение «Учёт КГ» в колонке «Тип сделки».
"""
import json
import re
import time
from datetime import date, datetime

import memory

DEAL_TYPE_KG = "Учёт КГ"
STATUS_PODBOR = "Подбор"

# Срок исполнения поручения по договору (п. 2.4) и шаг напоминаний.
DEADLINE_DAYS = 30
REMIND_EVERY_DAYS = 5

TEMPLATES = {
    "contract": "contract_template_KG.docx",
    "spec":     "spec_template_KG.docx",
    "dkp":      "dkp_template_KG.docx",
    "receipt":  "raspiska_template_KG.docx",
    "act":      "act_template_KG.docx",
    "report":   "otchet_agenta_template_KG.docx",
}

# Колонки журнала (ключи gsheets_service.COLUMNS)
COL_SPEC_DATE = "Дата спецификации"
COL_SRTS      = "srts"        # «серия KG № 1234567 от 05.10.2026»
COL_GOSNOMER  = "gos_number"  # «01KG123ABC»
COL_POA       = "poa"         # «№ 1234 от 06.10.2026, удостоверена нотариусом …»

# Поля этапа 2, без которых не собираются Спецификация и ДКП.
STAGE2_FIELDS = [
    ("seller_name",           "ФИО продавца"),
    ("seller_initials",       "инициалы продавца"),
    ("seller_birth_date",     "дата рождения продавца"),
    ("seller_address",        "адрес продавца"),
    ("seller_id_number",      "номер ID-карты продавца"),
    ("seller_id_issued_by",   "кем выдана ID-карта"),
    ("seller_id_issued_date", "дата выдачи ID-карты"),
    ("car_vin",               "VIN"),
    (COL_SRTS,                "СРТС (серия, номер, дата)"),
    (COL_GOSNOMER,            "госномер"),
    ("Дата ДКП",              "дата ДКП"),
]
# Доверенность нужна расписке, акту и отчёту. Спецификация и ДКП без неё
# собираются: доверенность выдаётся одновременно с подписанием ДКП (п. 8 ДКП).
POA_FIELD = (COL_POA, "реквизиты доверенности")

# Поля этапа 1 — то, что идёт в Поручение.
STAGE1_FIELDS = [
    ("car_model", "марка и модель"),
    ("car_year",  "год выпуска"),
    ("car_color", "цвет"),
    ("car_price", "цена (руб.)"),
]

_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


# ─── Тип сделки ─────────────────────────────────────────────────────────────

def is_kg(data: dict) -> bool:
    v = str((data or {}).get("deal_type") or "").strip().lower().replace("ё", "е")
    return v.startswith("учет кг") or v in ("кг", "kg", "учет kg")


def _val(data: dict, key: str) -> str:
    v = str((data or {}).get(key) or "").strip()
    return "" if v == "None" else v


def stage2_done(deal: dict) -> bool:
    """Данные ТС внесены (этап 2 начат): есть VIN и дата ДКП."""
    return bool(_val(deal, "car_vin")) and bool(_val(deal, "Дата ДКП"))


# ─── Даты ───────────────────────────────────────────────────────────────────

def parse_date(s) -> date | None:
    m = _DATE_RE.search(str(s or ""))
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def srts_date(srts: str) -> str:
    """Дата регистрации ТС в КР — последняя дата в строке СРТС
    («серия KG № 1234567 от 05.10.2026» → «05.10.2026»)."""
    found = _DATE_RE.findall(str(srts or ""))
    if not found:
        return ""
    d, m, y = found[-1]
    return f"{d}.{m}.{y}"


def spec_date(deal: dict) -> str:
    """Дата Спецификации: колонка «Дата спецификации», по умолчанию = Дата ДКП."""
    return _val(deal, COL_SPEC_DATE) or _val(deal, "Дата ДКП") or _val(deal, "dkp_date")


def date_order_problems(deal: dict, settlement_date: str = "") -> list:
    """Порядок дат по разделу 6 правил. Каждая дата не раньше предыдущей:
    договор ≤ поступление ≤ регистрация ТС (СРТС) ≤ Спецификация = ДКП ≤ расчёт.
    Пустые даты пропускаются (их отсутствие ловят проверки заполнения)."""
    dkp = _val(deal, "Дата ДКП") or _val(deal, "dkp_date")
    chain = [
        ("дата договора",         _val(deal, "Дата договора")),
        ("дата поступления",      _val(deal, "Дата поступления")),
        ("дата регистрации ТС (СРТС)", srts_date(_val(deal, COL_SRTS))),
        ("дата ДКП",              dkp),
        ("дата расчёта",          settlement_date or _val(deal, "Дата расчёта")),
    ]
    problems = []
    spec = spec_date(deal)
    if spec and dkp and parse_date(spec) and parse_date(dkp) and parse_date(spec) != parse_date(dkp):
        problems.append(f"дата Спецификации ({spec}) должна совпадать с датой ДКП ({dkp})")
    prev_label, prev = None, None
    for label, value in chain:
        d = parse_date(value)
        if not d:
            continue
        if prev and d < prev:
            problems.append(f"{label} ({value}) раньше, чем {prev_label} ({prev.strftime('%d.%m.%Y')})")
        prev_label, prev = label, d
    return problems


def overdue_warning(deal: dict) -> str:
    """ДКП позже 30 дней от поступления — срок по договору истёк (п. 2.4).
    Только предупреждение, выдачу не блокирует."""
    got = parse_date(_val(deal, "Дата поступления"))
    dkp = parse_date(_val(deal, "Дата ДКП"))
    if got and dkp and (dkp - got).days > DEADLINE_DAYS:
        return (f"⚠️ От поступления средств ({got.strftime('%d.%m.%Y')}) до ДКП "
                f"прошло {(dkp - got).days} дн. — срок исполнения по договору "
                f"({DEADLINE_DAYS} дн.) истёк.")
    return ""


# ─── Проверки ───────────────────────────────────────────────────────────────

def stage1_missing(data: dict) -> list:
    return [label for key, label in STAGE1_FIELDS if not _val(data, key)]


def stage2_missing(deal: dict, need_poa: bool = False) -> list:
    out = [label for key, label in STAGE2_FIELDS if not _val(deal, key)]
    if need_poa and not _val(deal, POA_FIELD[0]):
        out.append(POA_FIELD[1])
    return out


def stage2_block(deal: dict, need_poa: bool = False, settlement_date: str = "") -> str:
    """Причина, по которой документы этапа 2 нельзя выдать, или пустая строка."""
    if not stage2_done(deal):
        return ("Данные ТС ещё не внесены — это сделка «Учёт КГ», документы этапа 2 "
                "(Спецификация, ДКП, расписка, акт, отчёт) формируются после кнопки "
                "«🚗 Внести данные ТС» в карточке сделки.")
    problems = []
    missing = stage2_missing(deal, need_poa=need_poa)
    if missing:
        problems.append("не заполнено: " + ", ".join(missing))
    vin = re.sub(r"[^A-Za-z0-9]", "", _val(deal, "car_vin"))
    if vin and len(vin) != 17:
        problems.append(f"VIN «{vin}» — {len(vin)} знаков вместо 17")
    srts = _val(deal, COL_SRTS)
    if srts and not srts_date(srts):
        problems.append("в СРТС нет даты выдачи (нужно «серия … № … от ДД.ММ.ГГГГ»)")
    problems += date_order_problems(deal, settlement_date)
    if not problems:
        return ""
    return "Документы «Учёт КГ» не выданы:\n" + "\n".join(f"• {p}" for p in problems)


# ─── Тексты для счёта ───────────────────────────────────────────────────────

def car_line(data: dict, model: str = "") -> str:
    """Строка автомобиля в счёте без VIN: «Haval Jolion, цвет белый, год выпуска 2025»."""
    bits = [model or _val(data, "car_model")]
    if _val(data, "car_color"):
        bits.append(f"цвет {_val(data, 'car_color').lower()}")
    if _val(data, "car_year"):
        bits.append(f"год выпуска {_val(data, 'car_year')}")
    return ", ".join(b for b in bits if b)


def qr_purpose(number: str, date_str: str, data: dict, model: str = "") -> str:
    """Назначение платежа в QR (счёт на РФ-счёт) — без VIN: на этапе оплаты его нет."""
    bits = [model or _val(data, "car_model"), _val(data, "car_color").lower(), _val(data, "car_year")]
    car = ", ".join(b for b in bits if b)
    return (f"Оплата по счету №{number} от {date_str} по Агентскому договору №{number} "
            f"от {date_str} за автомобиль {car}. Без НДС.")


# ─── Напоминание о сроке ────────────────────────────────────────────────────

def reminder_days(deal: dict, today: date | None = None) -> int:
    """Сколько дней сделка в «Подборе», если сегодня день напоминания
    (каждые 5 дней от даты поступления), иначе 0."""
    if not is_kg(deal):
        return 0
    if _val(deal, "Статус").lower() != STATUS_PODBOR.lower():
        return 0
    got = parse_date(_val(deal, "Дата поступления"))
    if not got:
        return 0
    days = ((today or date.today()) - got).days
    return days if days > 0 and days % REMIND_EVERY_DAYS == 0 else 0


def reminder_text(items: list) -> str:
    """items: [(deal, days)] → одно сообщение."""
    lines = ["⏰ *Сделки «Учёт КГ» в статусе «Подбор»*", ""]
    for deal, days in items:
        car = car_line(deal) or "—"
        line = f"• *{_val(deal, 'Номер договора')}* · {car} · {days} дн. от поступления"
        if days >= DEADLINE_DAYS:
            line += " — ⚠️ срок по договору истёк"
        lines.append(line)
    lines += ["", "Когда ТС будет на продавце — «🚗 Внести данные ТС» в карточке сделки."]
    return "\n".join(lines)


# ─── Выбор «Учёт КГ» для новой сделки и этап 2 — состояние у бота ───────────
# Как у салонов (salon.set_pending): кнопка запоминает выбор, create_contract
# сам проставляет тип, LLM только напоминается в промпте. Живёт сутки.

PENDING_TTL_SEC = 24 * 3600


def _new_key(chat_id) -> str:
    return f"new_deal_kg:{chat_id}"


def set_new_pending(chat_id) -> None:
    if chat_id:
        memory.set_setting(_new_key(chat_id), json.dumps({"ts": time.time()}))


def get_new_pending(chat_id) -> bool:
    if not chat_id:
        return False
    raw = memory.get_setting(_new_key(chat_id)) or ""
    if not raw:
        return False
    try:
        ts = float(json.loads(raw).get("ts") or 0)
    except Exception:
        return False
    if time.time() - ts > PENDING_TTL_SEC:
        clear_new_pending(chat_id)
        return False
    return True


def clear_new_pending(chat_id) -> None:
    if chat_id:
        memory.set_setting(_new_key(chat_id), "")


def _veh_key(chat_id) -> str:
    return f"kg_vehicle:{chat_id}"


def set_vehicle_pending(chat_id, number: str) -> None:
    """Начат этап 2 по сделке number: присланные файлы СРТС/ID/доверенности
    копятся здесь с типом, выбранным кнопкой, и уходят на Drive в set_kg_vehicle."""
    if chat_id:
        memory.set_setting(_veh_key(chat_id),
                           json.dumps({"num": number, "ts": time.time(), "files": []}))


def get_vehicle_pending(chat_id) -> dict:
    if not chat_id:
        return {}
    raw = memory.get_setting(_veh_key(chat_id)) or ""
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except Exception:
        return {}
    if time.time() - float(val.get("ts") or 0) > PENDING_TTL_SEC:
        clear_vehicle_pending(chat_id)
        return {}
    return val


def add_vehicle_file(chat_id, filepath: str, filename: str, kind: str) -> None:
    val = get_vehicle_pending(chat_id)
    if not val:
        return
    val.setdefault("files", []).append({"path": filepath, "name": filename, "kind": kind})
    memory.set_setting(_veh_key(chat_id), json.dumps(val))


def clear_vehicle_pending(chat_id) -> None:
    if chat_id:
        memory.set_setting(_veh_key(chat_id), "")


# Типы файлов этапа 2: (код, подпись кнопки, префикс имени на Drive)
FILE_KINDS = [
    ("srts", "📘 СРТС",                 "СРТС"),
    ("id",   "🪪 ID-карта продавца",     "Документы_сторон"),
    ("poa",  "📜 Доверенность",          "Доверенность"),
]
FILE_KIND_LABELS = {k: lbl.split(" ", 1)[1] for k, lbl, _ in FILE_KINDS}
FILE_KIND_PREFIX = {k: p for k, _, p in FILE_KINDS}


def vin6(vin: str) -> str:
    v = re.sub(r"[^A-Za-z0-9]", "", str(vin or ""))
    return v[-6:].upper() if len(v) >= 6 else ""


def drive_name(kind: str, number: str, vin: str, ext: str) -> str:
    """СРТС_XXXXXX / Доверенность_XXXXXX (последние 6 знаков VIN, как в номере
    ДКП); ID-карта — Документы_сторон_<номер сделки>."""
    prefix = FILE_KIND_PREFIX.get(kind, "Прочее")
    tail = vin6(vin) if kind in ("srts", "poa") and vin6(vin) else number
    return f"{prefix}_{tail}{ext}"


def today_str() -> str:
    return datetime.now().strftime("%d.%m.%Y")
