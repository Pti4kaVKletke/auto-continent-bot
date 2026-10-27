from agent import _stringify_scalars


def test_numbers_become_strings():
    d = {"car_price": 4533980, "car_year": 2026, "rate": 81.4, "x": 5.0,
         "flag": True, "none": None, "name": "Иван"}
    _stringify_scalars(d)
    assert d == {"car_price": "4533980", "car_year": "2026", "rate": "81.4", "x": "5",
                 "flag": True, "none": None, "name": "Иван"}
