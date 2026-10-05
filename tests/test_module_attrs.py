"""Ловит обращения к несуществующим функциям модулей проекта.

05.10.2026: кнопка выбора реквизитов падала на br.full_payload —
функции в bank_requisites не было, а тесты этот путь не проходили.
"""
import ast
import importlib
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOCAL = {p.stem for p in ROOT.glob("*.py")}
SKIP = {"bot"}  # запускается как __main__


def _aliases(tree):
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in LOCAL:
                    out[a.asname or a.name] = a.name
    return out


@pytest.mark.parametrize("path", sorted(ROOT.glob("*.py")), ids=lambda p: p.name)
def test_module_attributes_exist(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = _aliases(tree)
    missing = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id in aliases and aliases[node.value.id] not in SKIP):
            mod = importlib.import_module(aliases[node.value.id])
            if not hasattr(mod, node.attr):
                missing.append(f"{node.value.id}.{node.attr} (строка {node.lineno})")
    assert not missing, f"{path.name}: нет таких атрибутов: {missing}"
