# -*- coding: utf-8 -*-
"""
«📝 Только ДКП» (26.10.2026): договор купли-продажи без сделки — ни журнала,
ни Drive, ни агентского договора. Подписи только сторон.
"""
import asyncio
from pathlib import Path

import pytest

import drive_service

drive_service._build_service = lambda: object()   # Drive в тестах не нужен

import agent as agent_mod  # noqa: E402
import dkp_only  # noqa: E402
import kg_deal  # noqa: E402
from tests.test_documents import DEAL, PRICE, text_of

CHAT = "chat-dkp-only"

DKP_KEYS = tuple(k for k in DEAL if not k.startswith(("account", "bank", "corr_bank"))
                 and k not in ("exchange_rate", "Фактический курс", "Дата поступления",
                               "Дата расчёта", "cash_currency"))


def tpo_data(**extra):
    d = {k: DEAL[k] for k in DKP_KEYS}
    d.update(extra)
    return d


def kg_data(**extra):
    d = {k: v for k, v in tpo_data().items() if not k.startswith("tpo")}
    d.update({"srts": "серия KG № 1234567 от 10.07.2026", "gos_number": "01kg123abc",
              "poa": "1234 от 12.10.2026."})
    d.update(extra)
    return d


class Forbidden:
    """Журнал и Drive в этом режиме трогать нельзя — любое обращение = провал."""
    def __init__(self, name):
        self._name = name

    def __getattr__(self, attr):
        raise AssertionError(f"обращение к {self._name}.{attr} в режиме «Только ДКП»")


@pytest.fixture
def ag(monkeypatch):
    monkeypatch.setenv("SKIP_PDF", "1")
    a = agent_mod.DocumentAgent()
    a.drive, a.sheets = Forbidden("drive"), Forbidden("sheets")
    a._current_chat_id = CHAT
    yield a
    dkp_only.clear_pending(CHAT)


def run(a, name, inp):
    return asyncio.run(a._execute_tool(name, inp))


def test_pending_state():
    assert dkp_only.get_pending(CHAT) == ""
    dkp_only.set_pending(CHAT, "kg")
    assert dkp_only.get_pending(CHAT) == "kg"
    dkp_only.clear_pending(CHAT)
    assert dkp_only.get_pending(CHAT) == ""
    dkp_only.set_pending(CHAT, "мусор")
    assert dkp_only.get_pending(CHAT) == ""


def test_prompt_section_only_in_mode():
    assert dkp_only.prompt_section(CHAT) == ""
    dkp_only.set_pending(CHAT, "tpo")
    txt = dkp_only.prompt_section(CHAT)
    assert "ТОЛЬКО ДКП" in txt and "build_dkp_only" in txt and "tpo_number" in txt
    dkp_only.set_pending(CHAT, "kg")
    assert "srts" in dkp_only.prompt_section(CHAT)
    dkp_only.clear_pending(CHAT)


def test_file_name():
    assert dkp_only.file_base({"seller_initials": "ПЕТРОВ П.П."}, "C66483") == "ДКП_C66483_Петров"
    assert dkp_only.file_base({"seller_name": "Асанов Бакыт"}, "123456") == "ДКП_123456_Асанов"


def test_tpo_dkp_without_journal(ag):
    dkp_only.set_pending(CHAT, "tpo")
    res = run(ag, "build_dkp_only", {"data": tpo_data(), "dkp_date": "12.10.2026"})
    assert "error" not in res, res
    assert res["filename"] == "ДКП_C66483_Петров.docx"
    assert "не сохранялся" in res["caption"]
    txt = text_of(res["file"])
    assert "Договор купли-продажи автомобиля № C66483" in txt
    assert "«12» октября 2026" in txt
    assert "Петров Пётр Петрович" in txt and "Иванов Иван Иванович" in txt
    assert "41714106/220526/0000047349/00" in txt
    assert "Авто Континент" not in txt          # ни агента, ни печати компании
    assert "{{" not in txt
    assert dkp_only.get_pending(CHAT) == "tpo"   # режим остаётся — для правок


def test_kg_dkp(ag):
    res = run(ag, "build_dkp_only", {"data": kg_data(), "dkp_date": "12.10.2026",
                                     "variant": "kg"})
    assert "error" not in res, res
    txt = text_of(res["file"])
    assert "01KG123ABC" in txt and "1234567" in txt
    assert "удостоверенную доверенность № 1234 от 12.10.2026 на транспортное" in txt
    assert "{{" not in txt


def test_date_defaults_to_today(ag):
    res = run(ag, "build_dkp_only", {"data": tpo_data()})
    assert "error" not in res, res
    assert dkp_only.today()[6:] in res["message"]


def test_missing_fields_reported(ag):
    res = run(ag, "build_dkp_only", {"data": tpo_data(tpo_number="", car_price=""),
                                     "variant": "tpo"})
    assert "номер ТПО" in res["error"] and "цена" in res["error"]
    res = run(ag, "build_dkp_only", {"data": tpo_data(), "variant": "kg"})
    assert "СРТС" in res["error"] and "госномер" in res["error"]
    assert "доверенности" in res["error"]
    res = run(ag, "build_dkp_only", {"data": kg_data(poa=""), "variant": "kg"})
    assert "доверенности" in res["error"]


def test_poa_text():
    assert dkp_only.poa_text("№1234 от 06.10.2026.") == "№ 1234 от 06.10.2026"
    assert dkp_only.poa_text("  ") == ""


def test_kg_dkp_before_registration(ag):
    res = run(ag, "build_dkp_only", {"data": kg_data(), "dkp_date": "01.07.2026",
                                     "variant": "kg"})
    assert "раньше регистрации" in res["error"]


def test_bad_vin(ag):
    res = run(ag, "build_dkp_only", {"data": tpo_data(car_vin="LBV21GT02"), "variant": "tpo"})
    assert "VIN" in res["error"]


def test_ip_buyer(ag):
    res = run(ag, "build_dkp_only", {"data": tpo_data(buyer_type="ИП"), "variant": "tpo"})
    assert "ИНН покупателя" in res["error"]
    res = run(ag, "build_dkp_only", {"data": tpo_data(buyer_type="ИП", buyer_inn="771234567890"),
                                     "variant": "tpo", "dkp_date": "12.10.2026"})
    assert "error" not in res, res
    assert "Индивидуальный предприниматель" in text_of(res["file"])


def test_stale_deal_keys_ignored(ag):
    # Тип сделки «Учёт КГ» из прошлой переписки не должен переключить шаблон.
    res = run(ag, "build_dkp_only", {"data": tpo_data(deal_type=kg_deal.DEAL_TYPE_KG),
                                     "variant": "tpo", "dkp_date": "12.10.2026"})
    assert "error" not in res, res
    assert "ТПО" in text_of(res["file"])


def test_create_contract_blocked_in_mode(ag):
    dkp_only.set_pending(CHAT, "tpo")
    res = run(ag, "create_contract", {"commission_pct": 1.0, "data": tpo_data()})
    assert "Только ДКП" in res["error"]


def test_tool_is_journal_readonly():
    assert dkp_only.TOOL_NAME in agent_mod.DocumentAgent.JOURNAL_READONLY_TOOLS
    names = [t["name"] for t in agent_mod.DocumentAgent._get_tools(None)]
    assert dkp_only.TOOL_NAME in names


def test_menu_has_button():
    import bot_core
    kb = bot_core.main_menu_keyboard()
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "menu:dkp_only" in datas
