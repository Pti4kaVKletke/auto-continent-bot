from agent import regen_mismatch

OLD = {"buyer_name": "Иванов Иван", "car_vin": "LSVAB12345678"}


def test_new_deal_over_old_is_blocked():
    assert regen_mismatch(OLD, {"buyer_name": "Петров Пётр", "car_vin": "LGWXX99999999"})


def test_real_regen_passes():
    assert not regen_mismatch(OLD, dict(OLD))
    # правка одного поля
    assert not regen_mismatch(OLD, {"buyer_name": "Иванов Иван", "car_vin": "LGWXX99999999"})
    assert not regen_mismatch(OLD, {"buyer_name": "Петров Пётр", "car_vin": "lsvab 12345678"})


def test_empty_sides_do_not_block():
    assert not regen_mismatch({}, {"buyer_name": "A", "car_vin": "B"})
    assert not regen_mismatch({"buyer_name": "", "car_vin": ""}, {"buyer_name": "A", "car_vin": "B"})
    assert not regen_mismatch(OLD, {"buyer_name": "A"})
