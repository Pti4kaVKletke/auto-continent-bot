"""salon.py — карточки салонов РФ (Агент в субагентской сделке).

Субагентская сделка (с 26.09.2026): салон в РФ (ООО или ИП) заключил договор
с клиентом-физлицом и поручает сделку нам. В документах:
  ОсОО «Авто Континент» — Субагент, салон — Агент, клиент — Конечный покупатель.

Салонов немного, и они повторяются, поэтому реквизиты салона живут в
справочнике бота (SQLite, таблица salons, ключ — ИНН), а не в журнале. В
журнале по сделке стоят только три колонки блока «СУБАГЕНТ»:
  «Тип сделки»                — «Прямая» / «Субагент»;
  «Салон (Агент РФ)»          — «ООО «Автомир», ИНН 7701234567» (по ИНН
                                бот находит карточку);
  «Договор салона с клиентом» — «№ 45 от 12.09.2026».

Здесь же — плейсхолдеры {{АГЕНТ_РФ_*}} для шаблонов и «ожидаемый тип» новой
сделки по чату: выбор «через салон» делается кнопкой, и помнит его бот, а не
LLM (см. feedback-llm-state).
"""
import json
import logging
import re
import time

import memory

logger = logging.getLogger(__name__)

DEAL_TYPE_DIRECT   = "Прямая"
DEAL_TYPE_SUBAGENT = "Субагент"

ORG_OOO = "ООО"
ORG_IP  = "ИП"

# (ключ, подпись в меню, только для ООО)
FIELDS = [
    # Торговое название («Казах Авто») — для списков в боте и журнала. В
    # документы не идёт: там только юридическое наименование.
    ("brand",               "Название салона",                  False),
    ("org_form",            "Форма (ООО / ИП)",                 False),
    ("name_short",          "Краткое наименование",             False),
    ("name_full",           "Полное наименование",              True),
    ("inn",                 "ИНН",                              False),
    ("kpp",                 "КПП",                              True),
    ("ogrn",                "ОГРН / ОГРНИП",                    False),
    ("address",             "Юр. адрес / адрес регистрации",    False),
    ("signer_position",     "Подписант: должность",             True),
    ("signer_position_gen", "Должность (род. падеж)",           True),
    ("signer_name",         "Подписант: ФИО",                   False),
    ("signer_name_gen",     "ФИО (род. падеж)",                 True),
    ("signer_basis",        "Основание полномочий",             True),
    ("account",             "Расчётный счёт",                   False),
    ("bank_name",           "Банк",                             False),
    ("bank_bic",            "БИК",                              False),
    ("bank_corr",           "Корр. счёт банка",                 False),
]
KEYS   = [k for k, _, _ in FIELDS]
LABELS = {k: label for k, label, _ in FIELDS}
OOO_ONLY = {k for k, _, only in FIELDS if only}

_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
           "августа", "сентября", "октября", "ноября", "декабря"]


# ─── Тип сделки ─────────────────────────────────────────────────────────────

def is_subagent(data: dict) -> bool:
    """Субагентская ли сделка — по колонке «Тип сделки» (ключ deal_type).
    Пусто (все сделки до 26.09.2026) и «Прямая» — прямая сделка."""
    return str((data or {}).get("deal_type") or "").strip().lower().startswith("субагент")


def normalize_deal_type(value) -> str:
    return DEAL_TYPE_SUBAGENT if str(value or "").strip().lower().startswith("субагент") \
        else DEAL_TYPE_DIRECT


# ─── Карточка ───────────────────────────────────────────────────────────────

def _digits(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def _strip_ip(name: str) -> str:
    return re.sub(r"^(?:ип|индивидуальный\s+предприниматель)\b[\s.,:]*", "",
                  str(name or "").strip(), flags=re.IGNORECASE).strip()


def initials(full_name: str) -> str:
    """«Иванов Иван Иванович» → «Иванов И.И.» (как {{ДИРЕКТОР_ИНИЦИАЛЫ}})."""
    parts = str(full_name or "").split()
    if not parts:
        return ""
    return (parts[0] + " " + "".join(p[0].upper() + "." for p in parts[1:3])).strip()


def normalize(card: dict) -> dict:
    """Приводит карточку к одному виду. Для ИП часть полей выводится сама:
    подписант — сам предприниматель, наименование — «ИП ФИО»."""
    c = {k: str((card or {}).get(k) or "").strip() for k in KEYS}
    c["inn"]  = _digits(c["inn"])
    c["kpp"]  = _digits(c["kpp"])
    c["ogrn"] = _digits(c["ogrn"])
    for k in ("account", "bank_bic", "bank_corr"):
        c[k] = _digits(c[k])

    form = c["org_form"].upper().replace(" ", "")
    if form in ("ИП", "ИНДИВИДУАЛЬНЫЙПРЕДПРИНИМАТЕЛЬ"):
        form = ORG_IP
    elif form:
        form = ORG_OOO
    else:
        # ИНН физлица/ИП — 12 цифр, юрлица — 10.
        form = ORG_IP if len(c["inn"]) == 12 else ORG_OOO
    c["org_form"] = form

    if form == ORG_IP:
        person = _strip_ip(c["signer_name"]) or _strip_ip(c["name_short"]) or _strip_ip(c["name_full"])
        c["signer_name"]         = person
        c["name_short"]          = f"ИП {person}".strip() if person else ""
        c["name_full"]           = f"Индивидуальный предприниматель {person}".strip() if person else ""
        c["signer_position"]     = "Индивидуальный предприниматель"
        c["signer_position_gen"] = ""
        c["signer_name_gen"]     = ""
        c["signer_basis"]        = ""
        c["kpp"]                 = ""
    else:
        if not c["signer_basis"]:
            c["signer_basis"] = "Устава"
    return c


def problems(card: dict) -> list:
    """Незаполненные поля — подписи для сообщения."""
    c = normalize(card)
    out = []
    for k in KEYS:
        if k in ("org_form", "brand"):
            continue
        if c["org_form"] == ORG_IP and k in OOO_ONLY:
            continue
        if not c[k]:
            out.append(LABELS[k])
    if c["inn"] and len(c["inn"]) not in (10, 12):
        out.append("ИНН — должно быть 10 (ООО) или 12 (ИП) цифр")
    return out


def journal_value(card: dict) -> str:
    """Что пишется в колонку журнала «Салон (Агент РФ)»."""
    c = normalize(card)
    if c["brand"]:
        return f"{c['brand']} ({c['name_short']}), ИНН {c['inn']}"
    return f"{c['name_short']}, ИНН {c['inn']}"


def title(card: dict) -> str:
    """Как салон называется в кнопках и сообщениях бота."""
    c = normalize(card)
    if c["brand"] and c["name_short"]:
        return f"{c['brand']} ({c['name_short']})"
    return c["brand"] or c["name_short"] or c["inn"]


def inn_from_journal(value) -> str:
    """ИНН из колонки «Салон (Агент РФ)»: 10 или 12 цифр подряд."""
    runs = re.findall(r"\d{10,12}", str(value or ""))
    for r in runs:
        if len(r) in (10, 12):
            return r
    return ""


def get(inn: str) -> dict:
    inn = _digits(inn)
    card = memory.get_salon(inn) if inn else {}
    return normalize(card) if card else {}


def save(card: dict) -> dict:
    c = normalize(card)
    if not c["inn"]:
        raise ValueError("у салона не указан ИНН")
    memory.save_salon(c["inn"], c)
    logger.info(f"Салон сохранён: {c['name_short']} (ИНН {c['inn']})")
    return c


def list_all() -> list:
    return [(inn, normalize(card)) for inn, card in memory.list_salons()]


def card_for_deal(data: dict) -> dict:
    """Карточка салона сделки по колонке «Салон (Агент РФ)». Нет — {}."""
    return get(inn_from_journal((data or {}).get("salon")))


# ─── Договор салона с клиентом ──────────────────────────────────────────────

def parse_client_contract(value) -> tuple:
    """«№ 45 от 12.09.2026» → («45», «12.09.2026»). Чего нет — пустая строка."""
    text = " ".join(str(value or "").split())
    m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", text)
    date = f"{int(m.group(1)):02d}.{int(m.group(2)):02d}.{m.group(3)}" if m else ""
    head = text[:m.start()] if m else text
    head = re.sub(r"\bот\s*$", "", head.strip(), flags=re.IGNORECASE).strip()
    number = re.sub(r"^(?:договор\s*)?№\s*", "", head, flags=re.IGNORECASE).strip(" ,;")
    return number, date


def format_client_contract(number: str, date: str) -> str:
    return f"№ {number} от {date}".strip()


def date_words(date: str) -> str:
    """«12.09.2026» → «"12" сентября 2026 г.» в ёлочках."""
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})$", str(date or "").strip())
    if not m:
        return ""
    return f"«{m.group(1)}» {_MONTHS[int(m.group(2)) - 1]} {m.group(3)} г."


# ─── Плейсхолдеры ───────────────────────────────────────────────────────────

def full_details(card: dict) -> str:
    """{{АГЕНТ_РФ_ПОЛНЫЕ_ДАННЫЕ}} — середина фразы «…и <это>, именуемое(ый)
    в дальнейшем «Агент»…», без точки в конце."""
    c = normalize(card)
    if c["org_form"] == ORG_IP:
        return (f"Индивидуальный предприниматель {c['signer_name']} "
                f"(ИНН {c['inn']}, ОГРНИП {c['ogrn']})")
    basis = c["signer_basis"]
    if not re.match(r"^действующ", basis, flags=re.IGNORECASE):
        basis = f"действующего на основании {basis}"
    return (f"{c['name_full']} (ИНН {c['inn']}, КПП {c['kpp']}, ОГРН {c['ogrn']}), "
            f"в лице {c['signer_position_gen']} {c['signer_name_gen']}, {basis}")


def requisites_line(card: dict) -> str:
    """{{АГЕНТ_РФ_РЕКВИЗИТЫ}} — строка «Плательщик» в счёте."""
    c = normalize(card)
    parts = [c["name_short"], f"ИНН {c['inn']}"]
    if c["kpp"]:
        parts.append(f"КПП {c['kpp']}")
    parts.append(c["address"])
    return ", ".join(p for p in parts if p)


def placeholders(card: dict) -> dict:
    c = normalize(card)
    return {
        "{{АГЕНТ_РФ_ПОЛНЫЕ_ДАННЫЕ}}":        full_details(c),
        "{{АГЕНТ_РФ_НАИМЕНОВАНИЕ}}":         c["name_short"],
        "{{АГЕНТ_РФ_ИНН_КПП}}":              f"{c['inn']} / {c['kpp']}" if c["kpp"] else c["inn"],
        "{{АГЕНТ_РФ_ОГРН}}":                 c["ogrn"],
        "{{АГЕНТ_РФ_АДРЕС}}":                c["address"],
        "{{АГЕНТ_РФ_СЧЕТ}}":                 c["account"],
        "{{АГЕНТ_РФ_БАНК_СТРОКА1}}":         f"Банк: {c['bank_name']}" if c["bank_name"] else "",
        "{{АГЕНТ_РФ_БАНК_СТРОКА2}}":         f"БИК: {c['bank_bic']}" if c["bank_bic"] else "",
        "{{АГЕНТ_РФ_БАНК_СТРОКА3}}":         f"к/с: {c['bank_corr']}" if c["bank_corr"] else "",
        "{{АГЕНТ_РФ_БАНК_СТРОКА4}}":         "",
        "{{АГЕНТ_РФ_ПОДПИСАНТ_ДОЛЖНОСТЬ}}":  c["signer_position"],
        "{{АГЕНТ_РФ_ИНИЦИАЛЫ}}":             initials(c["signer_name"]),
        "{{АГЕНТ_РФ_РЕКВИЗИТЫ}}":            requisites_line(c),
    }


def describe(card: dict) -> str:
    """Карточка для показа в чате (обычный текст, без разметки)."""
    c = normalize(card)
    lines = []
    for k, label, only in FIELDS:
        if c["org_form"] == ORG_IP and only:
            continue
        lines.append(f"{label}: {c[k] or '—'}")
    return "\n".join(lines)


# ─── «Ожидаемый тип» новой сделки по чату ────────────────────────────────────
# Кнопка «Новая сделка → Через салон РФ → <салон>» запоминает выбор здесь.
# create_contract читает его и сам проставляет тип и салон, а системный
# промпт напоминает LLM спросить номер и дату договора салона с клиентом.
# Живёт сутки: брошенный сценарий не должен превратить в субагентскую
# случайную прямую сделку через неделю.

PENDING_TTL_SEC = 24 * 3600


def _pending_key(chat_id) -> str:
    return f"new_deal_salon:{chat_id}"


def set_pending(chat_id, inn: str) -> None:
    memory.set_setting(_pending_key(chat_id),
                       json.dumps({"inn": _digits(inn), "ts": time.time()}))


def get_pending(chat_id) -> dict:
    """Карточка салона, выбранного для новой сделки в этом чате, или {}."""
    if not chat_id:
        return {}
    raw = memory.get_setting(_pending_key(chat_id)) or ""
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except Exception:
        return {}
    if time.time() - float(val.get("ts") or 0) > PENDING_TTL_SEC:
        clear_pending(chat_id)
        return {}
    return get(val.get("inn", ""))


def clear_pending(chat_id) -> None:
    if chat_id:
        memory.set_setting(_pending_key(chat_id), "")
