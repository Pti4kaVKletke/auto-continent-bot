# -*- coding: utf-8 -*-
import asyncio
from types import SimpleNamespace

from PIL import Image

import agent as agent_mod
from scan_rename import (initials_from_name, parse_classification, person_tag,
                         preview_jpegs, target_name, unique_name, vin6)

DATA = {"buyer_name": "Абасов Магомед Магомедарипович", "buyer_initials": "Абасов М.М.",
        "seller_name": "Курбанбеков Бектур Алмазбекович", "seller_initials": "Курбанбеков Б.А.",
        "car_vin": "LMGNE3S89T1004360"}


def test_names():
    assert initials_from_name("Абасов Магомед Магомедарипович") == "Абасов_М.М."
    assert person_tag("Абасов М.М.", "") == "Абасов_М.М."
    assert person_tag("", "Иванов Иван") == "Иванов_И."
    assert vin6("lmgne3s89t1004360") == "004360" and vin6("123") == ""
    assert target_name("passport", "buyer", DATA, ".jpg") == "Паспорт_Абасов_М.М.jpg"
    assert target_name("declaration", "unknown", DATA, ".pdf") == "Декларация_004360.pdf"
    assert target_name("passport", "seller", DATA, ".pdf") == "ID_Курбанбеков_Б.А.pdf"
    assert target_name("passport", "unknown", DATA, ".pdf") is None
    assert target_name("other", "buyer", DATA, ".pdf") is None
    assert unique_name("a.pdf", {"a.pdf", "a_2.pdf"}) == "a_3.pdf"


def test_parse():
    assert parse_classification('{"kind": "passport", "owner": "buyer"}') == ("passport", "buyer")
    assert parse_classification("мусор") == ("other", "unknown")
    assert parse_classification('{"kind": "x"}') == ("other", "unknown")


def test_intake_rename(tmp_path):
    def mk(n):
        p = tmp_path / n
        Image.new("RGB", (100, 100)).save(p)
        return str(p)
    answers = iter(['{"kind":"passport","owner":"buyer"}', '{"kind":"declaration","owner":"unknown"}',
                    '{"kind":"other","owner":"unknown"}', 'ошибка-не-json'])

    async def create(**kw):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=next(answers))])
    ag = agent_mod.DocumentAgent.__new__(agent_mod.DocumentAgent)
    ag.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    ag.model = "m"
    used, got = set(), []
    for n in ("1.jpg", "2.jpg", "3.jpg", "4.jpg"):
        name = asyncio.run(ag._intake_scan_name({"filepath": mk(n), "original_name": n}, DATA, used))
        used.add(name)
        got.append(name)
    assert got[0] == "Паспорт_Абасов_М.М.jpg" and got[1] == "Декларация_004360.jpg"
    assert got[2:] == ["3.jpg", "4.jpg"]            # не уверены → прежнее имя
    assert preview_jpegs(str(tmp_path / "x.docx")) == []
