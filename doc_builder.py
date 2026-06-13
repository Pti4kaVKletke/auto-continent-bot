def replace_in_para(para):
    p_elem = para._element
    runs   = p_elem.findall(f"{{{W}}}r")
    if not runs:
        return

    full_text = "".join(
        t.text or ""
        for r in runs
        for t in r.findall(f"{{{W}}}t")
    )
    if not any(ph in full_text for ph in replacements):
        return

    # Найти один run, который содержит ВСЕ плейсхолдеры этого параграфа —
    # обычно так и есть. Заменяем текст внутри него на месте, сохраняя
    # его собственное форматирование (bold/etc) и НЕ трогая форматирование
    # соседних runs (например жирный номер пункта "2.", "4.", "6.").
    target_run = None
    for r in runs:
        r_text = "".join(t.text or "" for t in r.findall(f"{{{W}}}t"))
        if any(ph in r_text for ph in replacements):
            target_run = r
            break

    if target_run is not None:
        for t in target_run.findall(f"{{{W}}}t"):
            if t.text:
                new_text = t.text
                for ph, val in replacements.items():
                    new_text = new_text.replace(ph, str(val) if val is not None else "")
                t.text = new_text
                if new_text and (new_text[0] == " " or new_text[-1] == " "):
                    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        return

    # Фоллбэк: плейсхолдер разорван между несколькими runs —
    # сливаем как раньше (форматирование первого run).
    first_rpr  = runs[0].find(f"{{{W}}}rPr")
    children   = list(p_elem)
    insert_idx = children.index(runs[0])

    new_text = full_text
    for ph, val in replacements.items():
        new_text = new_text.replace(ph, str(val) if val is not None else "")

    for r in runs:
        p_elem.remove(r)

    new_run = etree.Element(f"{{{W}}}r")
    if first_rpr is not None:
        new_run.append(deepcopy(first_rpr))
    new_t = etree.SubElement(new_run, f"{{{W}}}t")
    new_t.text = new_text
    if new_text and (new_text[0] == " " or new_text[-1] == " "):
        new_t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")

    p_elem.insert(insert_idx, new_run)
