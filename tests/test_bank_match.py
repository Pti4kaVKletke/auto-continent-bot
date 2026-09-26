# -*- coding: utf-8 -*-
"""Профиль реквизитов сделки: один счёт — несколько профилей с разными корреспондентами."""
import bank_requisites as br

ACC = "1240020002505333"
BASE = {"account_type": br.CORR, "account_number": ACC, "account_currency": "RUB",
        "bank_name": "ОАО БАКАЙ БАНК, г. Бишкек", "bank_bic": "044525000",
        "bank_corr_acc": "30111810000000000001"}
ALFA = {**BASE, "corr_bank_name": "АО «АЛЬФА-БАНК»", "corr_bank_bic": "044525593",
        "corr_bank_acc": "30101810200000000593"}
GAZ  = {**BASE, "corr_bank_name": "Банк ГПБ (АО)", "corr_bank_bic": "044525823",
        "corr_bank_acc": "30101810200000000823"}
PROFILES = {"Альфа-Бакай": ALFA, "Газпром-Бакай": GAZ}


def test_picks_matching_correspondent():
    assert br.match_profile(dict(GAZ), PROFILES) == "Газпром-Бакай"
    assert br.match_profile(dict(ALFA), PROFILES) == "Альфа-Бакай"


def test_order_does_not_matter():
    assert br.match_profile(dict(GAZ), dict(reversed(list(PROFILES.items())))) == "Газпром-Бакай"


def test_other_account_not_matched():
    assert br.match_profile({**GAZ, "account_number": "999"}, PROFILES) is None
    assert br.match_profile({**GAZ, "account_number": ""}, PROFILES) is None
