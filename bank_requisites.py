"""bank_requisites.py — модель банковского счёта компании.

Здесь только то, что принадлежит СЧЁТУ: номер, валюта, банк и его реквизиты.
Наименование получателя, оба ИНН и КПП живут в карточке компании (company.py)
и сюда не копируются — в шапке счёта они печатаются рядом с банковскими, но
данными счёта не являются. Какой из двух ИНН попадёт в документ, решает не код,
а сам шаблон: их два, и каждый знает свою юрисдикцию.

Тип счёта — ЯВНОЕ поле account_type, а не догадка «пуст ли банк-корреспондент».
До 04.09.2026 он выводился из пустоты `bank_corr_line1` в трёх местах кода, и
одна лишняя строка в профиле давала документ с ИНН другой юрисдикции, ничем
себя не проявляя. Старая раскладка (bank_corr_line1..3, bank_ben_line1..2,
bank_kpp) разбирается только при разовой миграции журнала — см. from_legacy.
"""
import re

DIRECT_RF = "direct_rf"   # прямой расчётный счёт в российском банке
CORR      = "corr"        # счёт в банке КР, платёж идёт через корреспондента в РФ

ACCOUNT_TYPE_LABELS = {
    DIRECT_RF: "прямой счёт в РФ",
    CORR:      "через банк-корреспондент",
}
ACCOUNT_TYPE_ICONS = {DIRECT_RF: "🇷🇺", CORR: "🇰🇬"}

# ─── Поля ───────────────────────────────────────────────────────────────────
# Структура одинаковая у обоих типов: у счёта всегда есть свой банк с БИК и
# корр. счётом. Разница только в том, что у киргизского банка есть ещё
# банк-корреспондент в РФ, через который проходит платёж.

COMMON_FIELDS = [
    "account_type",
    "account_number",
    "account_currency",
    "bank_name",
    "bank_bic",
    "bank_corr_acc",
    "bank_swift",
]
CORR_ONLY_FIELDS = ["corr_bank_name", "corr_bank_bic", "corr_bank_acc"]
ALL_FIELDS = COMMON_FIELDS + CORR_ONLY_FIELDS

FIELD_LABELS = {
    "account_number":   "Номер счёта",
    "account_currency": "Валюта счёта",
    "bank_name":        "Банк",
    "bank_bic":         "БИК банка",
    "bank_corr_acc":    "Корр. счёт банка",
    "bank_swift":       "SWIFT банка",
    "corr_bank_name":   "Банк-корреспондент",
    "corr_bank_bic":    "БИК корреспондента",
    "corr_bank_acc":    "Корр. счёт корреспондента",
}

FIELD_PLACEHOLDERS = {
    "account_number":   "{{СЧЕТ}}",
    "account_currency": "{{СЧЕТ_ВАЛЮТА}}",
    "bank_name":        "{{БАНК}}",
    "bank_bic":         "{{БАНК_БИК}}",
    "bank_corr_acc":    "{{БАНК_КОРРСЧЕТ}}",
    "bank_swift":       "{{БАНК_SWIFT}}",
    "corr_bank_name":   "{{КОРБАНК}}",
    "corr_bank_bic":    "{{КОРБАНК_БИК}}",
    "corr_bank_acc":    "{{КОРБАНК_КОРРСЧЕТ}}",
}

# SWIFT необязателен: он нужен для карточки контрагенту и валютных переводов,
# но счёт по рублёвому платежу собирается и без него.
OPTIONAL = ("bank_swift",)

REQUIRED_BY_TYPE = {
    DIRECT_RF: ["account_number", "account_currency",
                "bank_name", "bank_bic", "bank_corr_acc"],
    CORR:      ["account_number", "account_currency",
                "bank_name", "bank_bic", "bank_corr_acc",
                "corr_bank_name", "corr_bank_bic", "corr_bank_acc"],
}

# Порядок вопросов при заведении и порядок строк в карточке.
FIELD_ORDER = {
    DIRECT_RF: ["account_number", "account_currency",
                "bank_name", "bank_bic", "bank_corr_acc", "bank_swift"],
    CORR:      ["account_number", "account_currency",
                "bank_name", "bank_bic", "bank_corr_acc", "bank_swift",
                "corr_bank_name", "corr_bank_bic", "corr_bank_acc"],
}


# ─── Определение типа счёта ─────────────────────────────────────────────────

def resolve_account_type(data: dict) -> str:
    """Тип счёта: явное поле, иначе старое правило по банку-корреспонденту.

    Единственное место в проекте, где тип может быть выведен, а не прочитан.
    """
    explicit = str(data.get("account_type") or "").strip().lower()
    if explicit in (DIRECT_RF, CORR):
        return explicit
    legacy_corr = str(data.get("corr_bank_name") or data.get("bank_corr_line1") or "").strip()
    return CORR if legacy_corr else DIRECT_RF


def is_direct(data: dict) -> bool:
    return resolve_account_type(data) == DIRECT_RF


# ─── Нормализация ───────────────────────────────────────────────────────────

def normalize(data: dict) -> dict:
    """Приводит набор полей к модели: только поля счёта, ничего лишнего."""
    def g(*keys) -> str:
        for k in keys:
            v = data.get(k)
            if v is not None and str(v).strip() and str(v).strip() != "None":
                return str(v).strip()
        return ""

    acc_type = resolve_account_type(data)
    out = {
        "account_type":     acc_type,
        "account_number":   g("account_number"),
        "account_currency": g("account_currency") or "RUB",
        "bank_name":        g("bank_name"),
        "bank_bic":         g("bank_bic"),
        "bank_corr_acc":    g("bank_corr_acc"),
        "bank_swift":       g("bank_swift").upper(),
    }
    if acc_type == CORR:
        out["corr_bank_name"] = g("corr_bank_name")
        out["corr_bank_bic"]  = g("corr_bank_bic")
        out["corr_bank_acc"]  = g("corr_bank_acc")
    else:
        # У прямого счёта корреспондента нет. Пустые поля пишем явно, иначе
        # остатки от прошлого типа уедут в документ при смене профиля.
        out["corr_bank_name"] = ""
        out["corr_bank_bic"]  = ""
        out["corr_bank_acc"]  = ""
    return out


def placeholders(data: dict) -> dict:
    """Плейсхолдеры блока реквизитов для подстановки в документ."""
    norm = normalize(data)
    return {ph: norm.get(field, "") for field, ph in FIELD_PLACEHOLDERS.items()}


# ─── Разбор старой раскладки (только для разовой миграции журнала) ──────────

_BEN_LINE2_RE = re.compile(
    r"БИК\D*(?P<bic>\d{6,12}).*?(?:корр|кор)\.?\s*сч\S*\D*(?P<acc>\d{16,25})",
    re.IGNORECASE | re.DOTALL,
)


def parse_ben_line2(line: str) -> tuple[str, str]:
    """«БИК: 124034, корр. счёт: 30111810100000000028» → ("124034", "3011…").

    Поле bank_ben_line2 существовало только потому, что в шаблоне счёта БИК и
    корр. счёт банка получателя были свёрстаны одной строкой в ячейке напротив
    «БИК», а строка «Сч. №» под ней пустовала. Шаблон переверстан, поле ушло.
    """
    line = (line or "").strip()
    if not line:
        return "", ""
    m = _BEN_LINE2_RE.search(line)
    if m:
        return m.group("bic"), m.group("acc")
    nums = re.findall(r"\d{6,25}", line)
    bic = next((n for n in nums if len(n) <= 12), "")
    acc = next((n for n in nums if len(n) >= 16), "")
    return bic, acc


def from_legacy(row: dict) -> dict:
    """Старые шесть полей журнала → поля новой модели.

    В старой раскладке имена врали: у прямого счёта bank_corr_line2/3 держали
    БИК и корр. счёт банка ПОЛУЧАТЕЛЯ, а не корреспондента.
    """
    def g(key) -> str:
        v = row.get(key)
        return "" if v is None else str(v).strip()

    if g("bank_corr_line1"):
        bic, acc = parse_ben_line2(g("bank_ben_line2"))
        return {
            "account_type":   CORR,
            "bank_name":      g("bank_ben_line1"),
            "bank_bic":       bic,
            "bank_corr_acc":  acc,
            "bank_swift":     "",
            "corr_bank_name": g("bank_corr_line1"),
            "corr_bank_bic":  g("bank_corr_line2"),
            "corr_bank_acc":  g("bank_corr_line3"),
        }
    return {
        "account_type":   DIRECT_RF,
        "bank_name":      g("bank_ben_line1"),
        "bank_bic":       g("bank_corr_line2"),
        "bank_corr_acc":  g("bank_corr_line3"),
        "bank_swift":     "",
        "corr_bank_name": "",
        "corr_bank_bic":  "",
        "corr_bank_acc":  "",
    }


# ─── Проверки формата ───────────────────────────────────────────────────────
# Ловим кривой ввод при заведении реквизитов, а не при сборке документа:
# ошибка на этапе выдачи счёта приходит поздно и непонятно откуда.

def validate_field(field: str, value: str, account_type: str = "") -> str:
    """Текст ошибки или "" если значение годится."""
    v = (value or "").strip()
    digits = re.sub(r"\D", "", v)

    if field == "bank_bic":
        if account_type == DIRECT_RF and len(digits) != 9:
            return "БИК российского банка — ровно 9 цифр."
        if not (6 <= len(digits) <= 12):
            return "БИК — от 6 до 12 цифр."
    elif field == "corr_bank_bic":
        if len(digits) != 9:
            return "БИК российского банка-корреспондента — ровно 9 цифр."
    elif field in ("bank_corr_acc", "corr_bank_acc"):
        if len(digits) != 20:
            return "Корр. счёт — ровно 20 цифр."
    elif field == "account_number":
        if account_type == DIRECT_RF and len(digits) != 20:
            return "Расчётный счёт в РФ — ровно 20 цифр."
        if not (10 <= len(digits) <= 25):
            return "Номер счёта — от 10 до 25 цифр."
    elif field == "bank_swift":
        if v and not re.fullmatch(r"[A-Za-z]{4}[A-Za-z]{2}[A-Za-z0-9]{2}([A-Za-z0-9]{3})?", v):
            return "SWIFT — 8 или 11 символов, латиницей."
    elif field == "account_currency":
        if not re.fullmatch(r"[A-Za-z]{3}", v):
            return "Валюта — трёхбуквенный код, например RUB или USD."
    elif field in ("bank_name", "corr_bank_name"):
        if len(v) < 3:
            return "Слишком короткое название."
    return ""


def validate_profile(data: dict) -> list:
    """Проблемы профиля: незаполненные обязательные поля и кривые форматы."""
    norm = normalize(data)
    acc_type = norm["account_type"]
    problems = []
    for field in REQUIRED_BY_TYPE[acc_type]:
        if not norm.get(field, "").strip():
            problems.append(f"не заполнено: {FIELD_LABELS.get(field, field)}")
    for field, value in norm.items():
        if field == "account_type" or not str(value).strip():
            continue
        err = validate_field(field, value, acc_type)
        if err:
            problems.append(f"{FIELD_LABELS.get(field, field)}: {err}")
    return problems


def describe(data: dict) -> str:
    """Карточка реквизитов для меню и подтверждения."""
    norm = normalize(data)
    acc_type = norm["account_type"]
    lines = [f"Тип: {ACCOUNT_TYPE_LABELS[acc_type]}"]
    for field in FIELD_ORDER[acc_type]:
        val = norm.get(field, "")
        if val:
            lines.append(f"{FIELD_LABELS[field]}: {val}")
    return "\n".join(lines)
