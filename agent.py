import os
import base64
import json
import re
import asyncio
import tempfile
from pathlib import Path
from datetime import datetime
import logging
import httpx

import anthropic

logger = logging.getLogger(__name__)

import memory
from drive_service import GoogleDriveService
from doc_builder import DocumentBuilder


class DocumentAgent:

    def __init__(self):
        self.client  = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self.drive   = GoogleDriveService()
        self.builder = DocumentBuilder()
        self.model   = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
        memory.init_db()

    def _build_system_prompt(self) -> str:
        today_str = datetime.now().strftime("%d.%m.%Y")
        base = f"""Ты Александра, автоматизированный агент директора компании ОсОО «Авто Континент» (г. Бишкек, Кыргызстан).
Компания занимается продажей автомобилей из Китая, выступает платёжным агентом между покупателями из России и местными продавцами.

У тебя есть РЕАЛЬНЫЕ ИНСТРУМЕНТЫ которые ты ОБЯЗАН использовать:

- create_contract — создать полный пакет документов сделки (агентский договор, ДКП и счёт) и сохранить на Google Drive
- save_company    — сохранить реквизиты компании/клиента в постоянную память
- save_instruction — сохранить инструкцию для себя

ВАЖНО: Когда пользователь просит создать договор или счёт — ВСЕГДА вызывай соответствующий инструмент.

=== ОБЯЗАТЕЛЬНЫЕ КЛЮЧИ В ПОЛЕ data ===

При вызове create_contract ты ОБЯЗАН передать data со СТРОГО ЭТИМИ ключами (используй только эти, никакие другие):

ПОКУПАТЕЛЬ (гражданин РФ):
buyer_name           — ФИО полностью
buyer_birth_date     — дата рождения (ДД.ММ.ГГГГ)
buyer_address        — адрес регистрации
buyer_initials       — Фамилия + инициалы имени и отчества (формат: "Иванов И.И." — фамилия ПОЛНОСТЬЮ, имя и отчество — только первые буквы с точками)
passport_series      — серия паспорта
passport_number      — номер паспорта
passport_issued_by   — кем выдан
passport_issued_date — дата выдачи (ДД.ММ.ГГГГ)
passport_code        — код подразделения

ПРОДАВЕЦ (гражданин КР):
seller_name          — ФИО полностью
seller_birth_date    — дата рождения (ДД.ММ.ГГГГ)
seller_address       — адрес регистрации
seller_initials      — Фамилия + инициалы имени и отчества (формат: "Иванов И.И." — фамилия ПОЛНОСТЬЮ, имя и отчество — только первые буквы с точками)
seller_id_number     — номер идентификационной карты
seller_id_issued_by  — кем выдана карта
seller_id_issued_date — дата выдачи карты (ДД.ММ.ГГГГ)

АВТОМОБИЛЬ:
car_model       — марка и модель (Toyota RAV4)
car_vin         — VIN номер
car_year        — год выпуска
car_color       — цвет
car_body_number — номер кузова (если есть, иначе VIN)
tpo_number      — номер ТПО
tpo_day         — день выдачи ТПО
tpo_month       — месяц выдачи ТПО (прописью: января, февраля...)
tpo_year        — год выдачи ТПО

ФИНАНСЫ — ВАЖНО: это ДВЕ РАЗНЫЕ СУММЫ:
car_price        — цена автомобиля в ДКП цифрами (например: 4200000). Валюта — рубли.
car_price_words  — цена ДКП прописью (Четыре миллиона двести тысяч рублей)
currency         — валюта ДКП (рублей)
cash_amount      — сумма наличных в Поручении цифрами (например: 54900). Это ДРУГАЯ сумма — в долларах!
cash_amount_words — сумма наличных прописью БЕЗ валюты (Пятьдесят четыре тысячи девятьсот)
cash_currency    — валюта наличных (долларов / сом)
account_currency — валюта счёта для банковского перевода
account_number   — номер счёта получателя
bank_corr_line1  — название банка-корреспондента (например "АО «Тинькофф Банк»")
bank_corr_line2  — БИК банка-корреспондента (только число, например "044525974")
bank_corr_line3  — корр.счёт банка-корреспондента (например "30101810145250000974")
bank_ben_line1   — название банка получателя (например "ОАО БАКАЙ БАНК")
bank_ben_line2   — БИК и корр.счёт банка получателя ОДНОЙ СТРОКОЙ в формате
                    "БИК: <бик>, корр. счёт: <счёт>" (например "БИК: 124034, корр. счёт: 30111810100000000028")

=== ОБЯЗАТЕЛЬНАЯ ПРОВЕРКА ПЕРЕД ВЫЗОВОМ create_contract ===

Перед тем как вызвать create_contract, ты ОБЯЗАН убедиться что у тебя есть ВСЕ поля из этого списка.
Если хотя бы одно обязательное поле пустое — НЕ создавай договор, а спроси все недостающие данные ОДНИМ сообщением.

ОБЯЗАТЕЛЬНЫЕ поля (без них договор создавать НЕЛЬЗЯ):
Покупатель: buyer_name, buyer_initials, buyer_birth_date, buyer_address, passport_series, passport_number, passport_issued_by, passport_issued_date, passport_code
Продавец:   seller_name, seller_initials, seller_id_issued_date, seller_birth_date, seller_address, seller_id_number, seller_id_issued_by
Автомобиль: car_model, car_vin, car_year, car_color, tpo_number, tpo_day, tpo_month, tpo_year
Финансы:    car_price, car_price_words, currency, cash_amount, cash_amount_words, cash_currency, account_currency, account_number, bank_corr_line1, bank_corr_line2, bank_corr_line3, bank_ben_line1, bank_ben_line2
Комиссия:   commission_pct передаётся как отдельный параметр инструмента, НЕ внутри data

НЕОБЯЗАТЕЛЬНЫЕ поля (оставь пустыми если нет):
car_body_number

=== ПРАВИЛА ИЗВЛЕЧЕНИЯ ДАННЫХ ИЗ ДОКУМЕНТОВ ===

Когда пользователь присылает скан или фото документа — автоматически извлекай все данные.

1. ТПО (Таможенный приходной ордер) — документ называется "Таможенный приходной ордер №":
- tpo_number: поле "1. Справочный номер" — длинный номер вида 41714106/310526/0000050870/00
- Дата ТПО берётся из самого номера ТПО — это средняя часть между первым и вторым слэшем:
  Например: 41714106/310526/0000050870/00 → средняя часть "310526" → ДДММГГ → день=31, месяц=05, год=2026
  tpo_day: первые 2 цифры (например "31")
  tpo_month: следующие 2 цифры → ПРОПИСЬЮ в родительном падеже (01=января, 02=февраля, 03=марта, 04=апреля, 05=мая, 06=июня, 07=июля, 08=августа, 09=сентября, 10=октября, 11=ноября, 12=декабря)
  tpo_year: последние 2 цифры + "20" спереди (26 → "2026")
- Из ТПО также можно взять данные продавца (поле "4. Плательщик"): ФИО, ИНН, адрес,
  номер ID карты, дату выдачи ID карты и орган выдачи (строка "ПАСПОРТ: ID ...")

2. Пассажирская таможенная декларация — содержит ТЕ ЖЕ данные продавца (ФИО, номер ID карты,
   дата выдачи, орган выдачи — раздел "1. Сведения о декларанте"), а также данные автомобиля
   (марка, модель, VIN, год выпуска — раздел "4. Дополнительные сведения о товарах") и
   адрес регистрации продавца.
   - Если ТПО и декларация присланы вместе — это ОДИН И ТОТ ЖЕ продавец, данные должны
     совпадать. Используй ТПО как основной источник ФИО/ID-карты (там формат проще для
     разбора — см. правило выше про строку "ПАСПОРТ: ID ..."), декларацию — для сверки
     и для данных, которых нет в ТПО (адрес регистрации, марка/модель/VIN/год авто).
   - Если присутствует только ОДИН из двух документов — извлекай все доступные поля
     из него.

3. Паспорт РФ — извлеки все поля покупателя:
buyer_name, buyer_birth_date, buyer_address, passport_series, passport_number, passport_issued_by, passport_issued_date, passport_code

ВАЖНО про buyer_address (адрес регистрации):
- Адрес регистрации указан НЕ на основном развороте с фото, а на страницах "Место жительства" —
  это штампы о регистрации/прописке (обычно идут после страниц с отметками о браке/детях).
- Если в паспорте НЕСКОЛЬКО штампов о регистрации (видны записи "ЗАРЕГИСТРИРОВАН" и/или
  "СНЯТ С РЕГИСТРАЦИОННОГО УЧЁТА") — бери адрес из САМОГО ПОСЛЕДНЕГО (самого нового) штампа
  регистрации, у которого нет отметки о снятии с учёта после него.
- Формат buyer_address: "РФ, г. <город>, ул. <улица>, д. <номер>, корп. <корпус>, кв. <номер>"
  (корпус указывай только если есть; если есть только "д. N кв. M" без корпуса — пропусти "корп.").
  Пример формата (это ТОЛЬКО иллюстрация формата, НЕ настоящий адрес — никогда не используй
  эти конкретные значения): "РФ, г. <Город>, ул. <Название улицы>, д. <N>, корп. <N>, кв. <N>"
- ВАЖНО: страница "Место жительства" в сканах/фото часто оказывается ПЕРЕВЁРНУТА на 180°
  (текст читается вверх ногами). Если страница регистрации перевёрнута — мысленно
  поверни изображение на 180° и читай текст в правильной ориентации, НЕ пропускай её
  как нечитаемую только из-за поворота.
- Различай два типа штампов:
  1) ШТАМП ОТ РУКИ (текст написан вручную, разные почерки) — действительно может быть
     нечитаем, особенно низкого качества.
  2) МАШИНОПИСНЫЙ ШТАМП (печатный текст, типографский шрифт, как у "ЗАРЕГИСТРИРОВАН ..., 
     МО МВД ..., р-н:, улица:, д.:") — такой штамп почти ВСЕГДА читаем, даже если повёрнут
     или с водяным знаком/гербом на фоне. Прочитай его внимательно по частям (район, улица,
     дом) — не сдавайся сразу.
- Штампы о регистрации часто заполнены от руки и могут быть труднораспознаваемы. Если текст
  штампа нечитаем или ты не уверен в адресе — НЕ угадывай и НЕ бери адрес с основного разворота
  (там указано только место РОЖДЕНИЯ, а не регистрации). Вместо этого оставь buyer_address пустым
  и явно напиши пользователю: "⚠️ Не удалось прочитать адрес регистрации (штамп от руки) —
  укажите адрес регистрации вручную".
- Если пользователь УЖЕ ввёл адрес регистрации вручную текстом в чате — используй ЭТОТ адрес
  как buyer_address без изменений. НЕ заменяй его другим адресом и НЕ придумывай новый.

ВАЖНО про passport_series и passport_number:
- На правом поле страницы напечатаны вертикальные столбики цифр — по одной-две цифре
  на строку, читаются сверху вниз. Это серия и номер паспорта: первые 4 цифры — серия,
  следующие 6 цифр — номер (например столбик "65 / 17 / 525507" → серия 6517, номер
  525507). Эта вертикальная надпись повторяется ДВА РАЗА на развороте: один раз в
  верхней половине страницы, второй раз — в нижней половине рядом с фотографией.
  Прочитай оба раза и используй значение, если оба совпадают.
- Внизу страницы с фотографией есть ДВЕ ГОРИЗОНТАЛЬНЫЕ строки из латинских букв, цифр
  и символов "<" — это машиночитаемая зона (MRZ). Серию и номер паспорта из неё
  брать НЕЛЬЗЯ, даже если там видна похожая последовательность цифр.
- "Код подразделения" (формат XXX-XXX с дефисом, рядом с датой выдачи) — это другой
  номер, не путай его с серией/номером паспорта.
- Если оба прочтения вертикального столбика не совпадают, или ты не уверен в чтении —
  не угадывай, спроси пользователя.


4. Идентификационная карта КР — извлеки все поля продавца:
seller_name, seller_birth_date, seller_address, seller_id_number, seller_id_issued_by, seller_id_issued_date

ВАЖНО про seller_name (ФИО продавца):
- В поле "4. Плательщик" ТПО ФИО записано ОДНОЙ СТРОКОЙ сразу после строки с "ИНН:",
  в порядке ФАМИЛИЯ ИМЯ ОТЧЕСТВО, например:
    "ИНН:11609199750050
     ЭШАНКУЛОВА ГУЛСИМА КУЧКОНОВНА"
  Здесь ПЕРВОЕ слово ("ЭШАНКУЛОВА") — это ФАМИЛИЯ. НЕ путай эту строку с адресом
  (адрес идёт НИЖЕ, начинается с "АДРЕС:"). Бери ВСЮ строку ФИО целиком, включая первое слово.
- В Пассажирской таможенной декларации (раздел "1. Сведения о декларанте") ФИО указано
  в ТРЁХ ОТДЕЛЬНЫХ колонках с подписями "(фамилия)", "(имя)", "(отчество)" — используй
  её для проверки/сверки с ТПО.
- seller_name ОБЯЗАН включать ВСЕ ТРИ части: "Фамилия Имя Отчество" полностью, без сокращений.
- Перед тем как вписать seller_name, перечитай поле 4 ТПО построчно и убедись, что взял
  ИМЕННО строку ФИО (идёт сразу после ИНН), а не строку с адресом, и что включил все три слова.

ВАЖНО про seller_birth_date (дата рождения продавца):
- ОБЯЗАТЕЛЬНО вычисляй seller_birth_date из ИНН продавца (поле "4. Плательщик" в ТПО,
  начинается с "ИНН:"), используя формулу ниже.
- ЭТО ПРАВИЛО ПРИМЕНЯЕТСЯ ВСЕГДА. НИКОГДА не используй дату выдачи ID-карты/паспорта
  как дату рождения. Дата выдачи документа и дата рождения — это РАЗНЫЕ даты.
  Дата рождения берётся ТОЛЬКО расчётом из ИНН.

- Формула (ИНН состоит из 14 цифр, нумерация с 1):
  Позиция:  1  2  3  4  5  6  7  8  9  10 11 12 13 14
  ДД = цифры на позициях 2 и 3
  ММ = цифры на позициях 4 и 5
  ГГГГ = цифры на позициях 6, 7, 8 и 9
  seller_birth_date = ДД.ММ.ГГГГ

- Пример 1: ИНН 20103200600200
  Позиции: 2=0, 3=1, 4=0, 5=3, 6=2, 7=0, 8=0, 9=6
  ДД=01, ММ=03, ГГГГ=2006 → seller_birth_date = "01.03.2006"

- Пример 2: ИНН 23101195600076
  Позиции: 2=3, 3=1, 4=0, 5=1, 6=1, 7=9, 8=5, 9=6
  ДД=31, ММ=01, ГГГГ=1956 → seller_birth_date = "31.01.1956"

- Пример 3: ИНН 20512199801234
  Позиции: 2=0, 3=5, 4=1, 5=2, 6=1, 7=9, 8=9, 9=8
  ДД=05, ММ=12, ГГГГ=1998 → seller_birth_date = "05.12.1998"

- ОБЯЗАТЕЛЬНАЯ ПРОВЕРКА: перед заполнением seller_birth_date напиши себе мысленно
  каждую цифру ИНН с её позицией (1,2,3...) и убедись что взял правильные позиции.

- ВАЖНО ПРО ТОЧНОСТЬ ИНН: OCR часто путает цифры в ИНН (например 3→1, 0→8, 6→5).
  Поэтому ВСЕГДА показывай пользователю ИНН который ты прочитал из документа и
  вычисленную из него дату рождения, и проси подтвердить:
  "ИНН продавца: XXXXXXXXXXXXXXX → дата рождения: ДД.ММ.ГГГГ — верно?"
  Если пользователь говорит что дата неверная — попроси ввести ИНН вручную.

ВАЖНО про seller_id_number, seller_id_issued_date, seller_id_issued_by (ID-карта продавца):
- В поле "4. Плательщик" ТПО ПОСЛЕ строки с адресом и СОАТЕ часто идёт отдельная строка вида:
    "ПАСПОРТ: ID 3155919, 13.01.2023, МКК 218061"
  (может встречаться написание "ПАСПОРТ:", даже если по факту это ID-карта КР — не паспорт).
- Из этой строки извлекай ВСЕ ТРИ значения:
  seller_id_number      — номер после "ID" (например "3155919")
  seller_id_issued_date — дата после номера ID (например "13.01.2023")
  seller_id_issued_by   — оставшаяся часть строки, обычно код подразделения вида "МКК 218061"
    (например "МКК 218061")
- ЭТИ ТРИ ЗНАЧЕНИЯ ВСЕГДА ЕСТЬ В ЭТОЙ СТРОКЕ ТПО. Если ты не нашёл seller_id_issued_date
  или seller_id_issued_by — перечитай поле 4 ТПО ещё раз построчно и найди строку, начинающуюся
  на "ПАСПОРТ:" — она идёт после строк "ИНН:", ФИО, "АДРЕС:" и "СОАТЕ:". НЕ оставляй эти поля
  пустыми и НЕ проси их у пользователя, если ТПО присутствует среди документов — все данные
  уже есть в этой строке.
- Пассажирская таможенная декларация (раздел "1. Сведения о декларанте") содержит ту же
  информацию отдельной строкой "ПАСПОРТ: ID ..., ОТ ..., МКК ..." — используй её для
  проверки/сверки с ТПО, если декларация тоже приложена.


5. Любой другой документ — извлеки все данные которые относятся к известным полям.

ВАЖНО: Если данные уже извлечены из документа — НЕ спрашивай их повторно у пользователя.

ПРАВИЛА:
1. car_price и cash_amount — РАЗНЫЕ суммы:
   - car_price   = цена в ДКП в РУБЛЯХ (например 4200000). Идёт только в ДКП.
   - cash_amount = сумма наличных которую агент передаёт продавцу в ДОЛЛАРАХ (например 54900). Идёт только в Поручение.
   - НИКОГДА не ставь car_price в поле cash_amount. Это всегда разные числа.
   - cash_amount_words — сумма прописью БЕЗ названия валюты. Только число словами: "Пятьдесят четыре тысячи девятьсот". Валюта указывается отдельно в поле cash_currency.

2. Если пользователь не назвал цвет автомобиля — обязательно спроси.
3. Если пользователь не назвал сумму наличных (cash_amount) отдельно — спроси.
4. Собери все недостающие поля в ОДНОМ вопросе, не задавай по одному.
5. Когда все обязательные поля собраны — ПЕРЕД вызовом create_contract выведи сводку всех данных в чат в таком формате и жди подтверждения:

📋 ПРОВЕРЬТЕ ДАННЫЕ ПЕРЕД СОЗДАНИЕМ ДОГОВОРА:

👤 ПОКУПАТЕЛЬ:
ФИО: ...
Дата рождения: ...
Адрес: ...
Паспорт: серия ... № ..., выдан: ..., дата: ..., код: ...

👤 ПРОДАВЕЦ:
ФИО: ...
Дата рождения: ...
Адрес: ...
Идентификационная карта № ..., выдана: ...

🚗 АВТОМОБИЛЬ:
Марка/модель: ...
VIN: ...
Год: ...
Цвет: ...

💰 ФИНАНСЫ:
Цена в ДКП (рублей): ...
Цена прописью: ...
Сумма наличными агенту (долларов): ...
Сумма наличными прописью (без валюты): ...
Комиссия: ...%

🏦 БАНКОВСКИЕ РЕКВИЗИТЫ:
Валюта счёта: ...
Номер счёта: ...
Банк-корреспондент: ...
Банк получателя: ...

Всё верно? Создаю договоры?

После этого жди ответа пользователя. Только если он подтвердил (написал "да", "верно", "создавай", "всё верно" или аналог) — спроси дату договора:
"📅 Дата договора: сегодня ({today_str}) или другая?"
Если пользователь говорит "сегодня" или аналог — используй {today_str}. Если называет другую дату — используй её. Передавай дату в параметр contract_date при вызове create_contract.

Если каких-то необязательных данных нет — оставь значение пустой строкой "".

Отвечай на русском языке. Будь краток и по делу."""

        instructions = memory.get_instructions()
        if instructions:
            base += "\n\nТВОИ ПОСТОЯННЫЕ ИНСТРУКЦИИ (всегда выполняй):\n"
            for i in instructions:
                base += f"- {i['text']}\n"

        companies = memory.list_companies()
        if companies:
            base += "\n\nСОХРАНЁННЫЕ РЕКВИЗИТЫ КОМПАНИЙ:\n"
            for c in companies:
                data = memory.get_company(c["name"])
                base += f"\n{c['name']}:\n"
                for k, v in data.items():
                    if v:
                        base += f"  {k}: {v}\n"

        bank_profile_names = memory.list_bank_profiles()
        base += "\n\n=== БАНКОВСКИЕ ПРОФИЛИ (реквизиты для зачисления денег продавцу) ===\n"
        if bank_profile_names:
            base += "Сохранённые профили:\n"
            for name in bank_profile_names:
                data = memory.get_bank_profile(name)
                base += f"\n{name}:\n"
                for k, v in data.items():
                    if v:
                        base += f"  {k}: {v}\n"
            base += (
                "\nПЕРЕД созданием сделки (create_contract), когда нужны банковские "
                "реквизиты (account_number, bank_corr_line1-3, bank_ben_line1-2, "
                "account_currency), ВСЕГДА вызывай инструмент request_bank_choice — "
                "он покажет пользователю кнопки с сохранёнными профилями + кнопку "
                "«Новые реквизиты». Не спрашивай это текстом, всегда через инструмент. "
                "После того как пользователь выберет вариант (его выбор придёт как "
                "обычное сообщение), либо подставь данные выбранного профиля в data, "
                "либо (если выбрано «Новые реквизиты») спроси реквизиты текстом и "
                "предложи сохранить их через save_bank_profile с понятным названием "
                "вида «Банк - Имя получателя» (например «Альфа Банк - Бакай»)."
            )
        else:
            base += (
                "Сохранённых профилей пока нет. ПЕРЕД созданием сделки, когда нужны "
                "банковские реквизиты, вызывай request_bank_choice — он покажет кнопку "
                "«Новые реквизиты» (других вариантов не будет). После того как "
                "пользователь укажет реквизиты текстом, предложи сохранить их через "
                "save_bank_profile с названием вида «Банк - Имя получателя»."
            )

        return base

    async def process_message(self, user_text: str, filepath: str = None, filename: str = None, chat_id: str = "") -> dict:
        self._current_chat_id = chat_id
        memory.add_to_history("user", user_text if not filepath else f"[файл: {filename}] {user_text}")

        history  = memory.get_history(limit=15)
        messages = []

        for h in history[:-1]:
            messages.append({"role": h["role"], "content": h["content"]})

        if filepath:
            current_content = await self._build_file_message(filepath, filename, user_text)
        else:
            current_content = user_text

        messages.append({"role": "user", "content": current_content})

        for attempt in range(3):
            try:
                response = await self.client.with_options(timeout=120.0).messages.create(
                    model=self.model,
                    max_tokens=1024,
                    system=self._build_system_prompt(),
                    tools=self._get_tools(),
                    messages=messages,
                )
                break
            except (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                logger.warning(f"Сетевая ошибка (попытка {attempt+1}/3): {e}")
                if attempt == 2:
                    return {"text": "⚠️ Ошибка соединения с AI. Попробуйте ещё раз.", "files": [], "success": False}
                await asyncio.sleep(2)

        try:
            result = await self._handle_response(response)
        except Exception as e:
            logger.error(f"Необработанная ошибка при выполнении инструмента: {e}", exc_info=True)
            result = {
                "text": f"⚠️ Произошла ошибка при обработке: {e}",
                "files": [],
                "success": False,
            }
        memory.add_to_history("assistant", result.get("text", ""))
        return result

    def _get_tools(self) -> list:
        return [
            {
                "name": "create_contract",
                "description": "Создать полный пакет документов по сделке (АГ договор, ДКП ТС, Счёт на оплату). ВСЕГДА спрашивай размер комиссии в процентах перед вызовом если он не указан.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "data": {
                            "type": "object",
                            "description": "Данные для заполнения документов — строго по ключам из системного промпта"
                        },
                        "contract_date":   {"type": "string",  "description": "Дата договора в формате ДД.ММ.ГГГГ (опционально, если не указана — используется сегодняшняя дата)"},
                        "commission_pct":  {"type": "number",  "description": "Комиссия агента в процентах, например 2.0"},
                    },
                    "required": ["data", "commission_pct"],
                },
            },
            {
                "name": "save_company",
                "description": "Сохранить реквизиты компании или клиента в память",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "data": {"type": "object"},
                    },
                    "required": ["name", "data"],
                },
            },
            {
                "name": "save_instruction",
                "description": "Сохранить постоянную инструкцию для агента",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                    },
                    "required": ["text"],
                },
            },
            {
                "name": "save_bank_profile",
                "description": (
                    "Сохранить набор банковских реквизитов для зачисления денег продавцу "
                    "под понятным названием, например «Альфа Банк - Бакай»."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Название профиля, например «Альфа Банк - Бакай»",
                        },
                        "data": {
                            "type": "object",
                            "description": (
                                "Реквизиты: account_number, account_currency, "
                                "bank_corr_line1, bank_corr_line2, bank_corr_line3, "
                                "bank_ben_line1, bank_ben_line2"
                            ),
                        },
                    },
                    "required": ["name", "data"],
                },
            },
            {
                "name": "request_bank_choice",
                "description": (
                    "Показать пользователю кнопки для выбора банковских реквизитов: "
                    "сохранённые профили + кнопка «Новые реквизиты». Вызывай ВСЕГДА "
                    "перед созданием сделки, когда нужны банковские реквизиты, вместо "
                    "того чтобы спрашивать текстом."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "Короткий текст-подпись над кнопками, например «Какие реквизиты использовать для этой сделки?»",
                        },
                    },
                    "required": ["message"],
                },
            },
        ]

    async def _handle_response(self, response) -> dict:
        result = {"text": "", "files": [], "success": True, "buttons": None}

        for block in response.content:
            if block.type == "text":
                result["text"] += block.text

            elif block.type == "tool_use":
                tool_result = await self._execute_tool(block.name, block.input)

                if tool_result.get("file"):
                    result["files"].append({
                        "file":       tool_result["file"],
                        "filename":   tool_result["filename"],
                        "drive_link": tool_result.get("drive_link", ""),
                    })

                for f_path, f_name in zip(
                    tool_result.get("extra_files", []),
                    tool_result.get("extra_names", []),
                ):
                    if Path(f_path).exists():
                        result["files"].append({
                            "file":       f_path,
                            "filename":   f_name,
                            "drive_link": "",
                        })

                if tool_result.get("buttons"):
                    result["buttons"] = tool_result["buttons"]
                    if tool_result.get("message"):
                        result["text"] += f"\n{tool_result['message']}"
                elif tool_result.get("message"):
                    msg = tool_result["message"]
                    prefix = "" if msg.startswith(("⚠️", "❌", "✅")) else "✅ "
                    result["text"] += f"\n{prefix}{msg}"

        return result

    async def _execute_tool(self, tool_name: str, tool_input: dict) -> dict:

        if tool_name == "create_contract":
            contract_date = tool_input.get("contract_date") or None
            number = await self.drive.get_next_contract_number(contract_date)

            logger.debug("DATA KEYS: " + str(list(tool_input.get("data", {}).keys())))

            date           = tool_input.get("contract_date") or datetime.now().strftime("%d.%m.%Y")
            commission_pct = float(tool_input.get("commission_pct", 1.0))
            deal_folder_id = await self.drive.get_or_create_deal_folder(number)

            # ── 0. Загружаем pending_scans в папку Сканы ──────────────────
            chat_id = getattr(self, "_current_chat_id", "")
            scans = memory.get_pending_scans(chat_id) if chat_id else []
            if scans:
                scans_folder_id = await self.drive._get_or_create_folder("Сканы", deal_folder_id)
                uploaded_scans = 0
                for scan in scans:
                    if Path(scan["filepath"]).exists():
                        await self.drive.upload_file(scan["filepath"], scan["original_name"], scans_folder_id)
                        uploaded_scans += 1
                    else:
                        logger.warning(f"Файл скана не найден: {scan['filepath']}")
                memory.clear_pending_scans(chat_id)
                logger.info(f"Загружено {uploaded_scans} сканов в папку сделки {number}")

            # ── 1. Строим документы ПОСЛЕДОВАТЕЛЬНО (не параллельно — меньше памяти) ──
            built = {}
            try:
                logger.info("Строю АГ договор...")
                built["ag"] = await self.builder.build_contract(tool_input["data"], number, date, commission_pct)

                logger.info("Строю ДКП...")
                built["dkp"] = await self.builder.build_dkp(tool_input["data"], number, date)

                logger.info("Строю счёт...")
                built["invoice"] = await self.builder.build_invoice(tool_input["data"], number, date, commission_pct)

            except Exception as e:
                logger.error(f"Ошибка построения документов для сделки {number}: {e}", exc_info=True)
                files_done = list(built.keys())
                return {
                    "message": (
                        f"⚠️ Ошибка при создании документов сделки {number}: {e}\n"
                        f"Успешно создано: {', '.join(files_done) if files_done else 'ничего'}"
                    ),
                }

            ag_path, dkp_path, invoice_path = built["ag"], built["dkp"], built["invoice"]

            ag_docx  = f"АГ_Договор_{number}.docx"
            dkp_docx = f"ДКП_ТС_{number}.docx"
            inv_xlsx = f"Счёт_{number}.xlsx"
            ag_pdf   = f"АГ_Договор_{number}.pdf"
            dkp_pdf  = f"ДКП_ТС_{number}.pdf"
            inv_pdf  = f"Счёт_{number}.pdf"

            # ── 2. Загружаем docx/xlsx на Drive ПОСЛЕДОВАТЕЛЬНО ────────────
            # (параллельные запросы через httplib2/googleapiclient в разных
            #  потоках вызывают сегфолт "double free or corruption")
            logger.info("Загружаю на Drive...")
            for fpath, fname in (
                (ag_path,      ag_docx),
                (dkp_path,     dkp_docx),
                (invoice_path, inv_xlsx),
            ):
                try:
                    await self.drive.upload_file(fpath, fname, deal_folder_id)
                except Exception as e:
                    logger.error(f"Ошибка загрузки на Drive (сделка {number}, файл {fname}): {e}", exc_info=True)

            # ── 3. PDF только если не отключён через SKIP_PDF=1 ───────────
            skip_pdf = os.environ.get("SKIP_PDF", "0") == "1"
            ag_pdf_path = dkp_pdf_path = inv_pdf_path = None
            ag_link = ""

            if not skip_pdf:
                logger.info("Конвертирую в PDF...")
                ag_pdf_path  = await self.builder.convert_to_pdf(ag_path)
                dkp_pdf_path = await self.builder.convert_to_pdf(dkp_path)
                inv_pdf_path = await self.builder.convert_to_pdf(invoice_path)

                if ag_pdf_path:
                    ag_link = await self.drive.upload_file(ag_pdf_path, ag_pdf, deal_folder_id)
                if dkp_pdf_path:
                    await self.drive.upload_file(dkp_pdf_path, dkp_pdf, deal_folder_id)
                if inv_pdf_path:
                    await self.drive.upload_file(inv_pdf_path, inv_pdf, deal_folder_id)
            else:
                logger.info("PDF пропущен (SKIP_PDF=1)")

            # ── 4. Собираем файлы для отправки в Telegram ─────────────────
            extra_files = []
            extra_names = []

            if ag_pdf_path and Path(ag_pdf_path).exists():
                extra_files.append(ag_pdf_path);   extra_names.append(ag_pdf)

            if Path(dkp_path).exists():
                extra_files.append(dkp_path);      extra_names.append(dkp_docx)
            if dkp_pdf_path and Path(dkp_pdf_path).exists():
                extra_files.append(dkp_pdf_path);  extra_names.append(dkp_pdf)

            if Path(invoice_path).exists():
                extra_files.append(invoice_path);  extra_names.append(inv_xlsx)
            if inv_pdf_path and Path(inv_pdf_path).exists():
                extra_files.append(inv_pdf_path);  extra_names.append(inv_pdf)

            total_files = 1 + len(extra_files)
            pdf_note = " (PDF отключён)" if skip_pdf else ("" if ag_pdf_path else " (LibreOffice недоступен)")

            return {
                "file":        ag_path,
                "filename":    ag_docx,
                "extra_files": extra_files,
                "extra_names": extra_names,
                "drive_link":  ag_link,
                "message":     f"Сделка {number}: {total_files} файлов отправлено{pdf_note}",
            }

        elif tool_name == "save_company":
            memory.save_company(tool_input["name"], tool_input["data"])
            return {"message": f"Реквизиты «{tool_input['name']}» сохранены"}

        elif tool_name == "save_instruction":
            memory.add_instruction(tool_input["text"])
            return {"message": f"Инструкция сохранена: {tool_input['text']}"}

        elif tool_name == "save_bank_profile":
            memory.save_bank_profile(tool_input["name"], tool_input["data"])
            return {"message": f"Реквизиты «{tool_input['name']}» сохранены как банковский профиль"}

        elif tool_name == "request_bank_choice":
            profiles = memory.list_bank_profiles()
            buttons = [{"text": name, "callback_data": f"bankprofile:{name}"} for name in profiles]
            buttons.append({"text": "🆕 Новые реквизиты", "callback_data": "bankprofile:__new__"})
            return {
                "message": tool_input.get("message", "Какие реквизиты использовать?"),
                "buttons": buttons,
                "no_check": True,
            }

        return {"message": "Выполнено"}

    async def _build_file_message(self, filepath: str, filename: str, user_text: str) -> list:
        ext     = Path(filename).suffix.lower()
        content = []

        if ext in [".jpg", ".jpeg", ".png", ".webp"]:
            with open(filepath, "rb") as f:
                data = base64.standard_b64encode(f.read()).decode("utf-8")
            media_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                         "png": "image/png",  "webp": "image/webp"}
            content.append({
                "type": "image",
                "source": {
                    "type":       "base64",
                    "media_type": media_map.get(ext.strip("."), "image/jpeg"),
                    "data":       data,
                },
            })

        elif ext == ".pdf":
            with open(filepath, "rb") as f:
                data = base64.standard_b64encode(f.read()).decode("utf-8")
            content.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": data},
            })

        else:
            try:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()[:5000]
                content.append({"type": "text", "text": f"Содержимое файла {filename}:\n{text}"})
            except Exception:
                pass

        prompt = user_text or "Извлеки все данные из этого документа: реквизиты, данные об автомобиле, суммы."
        content.append({"type": "text", "text": prompt})
        return content

    async def process_file(self, filepath: str, filename: str, caption: str = "", chat_id: str = "") -> dict:
        return await self.process_message(
            caption or "Извлеки все данные из документа и скажи что нашёл.",
            filepath=filepath,
            filename=filename,
            chat_id=chat_id,
        )
