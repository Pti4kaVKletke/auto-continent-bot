# -*- coding: utf-8 -*-
"""
Субагентская сделка целиком через create_contract (без Drive/Sheets/Telegram)
и экраны salon_ui на фейковых объектах Telegram.
"""
import asyncio

import pytest

import drive_service
import salon
from tests.test_documents import DEAL, PRICE
from tests.test_subagent import OOO

drive_service._build_service = lambda: object()   # Drive в тестах не нужен

import agent as agent_mod  # noqa: E402


class FakeDrive:
    async def get_next_contract_number(self, date=None): return "260926901"
    async def get_or_create_deal_folder(self, num): return "folder"
    async def _get_or_create_folder(self, name, parent): return "sub"
    async def upload_file(self, path, name, folder): return f"https://drive/{name}"


class FakeSheets:
    def __init__(self, existing=None):
        self.saved, self.updated, self.existing = None, None, existing or {}
    async def journal_block_reason(self): return ""
    async def get_deal(self, num): return self.existing.get(num)
    async def save_deal(self, **kw): self.saved = kw; return True
    async def update_deal(self, num, upd): self.updated = (num, upd); return True
    async def batch_update_column(self, *a, **k): return 0


@pytest.fixture
def ag(monkeypatch):
    monkeypatch.setenv("SKIP_PDF", "1")
    a = agent_mod.DocumentAgent()
    a.drive, a.sheets = FakeDrive(), FakeSheets()
    a._current_chat_id = "chat-flow"
    salon.save(OOO)
    yield a
    salon.clear_pending("chat-flow")


def _data(**extra):
    d = {k: v for k, v in DEAL.items() if k not in ("Фактический курс", "Дата поступления", "Дата расчёта")}
    d["car_price"] = str(PRICE)
    d.update(extra)
    return d


def run(a, **inp):
    return asyncio.run(a._execute_tool("create_contract", {"commission_pct": 1.0, **inp}))


def test_pending_salon_requires_client_contract(ag):
    salon.set_pending("chat-flow", OOO["inn"])
    res = run(ag, data=_data())
    assert "договор" in res.get("error", "") and ag.sheets.saved is None


def test_subagent_deal_created(ag):
    salon.set_pending("chat-flow", OOO["inn"])
    res = run(ag, data=_data(salon_contract="45 от 12.09.2026"), contract_date="26.09.2026")
    assert res["filename"] == "САГ_Договор_260926901.docx", res
    saved = ag.sheets.saved["deal_data"]
    assert saved["deal_type"] == "Субагент"
    assert saved["salon"] == "ООО «Автомир», ИНН 7701234567"
    assert saved["salon_contract"] == "№ 45 от 12.09.2026"
    assert salon.get_pending("chat-flow") == {}          # выбор использован


def test_direct_deal_without_pending(ag):
    res = run(ag, data=_data(), contract_date="26.09.2026")
    assert res["filename"] == "АГ_Договор_260926901.docx", res
    assert ag.sheets.saved["deal_data"]["deal_type"] == "Прямая"


def test_regen_keeps_subagent_type(ag):
    ag.sheets = FakeSheets({"260926901": {
        "deal_type": "Субагент", "salon": "ООО «Автомир», ИНН 7701234567",
        "salon_contract": "№ 45 от 12.09.2026"}})
    res = run(ag, data=_data(), existing_contract_number="260926901", contract_date="26.09.2026")
    assert res["filename"] == "САГ_Договор_260926901.docx", res


def test_subagent_direct_account_rejected(ag):
    salon.set_pending("chat-flow", OOO["inn"])
    res = run(ag, data=_data(salon_contract="№ 1 от 01.09.2026", account_type="direct_rf"))
    assert "корреспондент" in res.get("error", "")


def test_system_prompt_mentions_pending(ag):
    salon.set_pending("chat-flow", OOO["inn"])
    assert "ТЕКУЩАЯ НОВАЯ СДЕЛКА: СУБАГЕНТСКАЯ" in ag._build_system_prompt()
    salon.clear_pending("chat-flow")
    assert "ТЕКУЩАЯ НОВАЯ СДЕЛКА: СУБАГЕНТСКАЯ" not in ag._build_system_prompt()


def test_scan_name_recognized():
    assert agent_mod.scan_type_of("Подп_САГ_Договор_260926901.pdf") == "ag"


# ── salon_ui на фейках Telegram ─────────────────────────────────────────

class _Q:
    def __init__(self): self.sent = []
    async def edit_message_text(self, text, **kw): self.sent.append((text, kw.get("reply_markup")))


class _Upd:
    def __init__(self):
        self.callback_query = _Q()
        self.effective_chat = type("C", (), {"id": "chat-ui"})()


class _Ctx:
    def __init__(self): self.user_data = {}


def _buttons(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_ui_new_deal_via_salon():
    import salon_ui
    salon.save(OOO)
    upd, ctx = _Upd(), _Ctx()
    asyncio.run(salon_ui.handle_callback(upd, ctx, "nd:sub"))
    assert f"nd:s:{OOO['inn']}" in _buttons(upd.callback_query.sent[-1][1])
    asyncio.run(salon_ui.handle_callback(upd, ctx, f"nd:s:{OOO['inn']}"))
    assert ctx.user_data["awaiting_new_deal_docs"] == 1
    assert salon.get_pending("chat-ui")["inn"] == OOO["inn"]
    asyncio.run(salon_ui.handle_callback(upd, ctx, "nd:direct"))
    assert salon.get_pending("chat-ui") == {}


def test_ui_screens_render():
    import salon_ui
    salon.save(OOO)
    upd, ctx = _Upd(), _Ctx()
    for cb in ("sl:list", f"sl:v:{OOO['inn']}", f"sl:e:{OOO['inn']}", f"sl:f:{OOO['inn']}:3",
               "sl:new:deal", f"sl:del:{OOO['inn']}"):
        assert asyncio.run(salon_ui.handle_callback(upd, ctx, cb)) is True
    ctx.user_data["sl_draft"] = {"card": dict(OOO, inn="7702222222"), "for_deal": True}
    asyncio.run(salon_ui.handle_callback(upd, ctx, "sl:save"))
    assert salon.get("7702222222")["name_short"] == "ООО «Автомир»"
    assert salon.get_pending("chat-ui")["inn"] == "7702222222"
    salon.clear_pending("chat-ui")
