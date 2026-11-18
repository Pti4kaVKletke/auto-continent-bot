# -*- coding: utf-8 -*-
from error_text import explain_error, is_file_too_big, user_message


def test_known_errors():
    assert is_file_too_big(Exception("File is too big"))
    assert "20 МБ" in explain_error(Exception("Bad Request: file is too big"))
    assert "спецсимвол" in explain_error(Exception("Can't parse entities: can't find end"))
    assert "Кнопка устарела" in explain_error(Exception("Query is too old"))
    assert "лимит" in explain_error(Exception("HttpError 429 quota exceeded"))
    assert "не найдены" in explain_error(Exception("HttpError 404 File not found"))
    assert "Неизвестная" in explain_error(Exception("что-то странное"))


def test_user_message_has_tech_line():
    t = user_message(ValueError("boom" * 100))
    assert t.startswith("⚠️") and "ValueError" in t and len(t) < 400
