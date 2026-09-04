"""company.py — карточка компании: постоянные реквизиты ОсОО «Авто Континент».

Единственное место, где живут наименование, оба ИНН, ОКПО, адрес и данные
директора. Раньше они были вписаны текстом в семь шаблонов и двумя константами
в doc_builder — поменять адрес значило открыть каждый файл и не забыть ни один.

Хранится в настройках бота (таблица settings в SQLite), правится через меню
«🏢 Компания». Здесь только значения по умолчанию — то, что стоит в шаблонах
на сентябрь 2026, чтобы бот работал сразу, до первого захода в меню.

Данные компании НЕ дублируются в банковских профилях и НЕ пишутся в журнал по
каждой сделке: в шапке счёта они печатаются рядом с банковскими, но принадлежат
компании, а не счёту. Какой из двух ИНН печатать, решает не код, а сам шаблон:
их два, и каждый знает свою юрисдикцию.
"""
import logging

logger = logging.getLogger(__name__)

# ─── Поля карточки ──────────────────────────────────────────────────────────
# (ключ настройки, подпись в меню, плейсхолдер, значение по умолчанию)

FIELDS = [
    ("company_name",       "Наименование",        "{{КОМПАНИЯ}}",
     "ОсОО «Авто Континент»"),
    ("company_name_full",  "Полное наименование",  "{{КОМПАНИЯ_ПОЛНОЕ}}", ""),
    ("company_inn",        "ИНН (КР)",             "{{КОМПАНИЯ_ИНН}}",
     "01905202610324"),
    ("company_okpo",       "ОКПО",                 "{{КОМПАНИЯ_ОКПО}}", "34942535"),
    ("company_reg_number", "Номер госрегистрации", "{{КОМПАНИЯ_РЕГНОМЕР}}", ""),
    ("company_reg_date",   "Дата регистрации",     "{{КОМПАНИЯ_ДАТА_РЕГ}}", ""),
    ("company_inn_rf",     "ИНН (РФ)",             "{{КОМПАНИЯ_ИНН_РФ}}", "9909768607"),
    ("company_kpp_rf",     "КПП (РФ)",             "{{КОМПАНИЯ_КПП_РФ}}", "665887001"),
    ("company_address",    "Юридический адрес",    "{{КОМПАНИЯ_АДРЕС}}",
     "Кыргызская Республика, г. Бишкек, Октябрьский район, "
     "ул. Матросова, д. 58, Неж.Пом. 2"),
    ("company_address_fact", "Фактический адрес",  "{{КОМПАНИЯ_АДРЕС_ФАКТ}}", ""),
    ("company_city",       "Город",                "{{КОМПАНИЯ_ГОРОД}}", "г. Бишкек"),
    ("company_phone",      "Телефон",              "{{КОМПАНИЯ_ТЕЛЕФОН}}", ""),
    ("company_email",      "E-mail",               "{{КОМПАНИЯ_EMAIL}}", ""),
    ("director_position",  "Должность руководителя", "{{ДИРЕКТОР_ДОЛЖНОСТЬ}}",
     "Генеральный директор"),
    ("director_position_gen", "Должность (род. падеж)", "{{ДИРЕКТОР_ДОЛЖНОСТЬ_РОДИТ}}",
     "Генерального директора"),
    ("director_name",      "Руководитель",         "{{ДИРЕКТОР}}",
     "Колотовкин Илья Валерьевич"),
    # Родительный падеж — отдельное поле, а не вывод из именительного: правило
    # склонения фамилий с исключениями писать ради одного человека незачем, а
    # ошибка попадёт в текст договора.
    ("director_name_gen",  "Руководитель (род. падеж)", "{{ДИРЕКТОР_РОДИТ}}",
     "Колотовкина Ильи Валерьевича"),
    ("director_initials",  "Руководитель (инициалы)", "{{ДИРЕКТОР_ИНИЦИАЛЫ}}",
     "Колотовкин И.В."),
    ("director_basis",     "Основание полномочий",  "{{ДИРЕКТОР_ОСНОВАНИЕ}}",
     "действующего на основании Устава"),
]

DEFAULTS = {key: default for key, _, _, default in FIELDS}
LABELS = {key: label for key, label, _, _ in FIELDS}
PLACEHOLDERS = {key: ph for key, _, ph, _ in FIELDS}

# Поля, без которых не собрать ни один документ.
REQUIRED = ("company_name", "company_inn", "company_address",
            "director_position", "director_name", "director_initials")


def get(key: str, getter=None) -> str:
    """Значение поля карточки: из настроек, иначе значение по умолчанию."""
    default = DEFAULTS.get(key, "")
    if getter is None:
        return default
    try:
        return (getter(key, default) or default).strip() or default
    except Exception:
        return default


def card(getter=None) -> dict:
    """Вся карточка одним словарём."""
    return {key: get(key, getter) for key in DEFAULTS}


# ─── Собранные строки ───────────────────────────────────────────────────────
# Не вводятся руками: в шаблонах они стоят одной строкой, но склеены из полей
# выше. Хранить их отдельно значило бы завести второе место, где живёт адрес.

def company_line(getter=None) -> str:
    """Строка «Поставщик» в счёте."""
    c = card(getter)
    parts = [c["company_name"], f"ИНН: {c['company_inn']}"]
    if c["company_inn_rf"]:
        parts.append(f"ИНН РФ: {c['company_inn_rf']}")
    if c["company_okpo"]:
        parts.append(f"ОКПО: {c['company_okpo']}")
    parts.append(c["company_address"])
    return ", ".join(p for p in parts if p)


def company_block(getter=None) -> str:
    """Преамбула акта, расписки и отчёта."""
    c = card(getter)
    ids = ", ".join(p for p in (f"ИНН: {c['company_inn']}",
                                f"ОКПО: {c['company_okpo']}" if c["company_okpo"] else "") if p)
    # «в лице Генерального директора Колотовкина Ильи Валерьевича» — здесь и
    # должность, и фамилия стоят в родительном падеже, поэтому должностей в
    # карточке две: именительная для подписи и родительная для этой фразы.
    return (f"{c['company_name']} ({ids}), именуемое в дальнейшем «Агент», "
            f"в лице {c['director_position_gen']} {c['director_name_gen']}, "
            f"{c['director_basis']}")


def placeholders(getter=None) -> dict:
    """Все плейсхолдеры карточки — то, что уходит в каждый документ."""
    c = card(getter)
    out = {PLACEHOLDERS[key]: value for key, value in c.items()}
    out["{{КОМПАНИЯ_СТРОКОЙ}}"] = company_line(getter)
    out["{{КОМПАНИЯ_БЛОК}}"]    = company_block(getter)
    return out


def problems(getter=None) -> list:
    """Незаполненные обязательные поля карточки."""
    c = card(getter)
    return [LABELS[key] for key in REQUIRED if not c.get(key, "").strip()]


def describe(getter=None) -> str:
    """Человекочитаемая карточка для меню."""
    c = card(getter)
    lines = []
    for key, label, _, _ in FIELDS:
        if c.get(key):
            lines.append(f"{label}: {c[key]}")
    return "\n".join(lines)
