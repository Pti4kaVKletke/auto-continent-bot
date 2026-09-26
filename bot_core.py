"""
bot_core.py — общие объекты и помощники бота: агент, бэкап, отправка результата,
клавиатуры меню, проверка доступа. Вынесено из bot.py 25.09.2026, чтобы модули
кнопок (cb_*.py) могли их импортировать, не импортируя bot.py (он запускается
как __main__ — повторный импорт создал бы второй экземпляр агента).
"""

import os
import sys
import logging
import asyncio
import random
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import ContextTypes
from telegram.error import BadRequest
from agent import (
    _parse_payments,
    _calc_total_amount,
    _fmt_money,
    _resolve_period,
    _in_period,
    _parse_date_ddmmyyyy,
)
import memory
import company_ui
import settings_service
from backup_service import BackupService

AGENT_VERSION = os.environ.get("AGENT_VERSION", "v1")
if AGENT_VERSION == "v2":
    from agent_v2 import DocumentAgent
    logging.getLogger(__name__).info("Используется agent_v2")
else:
    from agent import DocumentAgent
    logging.getLogger(__name__).info("Используется agent_v1")

# Лог — в stdout: Railway помечает всё из stderr как «error», и настоящие
# ошибки терялись среди обычных INFO-строк.
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO,
                    stream=sys.stdout)
# httpx на уровне INFO пишет каждый запрос с полным адресом, а в адресе
# запросов к Telegram стоит токен бота. Оставляем только предупреждения и ошибки.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

agent  = DocumentAgent()
backup = BackupService()


# ─── AWAITING-ФЛАГИ ──────────────────────────────────────────────────────────
# Единый список всех состояний "жду от пользователя ввода". Клик по любой
# кнопке или команда /menu, /start означают переход в новое состояние, а не
# продолжение прошлого ожидания → сбрасываем всё разом. Обработчик, который
# сам ставит awaiting-флаг, установит его ПОСЛЕ этого сброса — порядок верный.
AWAITING_FLAGS = (
    "awaiting_search",
    "awaiting_stats_dates",
    "awaiting_deals_dates",
    "awaiting_deal_date",
    "awaiting_dkp_date",
    "awaiting_scan_for_deal",
    "awaiting_scan_folder_id",
    "awaiting_scan_for_existing",
    "awaiting_edit_deal",
    "awaiting_payment_for_deal",
    "awaiting_doc_to_sign",
    # После «📄 Новая сделка» файлы сразу читаются как документы сделки, без
    # вопроса «что с ним делать». Значение — сколько файлов уже принято:
    # первый читается «с нуля», следующие дополняют собранные данные.
    # Сбрасывается любым кликом по кнопке (как и прочие awaiting-флаги).
    "awaiting_new_deal_docs",
)

# Дополнительные "хвосты" — временные данные, привязанные к awaiting-состояниям
# как парные значения. Сбрасываем вместе с флагами.
# ⚠️  last_scan_* сюда НЕ входят: они читаются в самом handle_callback (ветка
# scan_route), сброс в начале колбэка их бы обнулил.
AWAITING_TAILS = (
    "pending_existing_filepath",
    "pending_existing_filename",
)

# ⚠️  awaiting_doc_field сюда тоже НЕ входит: он живёт между показом просьбы
# и нажатием кнопки быстрого выбора даты, а _clear_awaiting_flags() вызывается
# в начале каждого handle_callback — сброс убил бы его ровно в этот момент.

# ⚠️  pending_deal_date сюда НЕ входит: дата договора живёт между двумя
# нажатиями кнопок (сначала дата договора, затем дата ДКП), а
# _clear_awaiting_flags() вызывается в начале каждого handle_callback —
# сброс убил бы её ровно в момент выбора даты ДКП. Та же логика, что у
# copy_pending / overpay_pending.


def _clear_awaiting_flags(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сбрасывает все awaiting_* флаги и связанные с ними временные данные.
    Не трогает current_deal / new_deal_started (это не флаги ожидания ввода)."""
    for key in AWAITING_FLAGS + AWAITING_TAILS:
        context.user_data.pop(key, None)


# ─── ДЕТЕКТОР КОМАНД ─────────────────────────────────────────────────────────
# Защита от галлюцинации Haiku в v2 на свободных текстовых командах.
# Если пользователь пишет "добавь оплату N к сделке NNN" — LLM иногда просто
# сочиняет ответ вместо реального вызова add_payment. Здесь мы явно детектим
# намерение и через force_tool обязываем LLM вызвать конкретный инструмент.

_DEAL_NUM_RE = re.compile(r"\b\d{9}\b")
_PAY_ADD_RE  = re.compile(
    r"(?:добав\S*|запиш\S*|внес\S*|провед\S*)\s+.*?(?:оплат|плат|поступлен)"
    r"|(?:пришл\S*|поступил\S*)\s+.*?\d",
    re.IGNORECASE,
)
_PAY_DEL_RE  = re.compile(
    r"(?:удал\S*|убер\S*|отмен\S*)\s+.*?(?:оплат|плат|поступлен)",
    re.IGNORECASE,
)

# ─── КОНТРОЛЬ ДОСТУПА ────────────────────────────────────────────────────────

ALLOWED_CHAT_ID = int(os.environ.get("ALLOWED_CHAT_ID", "268470621"))

def _get_allowed_chat_ids() -> list[int]:
    """Список всех разрешённых chat_id — используется для рассылки
    административных уведомлений (алерты бэкапа и т.п.)."""
    return [
        int(x.strip())
        for x in os.environ.get("ALLOWED_CHAT_IDS", str(ALLOWED_CHAT_ID)).split(",")
        if x.strip().lstrip("-").isdigit()
    ]

async def check_access(update: Update) -> bool:
    allowed = set(_get_allowed_chat_ids())
    return update.effective_chat.id in allowed


# ─── TYPING INDICATOR ────────────────────────────────────────────────────────

async def typing_while(chat_id, context: ContextTypes.DEFAULT_TYPE, coro):
    """Показывает анимацию печати пока выполняется coro."""
    stop = asyncio.Event()

    async def _keep_typing():
        while not stop.is_set():
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                pass
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(_keep_typing())
    try:
        result = await coro
    finally:
        stop.set()
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass
    return result


# ─── ГЛАВНОЕ МЕНЮ ────────────────────────────────────────────────────────────

_GREETINGS = [
    "👋 Привет\\! Я готова к работе\\.",
    "🤝 На связи\\! Чем могу помочь\\?",
    "✨ Готова\\! Выбери действие или напиши что нужно\\.",
    "🚗 На месте\\! Что делаем\\?",
    "💼 Готова к работе\\. Выбери действие\\.",
    "👌 Здесь\\! Чем займёмся\\?",
]

def get_menu_text() -> str:
    return random.choice(_GREETINGS)

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📄 Новая сделка",    callback_data="menu:new_deal"),
            InlineKeyboardButton("🔍 Найти сделку",    callback_data="menu:find_deal"),
        ],
        [
            InlineKeyboardButton("📋 Сделки",          callback_data="menu:deals"),
            InlineKeyboardButton("📊 Статистика",      callback_data="menu:stats"),
        ],
        [
            InlineKeyboardButton("🏦 Реквизиты",       callback_data="menu:bank_profiles"),
            InlineKeyboardButton("🏢 Компания",        callback_data="menu:company"),
        ],
        [
            InlineKeyboardButton("🏬 Салоны",          callback_data="menu:salons"),
        ],
        [
            InlineKeyboardButton("🧠 Память",          callback_data="menu:memory"),
            InlineKeyboardButton("💾 Бэкапы",          callback_data="menu:backup"),
        ],
        [
            InlineKeyboardButton("✍️ Подписать",       callback_data="menu:sign_doc"),
            InlineKeyboardButton("⚙️ Настройки",       callback_data="menu:settings"),
        ],
    ])


def settings_menu_keyboard():
    """Корневое подменю настроек: по кнопке на каждую настройку из
    settings_service.SETTINGS. В подписи кнопки — текущее значение."""
    kb = []
    for i, s in enumerate(settings_service.SETTINGS):
        current = settings_service.get_current_label(s)
        label = f"{s['label']}: {current}"
        if len(label) > 60:
            label = label[:57] + "…"
        kb.append([InlineKeyboardButton(label, callback_data=f"settings:{i}")])
    kb.append([InlineKeyboardButton("◀️ Меню", callback_data="menu:back")])
    return InlineKeyboardMarkup(kb)


def setting_options_keyboard(setting_index: int):
    """Подменю выбора значения для конкретной настройки."""
    setting = settings_service.get_setting_by_index(setting_index)
    if not setting:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ К настройкам", callback_data="menu:settings")
        ]])
    current = settings_service.get_current_value(setting)
    kb = []
    for j, opt in enumerate(settings_service.get_options(setting)):
        marker = "✅ " if opt["value"] == current else "○ "
        kb.append([InlineKeyboardButton(
            marker + opt["label"],
            callback_data=f"settings:{setting_index}:{j}",
        )])
    kb.append([InlineKeyboardButton("◀️ К настройкам", callback_data="menu:settings")])
    kb.append([InlineKeyboardButton("◀️ Меню",         callback_data="menu:back")])
    return InlineKeyboardMarkup(kb)


# ── Пересборка комплектов по старым сделкам (разовая миграция, 10.09.2026) ──

def _md_escape(text: str) -> str:
    """Экранирует спецсимволы старого Markdown (_,*,`,[) в тексте, который
    может прийти откуда угодно — сообщение исключения, текст ошибки Drive,
    содержимое ячейки журнала — и попасть в сообщение с parse_mode="Markdown".
    Непарный спецсимвол в таком тексте роняет отправку целиком ("Can't parse
    entities") — ловили это на /regen_docs, когда несколько причин пропуска
    в одном отчёте в сумме давали нечётное число подчёркиваний.
    """
    text = str(text)
    for ch in ("\\", "_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


async def _safe_reply(message, text, **kwargs):
    """reply_text с фоллбэком на обычный текст, если Markdown не распарсился.

    Экранирование (_md_escape) закрывает известные места, но не гарантирует
    вообще все — а уронить итоговый отчёт после долгого прогона на кривой
    сущности хуже, чем показать его без форматирования.
    """
    try:
        await message.reply_text(text, **kwargs)
    except BadRequest as e:
        if "parse" in str(e).lower() or "entit" in str(e).lower():
            kwargs.pop("parse_mode", None)
            await message.reply_text(text, **kwargs)
        else:
            raise


def _format_regen_report(summary: dict) -> str:
    """Текстовый отчёт по итогам agent.regenerate_missing_docs_impl."""
    total           = summary["total"]
    base_ok         = summary["base_ok"]
    base_error      = summary["base_error"]
    closing_ok      = summary["closing_ok"]
    closing_skipped = summary["closing_skipped"]
    closing_error   = summary["closing_error"]

    closing_full    = sum(1 for v in closing_ok.values() if len(v) == 3)
    closing_partial = sum(1 for v in closing_ok.values() if 0 < len(v) < 3)

    lines = [
        f"🧾 *Пересборка завершена* — сделок в прогоне: {total}\n",
        f"✅ Базовый пакет (АГ + ДКП + Счёт): {len(base_ok)}",
    ]
    if base_error:
        lines.append(f"❌ Ошибка базового пакета: {len(base_error)}")
    lines.append(f"✅ Закрывающие полностью (расписка+акт+отчёт): {closing_full}")
    if closing_partial:
        lines.append(f"◐ Закрывающие частично (упёрлись на середине): {closing_partial}")
    if closing_skipped:
        lines.append(f"⏭ Закрывающие не делали — не хватает данных: {len(closing_skipped)}")
    if closing_error:
        lines.append(f"❌ Ошибка при сборке закрывающих: {len(closing_error)}")

    def _dump(title, d, limit=15):
        out = [f"\n*{title}:*"]
        for num, msg in list(d.items())[:limit]:
            out.append(f"  {_md_escape(num)}: {_md_escape(str(msg)[:150])}")
        if len(d) > limit:
            out.append(f"  …и ещё {len(d) - limit}")
        return out

    if base_error:
        lines += _dump("Ошибки базового пакета", base_error)
    if closing_skipped:
        lines += _dump("Закрывающие пропущены", closing_skipped)
    if closing_error:
        lines += _dump("Ошибки закрывающих", closing_error)

    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3800] + "\n\n_(отчёт обрезан — подробности в логах Railway)_"
    return text


_REGEN_DEFAULT_BATCH = 10  # безопасный размер прогона по умолчанию, см. /regen_docs


# ─── ОТПРАВКА РЕЗУЛЬТАТА ─────────────────────────────────────────────────────

async def send_result(message, result: dict, context=None, chat_id=None):
    """Отправляет файлы и текст результата."""
    # Пробрасываем данные ожидающего действия в user_data:
    # add_payment_impl при переоплате возвращает overpay_pending — bot.py
    # сохранит его для callback "payforce:confirm" (кнопка «Всё равно добавить»).
    if context is not None and result.get("overpay_pending"):
        context.user_data["overpay_pending"] = result["overpay_pending"]

    # copy_deal возвращает copy_pending — bot.py сохранит для callback "copy:ok",
    # где пользователь подтвердит начало сбора недостающих полей.
    if context is not None and result.get("copy_pending"):
        context.user_data["copy_pending"] = result["copy_pending"]

    if result.get("files"):
        for f_info in result["files"]:
            try:
                with open(f_info["file"], "rb") as f:
                    link = f_info.get("drive_link", "")
                    caption = f"☁️ {link}" if link else "☁️ (не загружено на Drive)"
                    await message.reply_document(
                        document=f,
                        filename=f_info["filename"],
                        caption=caption,
                    )
            except FileNotFoundError:
                logger.error(f"Файл не найден при отправке: {f_info.get('file')}")

    if result.get("text"):
        reply_markup = None
        if result.get("buttons"):
            keyboard = [
                [InlineKeyboardButton(b["text"], callback_data=b["callback_data"])]
                for b in result["buttons"]
            ]
            # Добавляем кнопку назад если её нет среди кнопок агента
            has_back = any("menu:back" in b["callback_data"] or "◀️" in b["text"]
                           for b in result["buttons"])
            if not has_back:
                keyboard.append([InlineKeyboardButton("◀️ Меню", callback_data="menu:back")])
            reply_markup = InlineKeyboardMarkup(keyboard)
        # Markdown включаем с откатом: тексты бота размечены (*жирный*, `код`),
        # а тексты LLM — нет, и случайный `_` или `*` в ФИО ломает разбор.
        # При ошибке разметки отправляем как есть, лишь бы сообщение дошло.
        try:
            await message.reply_text(result["text"], parse_mode="Markdown",
                                     reply_markup=reply_markup)
        except Exception as e:
            logger.warning(f"Markdown не разобран, отправляю без разметки: {e}")
            await message.reply_text(result["text"], reply_markup=reply_markup)



# ─── ОБРАБОТКА CALLBACK-КНОПОК ───────────────────────────────────────────────

async def ask_dkp_date(message, deal_date: str):
    """
    Второй шаг выбора дат при создании комплекта: дата ДКП.

    ДКП нередко подписывают раньше агентского договора, поэтому дату спрашиваем
    отдельно. Первой кнопкой — «та же, что договор»: это самый частый случай,
    Александре хватит одного нажатия.

    Вызывается ботом сразу после выбора даты договора (кнопкой или текстом),
    а не инструментом LLM — чтобы шаг нельзя было пропустить.
    """
    from datetime import datetime

    today_str = datetime.now().strftime("%d.%m.%Y")
    kb = [[InlineKeyboardButton(f"✅ Та же, что договор ({deal_date})",
                                callback_data="dkp_date:__same__")]]
    if today_str != deal_date:
        kb.append([InlineKeyboardButton(f"📅 Сегодня ({today_str})",
                                        callback_data=f"dkp_date:{today_str}")])
    kb.append([InlineKeyboardButton("✏️ Другая дата", callback_data="dkp_date:__custom__")])

    await message.reply_text(
        f"📅 Дата договора: *{deal_date}*\n\nТеперь дата ДКП:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ─── СПИСОК СДЕЛОК ЗА ПЕРИОД ────────────────────────────────────────────────
# Период считаем тем же _resolve_period, что и статистика: «месяц» в списке и
# «месяц» в статистике обязаны означать одно и то же. Фильтр — по «Дате
# договора», не по датам платежей.
# Статус сканов берём из колонки журнала, БЕЗ запроса к Drive: на странице 5
# сделок, это были бы 5 запросов и заметная задержка. Точное число, как и
# раньше, пересчитывается при открытии самой сделки (dealaction:*:menu).

DEALS_PER_PAGE = 5

# Ряды кнопок подменю срезов (код периода → подпись)
# «Сегодня» и «вчера» убраны сознательно (Илья, 04.09.2026): для списка
# сделок такие срезы почти всегда пустые, полезный минимум — неделя.
# Сами периоды в _resolve_period остались, статистика ими пользуется.
_DEAL_PERIOD_ROWS = [
    [("week", "📅 Неделя"),          ("month", "📅 Месяц")],
    [("last_month", "📅 Пр. месяц"), ("quarter", "📅 Квартал")],
    [("year", "📅 Год"),             ("all", "📊 Все")],
]

_DEAL_STATUS_ICONS = {
    "черновик":     "🔵",
    "активна":      "🟢",
    "ждём доплату": "⏳",
    "завершена":    "✅",
    "отменена":     "❌",
}

# Фильтры по статусу: код → (подпись, набор статусов). None = все, кроме
# отменённых — отменённые видны только по явному фильтру.
_DEAL_STATUS_FILTERS = [
    ("all",       "Все",          None),
    ("active",    "Активные",     ("активна", "ждём доплату")),
    ("pending",   "Ждём доплату", ("ждём доплату",)),
    ("done",      "Завершённые",  ("завершена",)),
    ("cancelled", "Отменённые",   ("отменена",)),
]

_SCAN_COUNT_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)")


def deals_menu_keyboard():
    """Подменю срезов: «Активные» первой строкой (как раньше, в один клик
    от главного меню), дальше периоды."""
    kb = [[InlineKeyboardButton("✅ Активные", callback_data="deals:all:active:0")]]
    for row in _DEAL_PERIOD_ROWS:
        kb.append([InlineKeyboardButton(title, callback_data=f"deals:{code}:all:0")
                   for code, title in row])
    kb.append([InlineKeyboardButton("📅 Свой период", callback_data="deals:ask_custom")])
    kb.append([InlineKeyboardButton("🔄 Обновить сканы", callback_data="rescan:menu")])
    kb.append([InlineKeyboardButton("◀️ Меню", callback_data="menu:back")])
    return InlineKeyboardMarkup(kb)


def rescan_menu_keyboard():
    """За какой срез пересчитывать сканы. Сетка та же, что у списка сделок:
    «срез» должен означать одно и то же в обоих меню. «📊 Все» = весь журнал."""
    kb = [[InlineKeyboardButton("✅ Активные", callback_data="rescan:all:active")]]
    for row in _DEAL_PERIOD_ROWS:
        kb.append([InlineKeyboardButton(title, callback_data=f"rescan:{code}:all")
                   for code, title in row])
    kb.append([InlineKeyboardButton("◀️ Назад", callback_data="menu:deals")])
    return InlineKeyboardMarkup(kb)


def _plural_deals(n: int) -> str:
    """1 сделка / 2 сделки / 5 сделок."""
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return "сделок"
    return {1: "сделка", 2: "сделки", 3: "сделки", 4: "сделки"}.get(n % 10, "сделок")


def _deal_status_of(d) -> str:
    return (d.get("Статус") or "").strip().lower()


def _deal_scans_complete(d) -> bool:
    """True, если в колонке «Сканы» полный комплект (например «5/5 ✓»)."""
    m = _SCAN_COUNT_RE.match(str(d.get("Сканы") or ""))
    return bool(m) and m.group(1) == m.group(2)


def _select_deals(deals, period, status_code, date_from="", date_to=""):
    """Отбирает сделки среза: сначала период (по «Дате договора»), потом статус.
    Возвращает (rows, present, label, status_code) — status_code уточнён, если
    пришёл неизвестный. Общая точка для списка и для обновления сканов, чтобы
    «срез» в обоих местах означал одно и то же."""
    from datetime import date as _date

    df, dt, label = _resolve_period(period, date_from, date_to)

    # Порядок важен: набор кнопок-фильтров зависит от того, какие статусы
    # вообще встретились в периоде.
    if df is None and dt is None:
        in_period = list(deals)
    else:
        in_period = [
            d for d in deals
            if _in_period(_parse_date_ddmmyyyy(d.get("Дата договора")), df, dt)
        ]
    present = {_deal_status_of(d) for d in in_period}

    allowed = None
    known = False
    for code, _lbl, statuses in _DEAL_STATUS_FILTERS:
        if code == status_code:
            allowed, known = statuses, True
            break
    if not known:
        status_code, allowed = "all", None

    if allowed is None:
        rows = [d for d in in_period if _deal_status_of(d) != "отменена"]
    else:
        rows = [d for d in in_period if _deal_status_of(d) in allowed]

    rows.sort(key=lambda d: _parse_date_ddmmyyyy(d.get("Дата договора")) or _date.min,
              reverse=True)
    return rows, present, label, status_code


def _build_deals_view(deals, period, status_code, page, date_from="", date_to="",
                      note=""):
    """Собирает текст и клавиатуру списка сделок.
    note — служебная строка под заголовком (итог обновления сканов).
    Возвращает (text, InlineKeyboardMarkup, page) — page уточнён по факту."""
    rows, present, label, status_code = _select_deals(
        deals, period, status_code, date_from, date_to)

    total = len(rows)
    total_pages = max(1, (total + DEALS_PER_PAGE - 1) // DEALS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    chunk = rows[page * DEALS_PER_PAGE:(page + 1) * DEALS_PER_PAGE]

    if period == "all" and status_code == "active":
        title = "✅ *Активные сделки*"
    else:
        title = f"📋 *Сделки · {label}*"
    head = f"{title} · {total} шт"
    if total_pages > 1:
        head += f" · стр. {page + 1}/{total_pages}"

    lines = [head]
    if note:
        lines.append(note)
    lines.append("")

    for d in chunk:
        num   = d.get("Номер договора", "—")
        fio   = d.get("buyer_initials") or d.get("buyer_name") or "—"
        car   = d.get("car_model") or "—"
        vin   = str(d.get("car_vin") or "")
        st    = _deal_status_of(d) or "—"
        icon  = _DEAL_STATUS_ICONS.get(st, "▫️")
        ddate = d.get("Дата договора") or "—"
        curr  = (d.get("currency") or "руб").strip() or "руб"

        total_amt = _calc_total_amount(d)
        received  = sum(p["amount"] for p in _parse_payments(d.get("Платежи", "")))
        rest      = total_amt - received
        money = f"💰 {_fmt_money(total_amt)} {curr}"
        money += " · оплачено" if rest <= 0.01 else f" · остаток *{_fmt_money(rest)}*"

        scans = str(d.get("Сканы") or "").strip() or "нет"
        if len(scans) > 45:
            scans = scans[:44] + "…"

        vin_tail = f" · `...{vin[-6:]}`" if vin else ""
        lines.append(
            f"📄 `{num}` · *{fio}*\n"
            f"   🚗 {car}{vin_tail}\n"
            f"   {icon} {st} · {ddate}\n"
            f"   {money}\n"
            f"   📎 Сканы: {scans}\n"
        )

    if not rows:
        lines.append("_Сделок за этот период нет._")
    else:
        # Итог по периоду. Отменённые в деньги не берём — как в статистике.
        by_curr = {}
        for d in rows:
            if _deal_status_of(d) == "отменена":
                continue
            c = (d.get("currency") or "руб").strip() or "руб"
            t = _calc_total_amount(d)
            r = sum(p["amount"] for p in _parse_payments(d.get("Платежи", "")))
            acc = by_curr.setdefault(c, [0.0, 0.0])
            acc[0] += t
            acc[1] += r

        lines.append("─" * 16)
        items = sorted(by_curr.items())
        if items:
            sums = ", ".join(f"*{_fmt_money(v[0])} {c}*" for c, v in items)
            lines.append(f"💼 Итого за период: {total} {_plural_deals(total)} на {sums}")
            got  = ", ".join(f"{_fmt_money(v[1])} {c}" for c, v in items)
            left = ", ".join(f"{_fmt_money(v[0] - v[1])} {c}"
                             for c, v in items if v[0] - v[1] > 0.01)
            line = f"📥 Получено {got}"
            if left:
                line += f" · ⏳ остаток {left}"
            lines.append(line)
        else:
            lines.append(f"💼 Итого за период: {total} {_plural_deals(total)}")
        lines.append(f"📎 Комплект сканов собран: "
                     f"{sum(1 for d in rows if _deal_scans_complete(d))} из {total}")

    # ── Клавиатура ───────────────────────────────────────────────────────
    kb = []
    for d in chunk:
        num  = d.get("Номер договора", "—")
        init = d.get("buyer_initials") or d.get("buyer_name") or "—"
        kb.append([InlineKeyboardButton(f"📄 {num} · {init}"[:32],
                                        callback_data=f"dealaction:{num}:menu")])

    filt = []
    for code, lbl, _st in _DEAL_STATUS_FILTERS:
        if code == "cancelled" and "отменена" not in present:
            continue
        mark = "• " if code == status_code else ""
        filt.append(InlineKeyboardButton(f"{mark}{lbl}",
                                         callback_data=f"deals:{period}:{code}:0"))
    for i in range(0, len(filt), 3):   # больше 3 кнопок в ряд не влезает по ширине
        kb.append(filt[i:i + 3])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "← Пред.", callback_data=f"deals:{period}:{status_code}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(
            "След. →", callback_data=f"deals:{period}:{status_code}:{page + 1}"))
    if nav:
        kb.append(nav)

    kb.append([InlineKeyboardButton("◀️ К срезам", callback_data="menu:deals"),
               InlineKeyboardButton("◀️ Меню",     callback_data="menu:back")])

    text = "\n".join(lines)
    if len(text) > 3900:      # лимит сообщения Telegram ~4096
        text = text[:3800] + "\n\n_(текст обрезан)_"
    return text, InlineKeyboardMarkup(kb), page


async def show_deals_list(query, context, period, status_code, page,
                          date_from="", date_to=""):
    """Рисует список сделок на месте текущего сообщения."""
    back_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ К срезам", callback_data="menu:deals"),
    ]])
    try:
        deals = await agent.sheets.get_all_deals()
    except Exception as e:
        logger.error(f"Ошибка получения сделок для списка: {e}", exc_info=True)
        await query.edit_message_text(f"⚠️ Ошибка получения сделок: {e}",
                                      reply_markup=back_kb)
        return

    text, kb, page = _build_deals_view(deals, period, status_code, page,
                                       date_from, date_to)
    # Запоминаем экран, чтобы «◀️ Назад» из карточки сделки вернула сюда же
    context.user_data["last_deals_view"] = f"deals:{period}:{status_code}:{page}"
    try:
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        logger.warning(f"Не удалось отредактировать список сделок, шлём новым: {e}")
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


def _make_card_sender(update, context):
    """Отдаёт карточку предприятия: PDF плюс тот же текст в чат.

    Текст — чтобы переслать в переписку с телефона, файл — чтобы приложить
    к письму. Если LibreOffice не отработал, уходит DOCX: карточка без PDF
    полезнее, чем сообщение об ошибке.
    """
    async def _send(profile_name: str):
        message = update.callback_query.message
        try:
            path = await agent.builder.build_company_card(
                memory.get_bank_profile(profile_name) or {},
                profile_name,
                getter=memory.get_setting,
            )
            if os.environ.get("SKIP_PDF", "0") != "1":
                pdf = await agent.builder.convert_to_pdf(path)
                if pdf:
                    path = pdf
            with open(path, "rb") as f:
                await message.reply_document(f, filename=os.path.basename(path))
            await message.reply_text(
                company_ui.contractor_card_text(profile_name),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Не удалось собрать карточку предприятия: {e}", exc_info=True)
            await message.reply_text(f"❌ Не удалось собрать карточку: {e}")
    return _send


# ─── ЗАКРЫВАЮЩИЕ ДОКУМЕНТЫ (акт / расписка / отчёт) ──────────────────────────

_DOC_IMPL_LABELS = {
    "build_act":     "акт",
    "build_receipt": "расписку",
    "build_report":  "отчёт агента",
}


async def run_doc_impl(update, context, message, num: str, action: str,
                       show_buttons: bool = True):
    """
    Формирует акт / расписку / отчёт агента и отправляет результат в чат.

    Вынесено из handle_callback: вызывается с отдельных кнопок сделки, из
    кнопки «Всё» (docmenu:*:full) и при автоповторе после ввода недостающего
    поля. message — сообщение, в ответ на которое отправляются файлы и статусы.

    Если *_impl вернул `needs` (не заполнены «Дата расчёта» или «Фактический
    курс»), запоминаем в user_data, какой документ просили, и показываем
    кнопки/просьбу ввести значение. Дальше apply_doc_field запишет ответ в
    журнал и вызовет эту же функцию повторно — возврат к документу не зависит
    от того, помнит ли о нём LLM.

    show_buttons=False — внутри пакета документов: кнопки «следующий шаг» и
    «К сделке» там только мусорят (предлагают то, что уже сформировано, или
    повторяются после каждого файла). Итоговая кнопка приходит одна, в конце
    пакета. На сообщения об ошибке флаг не влияет: там кнопки — часть
    диалога, ими вводят дату расчёта.

    Возвращает "ok" — документ выдан, "needs" — не хватает данных и выставлено
    ожидание ответа, "error" — прочая причина (не оплачено, неполные поля).
    Ошибка не исключение: вызывающий решает, продолжать пакет или ждать ответа.
    """
    label = _DOC_IMPL_LABELS.get(action, "документ")
    impls = {
        "build_act":     agent.build_act_impl,
        "build_receipt": agent.build_receipt_impl,
        "build_report":  agent.build_report_impl,
    }
    if action not in impls:
        return "error"

    await message.reply_text(f"⏳ Формирую {label} по сделке {num}...")
    result = await typing_while(update.effective_chat.id, context, impls[action](num))

    if result.get("error"):
        need = result.get("needs")
        keyboard = []
        if need:
            context.user_data["awaiting_doc_field"] = {
                "num": num, "action": action, "field": need,
            }
            if need == "settlement_date":
                from datetime import datetime as _dt
                today = _dt.now().strftime("%d.%m.%Y")
                keyboard.append([InlineKeyboardButton(
                    f"📅 Сегодня ({today})", callback_data=f"docfield:{today}")])
                hint = (result.get("needs_hint") or "").strip()
                if hint and hint != today:
                    keyboard.append([InlineKeyboardButton(
                        f"📅 {hint} (дата платежа)", callback_data=f"docfield:{hint}")])
        keyboard.append([InlineKeyboardButton("◀️ К сделке",
                                              callback_data=f"dealaction:{num}:menu")])
        await message.reply_text(
            result["error"],
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return "needs" if need else "error"

    # Адаптируем формат build_*_impl (file/extra_*) в формат send_result (files-list)
    files = []
    if result.get("file"):
        files.append({
            "file":       result["file"],
            "filename":   result["filename"],
            "drive_link": result.get("drive_link", ""),
        })
    for f_path, f_name, f_link in zip(
        result.get("extra_files", []),
        result.get("extra_names", []),
        result.get("extra_links", [""] * len(result.get("extra_files", []))),
    ):
        files.append({"file": f_path, "filename": f_name, "drive_link": f_link})

    await send_result(message, {
        "files":   files,
        "text":    result.get("message", ""),
        "buttons": result.get("buttons") if show_buttons else None,
    }, context=context)
    return "ok"


async def run_doc_batch(update, context, message, num: str, steps: list):
    """
    Формирует несколько документов подряд (расписка → акт → отчёт).

    Если документу не хватает данных, цикл ОСТАНАВЛИВАЕТСЯ: оставшиеся шаги
    кладутся в ожидание рядом с вопросом. Иначе следующие документы упёрлись
    бы в ту же нехватку, задали бы тот же вопрос ещё дважды, а ответ применился
    бы только к последнему. После ответа apply_doc_field продолжит пакет с
    прерванного места.
    """
    done = 0
    for i, step in enumerate(steps):
        status = await run_doc_impl(update, context, message, num, step,
                                    show_buttons=False)
        if status == "needs":
            pending = context.user_data.get("awaiting_doc_field")
            if pending:
                pending["steps"] = list(steps[i:])
            return
        if status == "ok":
            done += 1

    if done == len(steps):
        text = f"✅ Комплект по сделке *{num}* готов"
    else:
        text = (f"Комплект по сделке *{num}*: сформировано {done} из {len(steps)}, "
                "причины по остальным — выше")
    await message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ К сделке", callback_data=f"dealaction:{num}:menu")
        ]]),
    )


# Колонки журнала для полей, которые бот спрашивает по ходу сборки документа.
_DOC_FIELD_COLUMNS = {
    "settlement_date": "Дата расчёта",
    "fact_rate":       "Фактический курс",
}


async def apply_doc_field(update, context, message, value: str) -> bool:
    """
    Записывает в журнал значение, которого не хватило документу, и повторяет
    его сборку.

    Возвращает False, если ожидания не было или значение не распозналось —
    тогда сообщение обрабатывается дальше обычным путём.
    """
    pending = context.user_data.get("awaiting_doc_field")
    if not pending:
        return False

    field  = pending.get("field")
    num    = pending.get("num")
    action = pending.get("action")
    column = _DOC_FIELD_COLUMNS.get(field)
    if not column:
        context.user_data.pop("awaiting_doc_field", None)
        return False

    value = (value or "").strip()

    if field == "settlement_date":
        from datetime import datetime as _dt
        m = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})", value)
        if not m:
            await message.reply_text(
                "⚠️ Не разобрал дату. Пришлите её как ДД.ММ.ГГГГ — например 27.08.2026."
            )
            return True   # ожидание не снимаем, ждём корректный ввод
        d, mo, y = m.groups()
        y = y if len(y) == 4 else f"20{y}"
        try:
            value = _dt.strptime(f"{d.zfill(2)}.{mo.zfill(2)}.{y}", "%d.%m.%Y").strftime("%d.%m.%Y")
        except ValueError:
            await message.reply_text(f"⚠️ Такой даты не существует: «{value}». Проверьте и пришлите ещё раз.")
            return True
    else:  # fact_rate
        m = re.search(r"\d+(?:[.,]\d+)?", value.replace(" ", ""))
        if not m:
            await message.reply_text(
                "⚠️ Не разобрал курс. Пришлите число — например 82,80."
            )
            return True
        value = m.group(0).replace(".", ",")

    # pending нужен ниже (очередь пакета), поэтому берём копию перед сбросом
    pending = dict(pending)
    context.user_data.pop("awaiting_doc_field", None)

    ok = await agent.sheets.update_deal(num, {column: value})
    if not ok:
        await message.reply_text(f"⚠️ Не удалось записать «{column}» в журнал сделки {num}.")
        return True

    await message.reply_text(f"✅ {column}: {value} — записано по сделке {num}")

    steps = pending.get("steps")
    if steps:
        await run_doc_batch(update, context, message, num, steps)
    else:
        await run_doc_impl(update, context, message, num, action)
    return True
