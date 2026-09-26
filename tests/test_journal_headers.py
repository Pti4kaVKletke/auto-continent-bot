# -*- coding: utf-8 -*-
"""Проверка колонок журнала: при сдвиге колонок запись должна блокироваться."""
import asyncio

import gsheets_service as g

REAL = [h if isinstance(h, str) else h[1] for h in g.EXPECTED_HEADERS]


def test_lists_have_same_length():
    assert len(g.EXPECTED_HEADERS) == len(g.COLUMNS)


def test_real_header_matches():
    assert g.compare_headers(REAL) == []


def test_case_spaces_yo_ignored():
    assert g.compare_headers([h.upper().replace("Ё", "Е") + "  " for h in REAL]) == []


def test_inserted_column_detected():
    shifted = REAL[:10] + ["Новая колонка"] + REAL[10:]
    diffs = g.compare_headers(shifted)
    assert diffs and diffs[0][0] == "K"


def test_extra_column_at_end_detected():
    diffs = g.compare_headers(REAL + ["Лишняя"])
    assert diffs == [("BO", "— колонки нет в коде —", "Лишняя")]


class _FakeSvc:
    def __init__(self, header): self.header = header
    def spreadsheets(self): return self
    def values(self): return self
    def get(self, **kw): return self
    def execute(self): return {"values": [self.header]}


def test_write_blocked_then_unblocked():
    s = g.GoogleSheetsService()
    s._service = _FakeSvc(REAL[:5] + ["X"] + REAL[5:])
    assert asyncio.run(s._guard_write("test")) is False
    assert "заблокирована" in asyncio.run(s.journal_block_reason())
    # таблицу поправили → блокировка снимается без перезапуска
    s._service = _FakeSvc(REAL)
    s._header_checked_at -= g.HEADER_RECHECK_SEC + 1
    assert asyncio.run(s.journal_block_reason()) == ""

# ── Блок «СУБАГЕНТ» A–C (26.09.2026): номер сделки теперь в колонке D ──

def test_subagent_block_first():
    assert REAL[:4] == ["Тип сделки", "Салон (Агент РФ)", "Договор салона с клиентом", "Номер договора"]
    assert g.NUM_COL_IDX == 3


class _RowsSvc:
    """Фейк Sheets: заголовок совпадает, данные — одна строка сделки."""
    def __init__(self, rows):
        self.rows, self.appended, self.updated, self._last = rows, None, None, None
    def spreadsheets(self): return self
    def values(self): return self
    def get(self, **kw): self._last = kw["range"]; return self
    def append(self, **kw): self.appended = kw["body"]["values"][0]; self._last = "append"; return self
    def update(self, **kw): self.updated = kw; self._last = "update"; return self
    def execute(self):
        if self._last == f"A{g.HEADER_ROW}:ZZ{g.HEADER_ROW}":
            return {"values": [REAL]}
        return {"values": self.rows}


def test_save_deal_writes_default_type():
    s = g.GoogleSheetsService()
    s._service = _RowsSvc([])
    assert asyncio.run(s.save_deal("260926001", "26.09.2026", {"car_price": "1000"}, 1.0))
    row = s._service.appended
    assert row[0] == "Прямая" and row[g.NUM_COL_IDX] == "260926001"
    assert len(row) == len(g.COLUMNS)


def test_update_deal_finds_row_by_number_column():
    row = ["Прямая", "", "", "260926001"] + [""] * (len(g.COLUMNS) - 4)
    s = g.GoogleSheetsService()
    s._service = _RowsSvc([row])
    assert asyncio.run(s.update_deal("260926001", {"Статус": "закрыта"}))
    written = s._service.updated["body"]["values"][0]
    assert written[g.COLUMNS.index("Статус")] == "закрыта"
    assert written[g.NUM_COL_IDX] == "260926001"
