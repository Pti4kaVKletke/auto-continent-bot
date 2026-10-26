# -*- coding: utf-8 -*-
"""
dkp_only.py — «📝 Только ДКП» (26.10.2026): договор купли-продажи как
отдельный инструмент, без сделки.

Ничего не пишется в журнал и на Drive, агентский договор и счёт не
собираются: бот читает документы (как при новой сделке), собирает ДКП по
обычному шаблону (по ТПО) или по шаблону «Учёт КГ» (СРТС + госномер) и
отдаёт DOCX + PDF в чат. Подписи в ДКП — только сторон (клиентов), печать и
подпись компании не ставятся.

Режим живёт у бота, а не в памяти LLM (см. feedback-llm-state): ключ
настройки dkp_only:<chat_id> с вариантом и временем включения. Включается
кнопкой в меню, выключается кнопкой «◀️ Меню», новой сделкой и /clear.
"""
import json
import re
import time
from datetime import datetime

import kg_deal
import memory

VARIANT_TPO = "tpo"
VARIANT_KG = "kg"
VARIANTS = {
    VARIANT_TPO: "по ТПО",
    VARIANT_KG: "«Учёт КГ» (СРТС, госномер)",
}

PENDING_TTL_SEC = 24 * 3600
TOOL_NAME = "build_dkp_only"

# ── Поля ────────────────────────────────────────────────────────────────────

SELLER_FIELDS = [
    ("seller_name",           "ФИО продавца"),
    ("seller_initials",       "инициалы продавца"),
    ("seller_birth_date",     "дата рождения продавца"),
    ("seller_address",        "адрес продавца"),
    ("seller_id_number",      "номер ID-карты продавца"),
    ("seller_id_issued_by",   "кем выдана ID-карта"),
    ("seller_id_issued_date", "дата выдачи ID-карты"),
]
BUYER_FIELDS = [
    ("buyer_name",           "ФИО покупателя"),
    ("buyer_initials",       "инициалы покупателя"),
    ("buyer_birth_date",     "дата рождения покупателя"),
    ("buyer_address",        "адрес покупателя"),
    ("passport_series",      "серия паспорта"),
    ("passport_number",      "номер паспорта"),
    ("passport_issued_by",   "кем выдан паспорт"),
    ("passport_issued_date", "дата выдачи паспорта"),
    ("passport_code",        "код подразделения"),
]
CAR_FIELDS = [
    ("car_model", "марка и модель"),
    ("car_vin",   "VIN"),
    ("car_year",  "год выпуска"),
    ("car_color", "цвет"),
    ("car_price", "цена (руб.)"),
]
VARIANT_FIELDS = {
    VARIANT_TPO: [("tpo_number", "номер ТПО"), ("tpo_date", "дата ТПО")],
    VARIANT_KG:  [(kg_deal.COL_SRTS, "СРТС (серия, номер, дата)"),
                  (kg_deal.COL_GOSNOMER, "госномер"),
                  (kg_deal.COL_POA, "номер и дата доверенности")],
}

# П. 8 ДКП «Учёт КГ»: продавец одновременно выдаёт доверенность. В шаблоне
# номера нет (в сделке доверенность вносится позже), а в отдельном ДКП Илья
# вводит её текстом — номер и дата вставляются сразу после этих слов.
POA_ANCHOR = "нотариально удостоверенную доверенность"

# Поля сделки, которые к отдельному ДКП не относятся и не должны влиять на
# сборку (тип сделки, салон, номер/дата сделки из прошлой переписки и т.п.).
_DROP_KEYS = ("deal_type", "salon", "salon_contract", "Номер договора",
              "Дата договора", "Дата ДКП")

_DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")


# ── Состояние режима ────────────────────────────────────────────────────────

def _key(chat_id) -> str:
    return f"dkp_only:{chat_id}"


def set_pending(chat_id, variant: str) -> None:
    if chat_id and variant in VARIANTS:
        memory.set_setting(_key(chat_id), json.dumps({"ts": time.time(), "variant": variant}))


def get_pending(chat_id) -> str:
    """Вариант ДКП («tpo»/«kg»), если режим включён, иначе пустая строка."""
    if not chat_id:
        return ""
    raw = memory.get_setting(_key(chat_id)) or ""
    if not raw:
        return ""
    try:
        st = json.loads(raw)
        ts, variant = float(st.get("ts") or 0), str(st.get("variant") or "")
    except Exception:
        return ""
    if variant not in VARIANTS or time.time() - ts > PENDING_TTL_SEC:
        clear_pending(chat_id)
        return ""
    return variant


def clear_pending(chat_id) -> None:
    if chat_id:
        memory.set_setting(_key(chat_id), "")


# ── Тексты ──────────────────────────────────────────────────────────────────

CHOICE_TEXT = (
    "📝 *Только ДКП*\n\n"
    "Договор купли-продажи без сделки: в журнал и на Drive ничего не пишется, "
    "агентский договор и счёт не делаются.\n\n"
    "Какой ДКП нужен?\n"
    "• *По ТПО* — авто растаможено, право продавца по ТПО.\n"
    "• *Учёт КГ* — авто на кыргызском учёте, по СРТС и госномеру."
)


def docs_text(variant: str) -> str:
    car_docs = ("• ТПО и/или таможенную декларацию" if variant == VARIANT_TPO
                else "• СРТС (техпаспорт КР) — с госномером и датой выдачи")
    return (
        f"📝 *Только ДКП — {VARIANTS[variant]}*\n\n"
        "Отправь документы:\n"
        f"{car_docs}\n"
        "• ID-карту продавца\n"
        "• Паспорт РФ покупателя\n\n"
        "И напиши цену в рублях. Дату ДКП спрошу (по умолчанию сегодня)."
        + ("\nНомер и дату доверенности продавца на покупателя тоже спрошу — "
           "введёшь текстом." if variant == VARIANT_KG else "")
    )


def prompt_section(chat_id) -> str:
    variant = get_pending(chat_id)
    if not variant:
        return ""
    if variant == VARIANT_TPO:
        car_part = ("• ТПО/декларация → car_model, car_vin, car_year, car_color, tpo_number, "
                    "tpo_date (по тем же правилам извлечения, что и для сделки);\n")
    else:
        car_part = ("• СРТС → car_vin, car_model, car_year, car_color, srts одной строкой "
                    "«серия KG № 1234567 от ДД.ММ.ГГГГ» (дата выдачи обязательна), "
                    "gos_number (госномер). ТПО не нужен;\n"
                    "• poa — номер и дата доверенности продавца на покупателя строкой "
                    "«№ 1234 от ДД.ММ.ГГГГ» (без нотариуса и города). Её обычно нет на "
                    "сканах — СПРОСИ у пользователя текстом вместе с остальным недостающим;\n")
    return (
        f"\n\n=== РЕЖИМ «ТОЛЬКО ДКП» ({VARIANTS[variant]}) — ПЕРЕКРЫВАЕТ ПРАВИЛА СДЕЛКИ ===\n"
        "Пользователь хочет ТОЛЬКО договор купли-продажи, БЕЗ сделки. НЕ вызывай "
        "create_contract, generate_docs, update_deal, add_payment и не предлагай их. "
        "НЕ спрашивай комиссию, курс, сумму в долларах, банковские реквизиты и профиль — "
        "они здесь не нужны. Номер ДКП бот считает сам по VIN.\n"
        "Из документов извлеки:\n"
        f"{car_part}"
        "• ID-карта продавца → seller_name, seller_initials, seller_birth_date, "
        "seller_address, seller_id_number, seller_id_issued_by, seller_id_issued_date;\n"
        "• паспорт РФ покупателя → buyer_type («физ.лицо» или «ИП»; для ИП ещё buyer_inn), "
        "buyer_name, buyer_initials, buyer_birth_date, buyer_address, passport_series, "
        "passport_number, passport_issued_by, passport_issued_date, passport_code;\n"
        "• car_price — цена в рублях цифрами (спроси, если не назвали), "
        "car_price_words — пропись БЕЗ слова «рублей», currency — «рублей».\n"
        "Дату ДКП (dkp_date, ДД.ММ.ГГГГ) спроси одним вопросом вместе с остальными "
        "недостающими полями; если пользователь не назвал — сегодня.\n"
        "Когда всё собрано — покажи короткую сводку и сразу вызови build_dkp_only "
        f"(variant=\"{variant}\"). На просьбу что-то поправить — снова вызови "
        "build_dkp_only с исправленными данными.\n"
    )


# Короткая шапка В НАЧАЛО промпта и напоминание в каждое сообщение
# пользователя. Одного блока в конце большого промпта сделки не хватило
# (проверка 26.10.2026): модель всё равно спрашивала «полный пакет или только
# ДКП?», комиссию, курс и реквизиты.
def prompt_head(chat_id) -> str:
    variant = get_pending(chat_id)
    if not variant:
        return ""
    return (
        f"!!! СЕЙЧАС РЕЖИМ «ТОЛЬКО ДКП» ({VARIANTS[variant]}). Это НЕ сделка: собираешь только "
        "договор купли-продажи инструментом build_dkp_only. Тип уже выбран кнопкой — НЕ спрашивай "
        "«полный пакет или только ДКП», «тип сделки», комиссию, курс, сумму в USD, банковские "
        "реквизиты/профиль. Правила сделки ниже (create_contract, реквизиты, комиссия, Поручение) "
        "в этом режиме НЕ действуют — действует блок «РЕЖИМ ТОЛЬКО ДКП» в конце. !!!\n\n"
    )


def user_reminder(chat_id) -> str:
    variant = get_pending(chat_id)
    if not variant:
        return ""
    need = ("продавец, покупатель, авто, ТПО, цена в рублях, дата ДКП" if variant == VARIANT_TPO
            else "продавец, покупатель, авто, СРТС, госномер, доверенность (№ и дата), "
                 "цена в рублях, дата ДКП")
    return (f"[Режим «Только ДКП» ({VARIANTS[variant]}). Нужны только: {need}. "
            "Не спрашивай тип сделки, комиссию, курс, USD и реквизиты. "
            "Всё собрано — вызывай build_dkp_only.]")


# ── Проверка и сборка ───────────────────────────────────────────────────────

def _v(data: dict, key: str) -> str:
    return str((data or {}).get(key) or "").strip()


def missing_fields(data: dict, variant: str) -> list[str]:
    fields = SELLER_FIELDS + BUYER_FIELDS + CAR_FIELDS + VARIANT_FIELDS[variant]
    miss = [label for key, label in fields if not _v(data, key)]
    if _v(data, "buyer_type").lower() in ("ип", "индивидуальный предприниматель") \
            and not _v(data, "buyer_inn"):
        miss.append("ИНН покупателя (для ИП)")
    return miss


def problems(data: dict, variant: str, dkp_date: str) -> list[str]:
    out = []
    vin = re.sub(r"[^A-Za-z0-9]", "", _v(data, "car_vin"))
    if vin and len(vin) != 17:
        out.append(f"VIN «{vin}» — {len(vin)} знаков вместо 17")
    if not _DATE_RE.match(dkp_date):
        out.append(f"дата ДКП «{dkp_date}» — нужен формат ДД.ММ.ГГГГ")
    if variant == VARIANT_KG:
        srts = _v(data, kg_deal.COL_SRTS)
        reg = kg_deal.srts_date(srts) if srts else ""
        if srts and not reg:
            out.append("в СРТС нет даты выдачи (нужно «серия … № … от ДД.ММ.ГГГГ»)")
        elif reg and _DATE_RE.match(dkp_date):
            d_reg = datetime.strptime(reg, "%d.%m.%Y")
            d_dkp = datetime.strptime(dkp_date, "%d.%m.%Y")
            if d_dkp < d_reg:
                out.append(f"дата ДКП {dkp_date} раньше регистрации ТС в КР ({reg})")
    return out


def poa_text(poa: str) -> str:
    """«1234 от 06.10.2026» / «№1234 от 06.10.2026.» → «№ 1234 от 06.10.2026»."""
    s = re.sub(r"\s+", " ", str(poa or "")).strip().rstrip(".,;")
    s = re.sub(r"^№\s*", "", s)
    return f"№ {s}" if s else ""


def insert_poa(docx_path: str, poa: str) -> bool:
    """Вписывает номер и дату доверенности в п. 8 ДКП «Учёт КГ».
    False — в шаблоне не нашлось места (текст пункта поменяли)."""
    from docx import Document
    text = poa_text(poa)
    if not text:
        return False
    doc = Document(docx_path)
    for p in doc.paragraphs:
        for r in p.runs:
            if POA_ANCHOR in (r.text or ""):
                r.text = r.text.replace(POA_ANCHOR, f"{POA_ANCHOR} {text}", 1)
                doc.save(docx_path)
                return True
    return False


def buyer_surname(data: dict) -> str:
    """Фамилия покупателя для имени файла (решение Ильи 26.10.2026 —
    покупатель, не продавец). Префикс «ИП» у покупателя-ИП отбрасывается."""
    src = _v(data, "buyer_initials") or _v(data, "buyer_name")
    words = [w for w in src.split()
             if w.upper().strip(".") not in ("ИП", "ИНДИВИДУАЛЬНЫЙ", "ПРЕДПРИНИМАТЕЛЬ")]
    word = words[0] if words else ""
    word = re.sub(r"[^\w-]", "", word)
    return word[:1].upper() + word[1:].lower() if word else ""


def file_base(data: dict, dkp_number: str) -> str:
    parts = ["ДКП", dkp_number or "б-н"]
    surname = buyer_surname(data)
    if surname:
        parts.append(surname)
    return "_".join(parts)


def prepare_data(data: dict, variant: str, dkp_date: str) -> dict:
    d = {k: v for k, v in (data or {}).items() if k not in _DROP_KEYS}
    d["dkp_date"] = dkp_date
    d.setdefault("currency", "рублей")
    if not _v(d, "currency"):
        d["currency"] = "рублей"
    if variant == VARIANT_KG:
        d["deal_type"] = kg_deal.DEAL_TYPE_KG
    return d


def today() -> str:
    return datetime.now().strftime("%d.%m.%Y")
