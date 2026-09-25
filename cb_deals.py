"""
cb_deals.py — Списки сделок, статистика и копирование сделки.

Вынесено из bot.handle_callback 25.09.2026 без изменения логики — тела веток
перенесены дословно. Общие объекты берутся из bot_core, маршрутизация —
таблица CALLBACK_ROUTES в bot.py.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from agent import _parse_payments, _calc_total_amount, _fmt_money

from bot_core import (
    agent,
    logger,
    show_deals_list,
)


async def on_copy(update, context, query, data):
    """Кнопки: callback_data «copy:…»."""
    sub = data.split(":", 1)[1]
    pending = context.user_data.get("copy_pending")

    if sub == "cancel":
        context.user_data.pop("copy_pending", None)
        await query.edit_message_text(
            "❌ Копирование отменено.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Меню", callback_data="menu:back"),
            ]]),
        )
        return

    if sub == "ok":
        if not pending:
            await query.edit_message_text(
                "⚠️ Данные черновика утеряны (перезапуск бота?). "
                "Попроси заново: «создай новую как в NNN».",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Меню", callback_data="menu:back"),
                ]]),
            )
            return

        # Убираем кнопки под предпросмотром, чтобы нельзя было переподтвердить
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        # Подсказываем, чего именно не хватает. Список полей — от _prepare_copy_data.
        prepared = pending.get("prepared") or {}
        missing = []
        if not prepared.get("car_model"): missing.append("модель авто")
        if not prepared.get("car_vin"):   missing.append("VIN")
        if not prepared.get("car_price"): missing.append("цену")

        if missing:
            text = (
                "✅ Данные скопированы. Осталось указать: "
                + ", ".join(missing) + ".\n\n"
                "Напиши в свободной форме — можно всё сразу одним сообщением, "
                "например: `Chery Tiggo 8, VIN LVVDB21B..., цена 3 200 000`."
            )
        else:
            text = (
                "✅ Данные скопированы. Скажи что ещё уточнить — "
                "или напиши «создавай», если всё готово."
            )
        await query.message.reply_text(text, parse_mode="Markdown")
        return
    return


async def on_deals(update, context, query, data):
    """Кнопки: callback_data «deals:…»."""
    parts = data.split(":")

    # Свой период — просим ввести диапазон дат (как в статистике)
    if len(parts) == 2 and parts[1] == "ask_custom":
        context.user_data["awaiting_deals_dates"] = True
        await query.edit_message_text(
            "📅 *Свой период*\n\n"
            "Введи диапазон дат в одном из форматов:\n"
            "• `01.06.2026 - 30.06.2026`\n"
            "• `01.06.2026 30.06.2026`\n"
            "• `01.06.2026` (одна дата — от неё до сегодня)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Отмена", callback_data="menu:deals"),
            ]]),
        )
        return

    period      = parts[1] if len(parts) > 1 else "all"
    status_code = parts[2] if len(parts) > 2 else "all"
    try:
        page = int(parts[3]) if len(parts) > 3 else 0
    except ValueError:
        page = 0
    # Даты своего периода в callback_data не влезают (лимит 64 байта),
    # поэтому держим их в user_data — как и остальное состояние диалога.
    df, dt = context.user_data.get("deals_custom", ("", ""))

    await query.edit_message_text("🔄 Загружаю сделки...")
    await show_deals_list(query, context, period, status_code, page, df, dt)
    return


async def on_stats(update, context, query, data):
    """Кнопки: callback_data «stats:…»."""
    period = data.split(":", 1)[1]

    # Список сделок, ждущих доплату — самостоятельный отчёт, не завязан на период
    if period == "pending":
        await query.edit_message_text("⏳ Собираю список сделок, ждущих доплату...")
        try:
            deals = await agent.sheets.get_all_deals()
        except Exception as e:
            logger.error(f"Ошибка получения сделок для 'ждём доплату': {e}", exc_info=True)
            await query.edit_message_text(
                f"⚠️ Ошибка: {e}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ К периодам", callback_data="menu:stats"),
                ]]),
            )
            return

        pending = [
            d for d in deals
            if (d.get("Статус") or "").strip().lower() == "ждём доплату"
        ]

        back_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ К периодам", callback_data="menu:stats")],
            [InlineKeyboardButton("◀️ Меню",       callback_data="menu:back")],
        ])

        if not pending:
            await query.edit_message_text(
                "✅ Сделок, ждущих доплату, нет.",
                reply_markup=back_kb,
            )
            return

        # Сортируем по дате договора (свежие сверху)
        def _sort_key(d):
            dt = d.get("Дата договора") or ""
            # DD.MM.YYYY → YYYYMMDD для лексикографической сортировки
            try:
                dd, mm, yy = dt.split(".")
                return f"{yy}{mm}{dd}"
            except Exception:
                return ""
        pending.sort(key=_sort_key, reverse=True)

        lines = [f"⏳ *Сделок ждёт доплату: {len(pending)}*\n"]
        total_remainder_by_currency: dict[str, float] = {}

        for d in pending:
            num  = d.get("Номер договора", "—")
            fio  = d.get("buyer_initials") or d.get("buyer_name") or "—"
            car  = d.get("car_model") or "—"
            vin  = d.get("car_vin") or ""
            curr = (d.get("currency") or "руб").strip() or "руб"

            total    = _calc_total_amount(d)
            received = sum(p["amount"] for p in _parse_payments(d.get("Платежи", "")))
            remainder = total - received

            total_remainder_by_currency[curr] = total_remainder_by_currency.get(curr, 0.0) + remainder

            vin_tail = f" · `...{vin[-6:]}`" if vin else ""
            lines.append(
                f"📄 `{num}` · *{fio}*\n"
                f"   🚗 {car}{vin_tail}\n"
                f"   💰 Сумма:    {_fmt_money(total)} {curr}\n"
                f"   📥 Оплачено: {_fmt_money(received)} {curr}\n"
                f"   ⏳ Остаток:  *{_fmt_money(remainder)} {curr}*\n"
            )

        # Итоговый остаток по валютам
        lines.append("─" * 20)
        remainder_str = ", ".join(
            f"*{_fmt_money(v)} {c}*"
            for c, v in sorted(total_remainder_by_currency.items())
        )
        lines.append(f"💼 *Всего к получению:* {remainder_str}")

        text_out = "\n".join(lines)

        # Telegram лимит на сообщение ~4096 симв. Если превысили — режем.
        if len(text_out) > 3900:
            text_out = text_out[:3800] + "\n\n_(список обрезан — слишком много сделок)_"

        try:
            await query.edit_message_text(
                text_out,
                parse_mode="Markdown",
                reply_markup=back_kb,
            )
        except Exception as e:
            logger.warning(f"Не удалось отредактировать сообщение с pending, шлём новым: {e}")
            await query.message.reply_text(
                text_out,
                parse_mode="Markdown",
                reply_markup=back_kb,
            )
        return

    # Свой период — просим ввести диапазон дат
    if period == "custom":
        context.user_data["awaiting_stats_dates"] = True
        await query.edit_message_text(
            "📅 *Свой период*\n\n"
            "Введи диапазон дат в одном из форматов:\n"
            "• `01.06.2026 - 30.06.2026`\n"
            "• `01.06.2026 30.06.2026`\n"
            "• `01.06.2026` (одна дата — от неё до сегодня)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Отмена", callback_data="menu:stats"),
            ]]),
        )
        return

    period_label = {
        "today":     "сегодня",
        "yesterday": "вчера",
        "week":      "неделю",
        "month":     "месяц",
        "last_month": "прошлый месяц",
        "quarter":   "квартал",
        "year":      "год",
        "all":       "всё время",
    }.get(period, period)
    await query.edit_message_text(f"📊 Считаю статистику за {period_label}...")

    # Вызываем инструмент напрямую через _execute_tool — избегаем LLM для
    # детерминированной операции (нет расхода токенов, нет риска галлюцинации,
    # мгновенный ответ).
    try:
        result = await agent._execute_tool("get_statistics", {"period": period})
    except Exception as e:
        logger.error(f"Ошибка вычисления статистики: {e}", exc_info=True)
        result = {"error": f"⚠️ Ошибка: {e}"}

    text = result.get("message") or result.get("error") or "Нет данных."
    kb = [
        [InlineKeyboardButton("◀️ К периодам", callback_data="menu:stats")],
        [InlineKeyboardButton("◀️ Меню",        callback_data="menu:back")],
    ]
    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return
