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
    assert diffs == [("BL", "— колонки нет в коде —", "Лишняя")]


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
