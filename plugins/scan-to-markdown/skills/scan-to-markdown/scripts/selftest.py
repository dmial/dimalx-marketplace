#!/usr/bin/env python3
"""Self-test of the logic that turns decisions and corrections into the final text.

Invariants:
  * choosing A everywhere reproduces text A character for character;
  * choosing B everywhere reproduces text B: character for character on the built-in cases
    (their agreed parts are identical), and as the same sequence of words, numbers, signs,
    brackets, colons, asterisks and minus signs before numbers on real pages (agreed parts may
    differ in spacing there);
  * a colon, bracket or minus that differs between agreed words is a discrepancy of its own; next
    to a differing word it joins that discrepancy; choosing B there keeps A's words;
  * any mix of choices keeps every table row whole: each line with a cell border is built like some
    line of A or B (a "|" at its start or not, at its end or not, as many "|"). Every mix is tried on
    up to ten discrepancies; beyond that, every mix of each four neighbouring ones, the others all A
    or all B, so B everywhere is always among them;
  * a border only one reader wrote at the end of a row stays with that row, not with the next;
  * build.py takes for agreed exactly the tokens compare.py matched;
  * a reviewer's correction replaces exactly its fragment and moves later marks along;
  * an unreadable number inside an uncertainty mark ([?4]) gets no second mark;
  * Vision's forms of a number count as seeing it: "3,24" for "3, 24", "СТЗ" for "СТ3", "125.1" for "I25.1",
    "1019" for "10^9";
  * stamps, logos and requisite lines fall into the service zone, test results do not;
  * in the Markdown file an asterisk that starts a line is escaped, so it is not taken for a list;
  * crops hold the spot: a stamp over its owner's printed name, a note in the margin, a row whose
    line a short number inside a code could take, a date on a line without words.
A failure names the case and shows both texts only around the first place where they part.

Usage: python3 selftest.py   (inside a project it also checks every page that has both readings)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # lets `python3 -I` find common.py
from build import Edit, agreed_ranges, apply_corrections, apply_edits, markdown_safe, plan_edits  # noqa: E402
from common import (  # noqa: E402
    ROOT,
    codes,
    in_ranges,
    load_manifest,
    load_text,
    number_forms,
    page_paths,
    service_ranges,
    tokenize,
    vision_support,
)
from compare import Text, diff_units, edit_for_unit, looked_for, place, place_check  # noqa: E402

# All sample data below is made up; none of it comes from real documents.
CASES = [
    ("handwritten mark differs", "Показатель [от руки: ✓] | 4.87 |", "Показатель [от руки: пометка] | 4.87 |"),
    ("uncertain digit only in A", "Сертификат: 1234567[?8][обрезано]\nВладелец", "Сертификат: 1234567[обрезано]\nВладелец"),
    ("extra table row in B", "Итог\n| a | 1 |\n| b | 2 |", "Итог\n| a | 1 |\n| x | 9 |\n| b | 2 |"),
    ("illegible line only in A", "[неразборчиво]\n\nНазвание учреждения", "Название учреждения"),
    ("number misread", "foo 4.87 ммоль/л bar", "foo 4.82 ммоль/л bar"),
    ("two differences in one stamp", "[печать: ООО Лаборатория 123 и 456]\nдалее", "[печать: ООО Лаборатори 128 и 459]\nдалее"),
    ("nested marks in a stamp", "ОГРН [печать: ИНН 7700 ОГРН 1027[неразборчиво]000 ДЛЯ]", "ОГРН [печать: ИНН [?7700] ОГРН [неразборчиво] [?Для]]"),
    ("mark at line start only in B", "Отчет\nврач подпись", "Отчет\n[обрезано] врач подпись"),
    ("triangle flag differs", "| Показатель ▲ | 7.40 |", "| Показатель | 7.40 |"),
    ("whole line only in B", "один\nтри", "один\nдва 2\nтри"),
    ("empty A", "", "текст 1"),
    ("only B unsure", "Сайт: www.example-lab.ru", "Сайт: [?www.example-lab.ru]"),
    ("only A unsure about a flag", "| строка [?▲] [от руки: ✓] | 4.87 |", "| строка ▲ [от руки: ✓] | 4.87 |"),
    ("different hand-drawn signs", "[от руки: ✓] Кровь", "[от руки: →] Кровь"),
    ("cut-off mark before a symbol", "имя\n№: 12345", "имя\n[обрезано]№: 12345"),
    ("icon in parentheses only in A", "Анализ (?)\nМетод", "Анализ\nМетод"),
    ("reference range misread", "| 4.87 | (3.5 - 5.0) |", "| 4.87 | (3.5 - 5.8) |"),
    ("stray parenthesis inside a stamp", "[печать: ООО (Лаборатория 12] далее", "[печать: ООО (Лаборатория 13] далее"),
    ("mark in the cell after a range", "| x | (3.5 - 5.0) | [от руки: пометка] | |", "| x | (3.5 - 5.0) | | |"),
    ("icon after a parenthesised word", "Витамин (название) [изображение: ?]\nМетод", "Витамин (название)\nМетод"),
    ("flag in another column", "| строка ▲ [от руки: ✓] | 4.87 | ммоль/л |", "| строка | ▲ [от руки: ✓] 4.87 | ммоль/л |"),
    ("value in the reference column", "| Показатель | 4.87 | (3.5 - 5.0) |", "| Показатель | | 4.87 (3.5 - 5.0) |"),
    ("two border moves in one row", "| строка [?▲] [от руки: ✓] | [от руки: ↑] 4.87 |", "| строка | ▲ [от руки: пометка] [от руки: ↑] 4.87 |"),
    ("colon inside a mark only in A", "[обрезано][?азн:] 12.03.2024 10:20:30", "[обрезано][?дан]: 12.03.2024 10:20:30"),
    ("colon inside a mark only in B", "[?дан]: 12.03.2024", "[?азн:] 12.03.2024"),
    ("footnote star inside a mark only in B", "[печать: Лаборатория № 12]\n\n* Время указано", "[печать: Лаборатория № 12]\n\n[?*] Время указано"),
    ("star flag only in A", "| Глюкоза | 6.70 * | ммоль/л |", "| Глюкоза | 6.70 | ммоль/л |"),
    ("word with a colon moved to the next line", "Тип исследования: Рентген грудной клетки\nДоза: 0.1", "Тип Рентген грудной клетки\nисследования:\nДоза: 0.1"),
    ("word with a colon moved into the line", "Тип Рентген грудной клетки\nисследования:\nДоза: 0.1", "Тип исследования: Рентген грудной клетки\nДоза: 0.1"),
    ("word before a colon only in A", "Дата взятия: 12.03.2024", "Дата: 12.03.2024"),
    ("word before a colon only in B", "Дата: 12.03.2024", "Дата взятия: 12.03.2024"),
    ("value after a colon only in B", "Анамнез:\nДиагноз: нет", "Анамнез: без особенностей\nДиагноз: нет"),
    ("start of a range only in A", "доза 5-10 мг", "доза 10 мг"),
    ("colon only in B between agreed words", "Анамнез гипертония", "Анамнез: гипертония"),
    ("colon only in A between agreed words", "Анамнез: гипертония", "Анамнез гипертония"),
    ("colon at a line end only in B", "Заключение\nПатологии не выявлено", "Заключение:\nПатологии не выявлено"),
    ("colon at the text end only in B", "Подпись врача", "Подпись врача:"),
    ("two colons of a time only in A", "Время 10:20:30", "Время 10 20 30"),
    ("colon only in A before a misread word", "Анамнез: гипертония", "Анамнез гипортония"),
    ("colon only in B before a misread word", "Анамнез гипертония", "Анамнез: гипортония"),
    ("time colon only in A next to a misread number", "Время 10:20", "Время 11 20"),
    ("time colon only in B next to a misread number", "Время 11 20", "Время 10:20"),
    ("closing bracket in another place", "Рост [от руки: 12] мм", "Рост [от руки: 12 мм]"),
    ("signature mark only in B", "Подпись врача подпись", "Подпись врача [подпись]"),
    ("empty checkbox only in A at the text start", "[ ] Да\nНет", "Да\nНет"),
    ("minus only in A in a table", "| Показатель | -2.1 | ммоль/л |", "| Показатель | 2.1 | ммоль/л |"),
    ("minus only in B", "Ось 30°", "Ось -30°"),
    ("minus and a misread number in one row", "| Показатель | -2.1 | 4.87 |", "| Показатель | 2.1 | 4.82 |"),
    ("last border of a row only in A", "| Глюкоза | 5.1 |\n| Белок | 70 |", "| Глюкоза | 5.1\n| Белок | 70 |"),
    ("last border of a row only in B", "| Глюкоза | 5.1\n| Белок | 70 |", "| Глюкоза | 5.1 |\n| Белок | 70 |"),
    ("first border of a row only in A", "| Глюкоза | 5.1 |\n| Белок | 70 |", "| Глюкоза | 5.1 |\nБелок | 70 |"),
    ("last border only in A, next row misread", "| Глюкоза | 5.1 |\n| Белок | 70 |", "| Глюкоза | 5.1\n| Белок | 72 |"),
    ("separator row one border short in B", "| Показатель | Значение |\n|---|---|\n| Глюкоза | 5.1 |",
     "| Показатель | Значение |\n|---|---\n| Глюкоза | 5.1 |"),
    ("separator row one border short in A", "| Показатель | Значение |\n|---|---\n| Глюкоза | 5.1 |",
     "| Показатель | Значение |\n|---|---|\n| Глюкоза | 5.1 |"),
]
# Cases where line breaks legitimately stay as in A, so B is matched by structure, not character by character.
LAYOUT_CASES = [
    ("signature moved", "12 мес\n\n[подпись]\nврач ( терапевт )", "12 мес\n\nврач [подпись] ( терапевт )"),
]
# Pairs that must produce no discrepancy at all: they differ only in ways the comparison ignores.
SAME = [
    ("homoglyphs and decimal comma", "Т3(общ) = 2,45 нмоль/л", "T3(общ) = 2.45 нмоль/л"),
    ("table spacing and case", "| Показатель | 128 г/л |", "|Показатель|128 Г/Л|"),
    ("superscript in a unit", "Объем 331 см³.", "Объем 331 см3."),
    ("spaces around a colon", "Анамнез : гипертония", "Анамнез: гипертония"),
    ("periods and commas", "Без изменений, норма.", "Без изменений норма"),
    ("dash of a range", "Норма 5-10 мг", "Норма 5 - 10 мг"),
    ("forms of a minus", "Ось -30°", "Ось −30°"),
]


def squeeze(text: str) -> str:
    """Text with blank lines and repeated spaces collapsed, no spaces around [ ] (they carry no data)."""
    text = re.sub(r"\n\s*\n", "\n\n", text).strip()
    return re.sub(r" *([\[\]]) *", r"\1", re.sub(r" +", " ", text))


STRUCTURE_RE = re.compile(r"[\[\]:*]|(?<![\w.,])[-−–](?=\d)")  # a minus right before a number, not in "5-10"
MIX_ALL = 10  # up to this many discrepancies on a page the row check tries every mix of choices
MIX_RUN = 4  # beyond, every mix of each run of this many neighbouring discrepancies


def structure(text: str) -> list[tuple[int, str]]:
    """Words, numbers and signs together with brackets, colons, asterisks and minus signs before
    numbers (any kind of minus as "-"), in order, with positions."""
    marks = [(m.start(), "-" if m.group() in "−–" else m.group()) for m in STRUCTURE_RE.finditer(text)]
    return sorted(set(marks + [(t.start, t.norm) for t in tokenize(text)]))


def excerpt(text: str, pos: int, width: int = 40) -> str:
    return text[max(0, pos - width):pos + width].replace("\n", "⏎")


def first_difference(got: str, expected: str) -> str:
    """Both texts around the first character where they part."""
    pos = next((i for i, (x, y) in enumerate(zip(got, expected)) if x != y), min(len(got), len(expected)))
    return f"at char {pos}: got «{excerpt(got, pos)}», expected «{excerpt(expected, pos)}»"


def structure_difference(got: str, expected: str) -> str | None:
    """None if both texts have the same structure, else both around the first place where it parts."""
    a, b = structure(got), structure(expected)
    if [n for _, n in a] == [n for _, n in b]:
        return None
    k = next((i for i, (x, y) in enumerate(zip(a, b)) if x[1] != y[1]), min(len(a), len(b)))
    pos_a = a[k][0] if k < len(a) else len(got)
    pos_b = b[k][0] if k < len(b) else len(expected)
    item = lambda seq: f"«{seq[k][1]}»" if k < len(seq) else "the end"
    return f"{item(a)} where text B has {item(b)}: got «{excerpt(got, pos_a)}», expected «{excerpt(expected, pos_b)}»"


def row_shapes(text: str) -> set[tuple[bool, bool, int]]:
    """How each line with a cell border is built: whether a "|" opens it, whether one closes it, and
    how many "|" it has."""
    lines = [line.strip() for line in text.split("\n")]
    return {(line.startswith("|"), line.endswith("|"), line.count("|")) for line in lines if "|" in line}


def mixes(count: int) -> list[int]:
    """The mixes of choices to try, as bit masks (bit n set: B for discrepancy n): every mix up to
    MIX_ALL discrepancies; beyond, every mix of each MIX_RUN neighbouring ones, the others all A or
    all B. So every discrepancy is tried with each choice next to each choice of its neighbours,
    and B everywhere is among the mixes."""
    if count <= MIX_ALL:
        return list(range(2 ** count))
    runs = {m << start for start in range(count - MIX_RUN + 1) for m in range(2 ** MIX_RUN)}
    return sorted(runs | {((1 << count) - 1) ^ m for m in runs})


def broken_row(a_text: str, b_text: str, edits: list[dict]) -> str | None:
    """The first mix of choices that gives a line with cell borders built as no line of A or B is
    (another number of "|", a row that lost its first or last "|"), with that line; None if every
    mix keeps table rows whole."""
    allowed = row_shapes(a_text) | row_shapes(b_text)
    for mask in mixes(len(edits)):
        mixed = [
            Edit(e["a_span"][0], e["a_span"][1], e["b_text"] if mask >> n & 1 else a_text[e["a_span"][0]:e["a_span"][1]], "resolved", f"u{n}")
            for n, e in enumerate(edits)
        ]
        final_mix, _, _ = apply_edits(a_text, mixed, 1000, 1000)
        wrong = next((line for line in final_mix.split("\n") if not row_shapes(line) <= allowed), None)
        if wrong is not None:
            choice = "".join("B" if mask >> n & 1 else "A" for n in range(len(edits)))
            return f"choice {choice} broke a table row: «{wrong.strip()}»"
    return None


def run_case(name: str, a_text: str, b_text: str, exact: bool = True) -> list[str]:
    a, b = Text(a_text), Text(b_text)
    units, a2b, _ = diff_units(a, b)
    edits = [edit_for_unit(a, b, u) for u in units]
    errors = []
    if agreed_ranges(a_text, b_text) != {(a.tokens[k].start, a.tokens[k].end) for k in a2b}:
        errors.append(f"{name}: build.py takes other tokens for agreed than compare.py")

    keep_a = [Edit(e["a_span"][0], e["a_span"][1], a_text[e["a_span"][0]:e["a_span"][1]], "resolved", f"u{n}") for n, e in enumerate(edits)]
    final_a, _, problems = apply_edits(a_text, keep_a, 1000, 1000)
    if problems or final_a != a_text:
        errors.append(f"{name}: choosing A changed the text: {problems or repr(final_a)}")

    broken = broken_row(a_text, b_text, edits)
    if broken:
        errors.append(f"{name}: {broken}")

    take_b = [Edit(e["a_span"][0], e["a_span"][1], e["b_text"], "resolved", f"u{n}") for n, e in enumerate(edits)]
    final_b, _, problems = apply_edits(a_text, take_b, 1000, 1000)
    if exact:
        differs = None if squeeze(final_b) == squeeze(b_text) else first_difference(squeeze(final_b), squeeze(b_text))
    else:
        differs = structure_difference(final_b, b_text)
    if problems or differs:
        errors.append(f"{name}: choosing B does not give text B {differs or ''} {problems or ''}".rstrip())
    return errors


def check_rows() -> list[str]:
    """The row check sees a row that lost its first border, and two rows joined by the last of
    twelve discrepancies on a page; numbers changed in rows break nothing."""
    errors = []
    table = "| Глюкоза | 5.1 |\n| Белок | 70 |"
    page = "".join(f"Показатель {n}: {n}.5\n" for n in range(1, 12)) + table
    numbers = [{"a_span": [page.index(f"{n}.5"), page.index(f"{n}.5") + 3], "b_text": f"{n}.6"} for n in range(1, 12)]
    joint = page.index("|\n| Белок")
    for name, text, edits, expected in [
        ("first border removed", table, [{"a_span": [table.index("| Белок"), table.index("Белок")], "b_text": ""}], True),
        ("rows joined by the twelfth edit", page, numbers + [{"a_span": [joint, joint + 3], "b_text": "|"}], True),
        ("numbers changed", page, numbers, False),
    ]:
        if (broken_row(text, text, edits) is not None) != expected:
            errors.append(f"row check on «{name}»: expected {'a broken row' if expected else 'none'}")
    return errors


def check_corrections() -> list[str]:
    errors = []
    text = "Показатель 4.87 ммоль/л\nДругой [?7.40] ммоль/л"
    mark = text.index("[?7.40]")
    segments = [{"start": mark, "end": mark + len("[?7.40]"), "status": "illegible", "id": "d1", "kept": []}]
    fixed, segs, problems = apply_corrections(text, segments, [{"find": "4.87", "replace": "14.87"}], 1000, 1000)
    later = next((seg for seg in segs if seg["id"] == "d1"), None)
    if problems or fixed != text.replace("4.87", "14.87") or later is None or fixed[later["start"]:later["end"]] != "[?7.40]":
        errors.append(f"correction did not move later marks correctly: {fixed!r} {segs}")
    fixed, segs, problems = apply_corrections(text, segments, [{"find": "[?7.40]", "replace": "7.40"}], 1000, 1000)
    if problems or "[?" in fixed or [s["status"] for s in segs] != ["human"]:
        errors.append(f"correction over an arbiter mark did not replace it: {fixed!r} {segs}")
    _, _, problems = apply_corrections(text, [], [{"find": "ммоль/л", "replace": "мкмоль/л"}], 1000, 1000)
    if not problems:
        errors.append("an ambiguous correction (fragment occurs twice) was applied instead of reported")
    return errors


def check_marks() -> list[str]:
    """An unreadable number inside a mark that already says "not sure" keeps a single mark."""
    errors = []
    text = "Сертификат: 1234567[?4][обрезано]\nВладелец"
    s = text.index("[?4]") + 2
    compare = {"discrepancies": [{"id": "d01", "kind": "vision_check", "needs_decision": True,
                                  "edit": {"mode": "inline", "a_span": [s, s + 1], "b_text": "4"}}]}
    for verdict, said in (("illegible", "4"), ("illegible", "[?4]"), ("own", "[?1]")):
        decision = {"id": "d01", "verdict": verdict, "text": said, "region": [10, 12, 0, 50]}
        edits, problems, _ = plan_edits(text, compare, {"d01": decision})
        final, _, more = apply_edits(text, edits, 1000, 1000)
        expected = text.replace("[?4]", "[?1]") if verdict == "own" else text
        if problems or more or final != expected:
            errors.append(f"{verdict} «{said}» inside [?4] gave {final!r}, expected {expected!r}")
    return errors


def check_vision_forms() -> list[str]:
    """The forms in which Vision may show a number the readers wrote."""
    errors = []

    def seen(text: str, number: str, vision: str) -> bool:
        tokens = tokenize(text)
        k = next(i for i, t in enumerate(tokens) if t.raw == number)
        obs = {"text": vision, "bbox": [0, 100, 500, 30], "norms": {t.norm for t in tokenize(vision)}, "codes": codes(vision)}
        return vision_support({"observations": [obs], "line_height": 30.0}, text, tokens, k, 1000)

    for text, number, vision, expected in [
        ("| Т3 св | 3, 24 | пмоль/л |", "24", "3,24 пмоль/л", True),
        ("| Т3 св | 3, 24 | пмоль/л |", "24", "3,21 пмоль/л", False),
        ("трийодтиронина (СТ3) в крови", "3", "трийодтиронина (СТЗ) в крови", True),
        ("Диагноз:I25.1. Атеросклероз", "25.1", "Диагноз:125.1. Атеросклероз", True),
        ("Диагноз:I25.1. Атеросклероз", "25.1", "Диагноз:126.1. Атеросклероз", False),
        ("| Лейкоциты | 6.30 *10^9/л | 4.0 - 9.0 |", "9", "6.30 *1019/л", True),
        ("| Лейкоциты | 6.30 *10^9/л | 4.0 - 9.0 |", "10", "6.30 *1019/л", True),
        ("| Лейкоциты | 6.30 *10^9/л | 4.0 - 9.0 |", "9", "6.30 *1012/л", False),
    ]:
        if seen(text, number, vision) != expected:
            errors.append(f"Vision reading «{vision}» of «{number}» in «{text}»: expected seen={expected}")
    sample = "Диагноз:I11.9; результат 15, 00; лейкоциты 6.30 *10^9/л"
    forms = number_forms(sample, tokenize(sample))
    if not {"111.9", "15.00", "1019"} <= forms:
        errors.append(f"number forms miss Vision's readings: {sorted(forms)}")
    return errors


def check_zones() -> list[str]:
    errors = []
    text = ("| Показатель | 4.87 | (3.5 - 5.0) |\n"
            "[печать: ОГРН 1027700000000]\n"
            "Лицензия № Л000-00000-00 от 01.02.2020\n"
            "Сертификат: 1234567890[?8]\n"
            "Дата рождения: 01.01.1970\n"
            "- Диета №5, выданы рекомендации. Подробнее https://example.org")
    zones = service_ranges(text)
    for fragment, zone in [("4.87", "content"), ("1027700000000", "service"), ("01.02.2020", "service"),
                           ("[?8]", "service"), ("01.01.1970", "content"), ("5", "content"),
                           ("https://example.org", "service")]:
        start = text.index(fragment, text.index("Диета №") if fragment == "5" else 0)
        got = "service" if in_ranges(start, start + len(fragment), zones) else "content"
        if got != zone:
            errors.append(f"zone of «{fragment}» is {got}, expected {zone}")
    return errors


def check_markdown() -> list[str]:
    """Footnote asterisks at a line start are escaped for Markdown; asterisks elsewhere stay."""
    text = "Глюкоза 6.70 *\n* Примечание\n** Второе примечание\n| x | 1.5 * |"
    expected = "Глюкоза 6.70 *\n\\* Примечание\n\\*\\* Второе примечание\n| x | 1.5 * |"
    got = markdown_safe(text)
    return [] if got == expected else [f"Markdown escaping gave {got!r}, expected {expected!r}"]


def check_places() -> list[str]:
    """Crops hold the spot, on a made-up page 2000 px high with lines 30 px high. A spot is (x0, y0,
    x1, y1) on the scan; a framed box must hold it, a row crop (vision checks) or a band must hold
    its lines."""
    errors = []

    def where(a_text: str, b_text: str, seen: list[tuple[str, int, int, int]], fragment: str) -> tuple:
        """compare.py's place of the discrepancy at `fragment` of A, or of that agreed number if none."""
        ocr = {"observations": [{"text": t, "bbox": [x, y, w, 30], "norms": {k.norm for k in tokenize(t)}, "codes": codes(t)}
                                for t, x, y, w in seen], "line_height": 30.0}
        a, b = Text(a_text), Text(b_text)
        start = a_text.index(fragment)
        unit = next((u for u in diff_units(a, b)[0] if u.sa <= start < u.ea), None)
        if unit:
            interest, marks = looked_for(a, b, unit)
            return place(a, ocr, 2000, interest, unit.i1, unit.i2, unit.sa, marks)
        k = next(i for i, t in enumerate(a.tokens) if t.start == start)
        return place_check(a, ocr, 2000, k, k + 1)

    def holds(found: tuple, spot: tuple[int, int, int, int], row: bool) -> bool:
        box, _, (top, bottom) = found
        x0, y0, x1, y1 = spot
        if box and not row:
            return box[0] <= x0 and x1 <= box[0] + box[2] and box[1] <= y0 and y1 <= box[1] + box[3]
        if box:
            top, bottom = box[1] - 30, box[1] + box[3] + 30
        return top - 45 <= y0 and y1 <= bottom + 45

    stamp = "Заключение: без особенностей.\nВрач Петрова А.А. [печать: {} АННА ВРАЧ]\nДата 12.03.2024"
    note = "ФИО Иванов И.И. Дата рождения: 01.01.1970 [от руки: вр. {}]\nВид оплаты ОМС"
    row = "| Тироксин свободный | 12, 34 | пмоль/л | (9 - 19) |\n[штрихкод]"
    dated = "Протокол исследования\nПротокол исследования\nот 05.06.2024\n\nПациент Иванов Возраст 54"
    for name, a_text, b_text, seen, fragment, spot, is_row in [
        ("stamp over its owner's printed name", stamp.format("ПЕТРОВА"), stamp.format("ПЕ[?Т]РОВА"),
         [("Заключение: без особенностей.", 100, 885, 600), ("Врач Петрова А.А.", 100, 985, 300), ("Дата 12.03.2024", 100, 1085, 300)],
         "[печать", (500, 880, 800, 1040), False),
        ("handwritten note at the edge of a line", note.format("Петрова"), note.format("[?Петрова]"),
         [("ФИО Иванов И.И.", 100, 285, 600), ("Дата рождения: 01.01.1970", 800, 285, 500), ("Вид оплаты ОМС", 500, 345, 400)],
         "[от руки", (1400, 290, 1700, 330), False),
        ("row and a short number inside a code", row, row,
         [("12,34 пмоль/л", 900, 585, 300), ("0A12BC", 100, 1485, 300)],
         "19", (1250, 590, 1300, 610), True),
        ("date on a line without words", dated, dated,
         [("Протокол исследования", 100, 85, 400), ("Пациент Иванов Возраст 54", 100, 1465, 600)],
         "05.06.2024", (150, 1350, 400, 1370), True),
    ]:
        found = where(a_text, b_text, seen, fragment)
        if not holds(found, spot, is_row):
            errors.append(f"crop of «{name}» misses the spot {spot}: box {found[0]}, range {tuple(round(v) for v in found[2])}")
    return errors


def check_signs() -> list[str]:
    """A sign that differs between agreed words is a spot of its own for the arbiter; next to a
    misread word it joins that spot instead of adding one. Choosing B there keeps A's words."""
    errors = []
    for a_text, b_text, kinds, final in [
        ("Анамнез: гипертония", "АНАМНЕЗ гипертония", ["punct"], "Анамнез гипертония"),
        ("Ось 30°", "Ось -30°", ["punct"], "Ось -30°"),
        ("Анамнез: гипертония", "Анамнез гипортония", ["text"], "Анамнез гипортония"),
    ]:
        a, b = Text(a_text), Text(b_text)
        units, _, _ = diff_units(a, b)
        edits = [edit_for_unit(a, b, u) for u in units]
        got, _, problems = apply_edits(a_text, [Edit(*e["a_span"], e["b_text"], "resolved", "d") for e in edits], 1000, 1000)
        if [u.kind for u in units] != kinds or problems or got != final:
            errors.append(f"«{a_text}» against «{b_text}»: {[u.kind for u in units]} giving {got!r} with B, expected {kinds} giving {final!r}")
    return errors


def main() -> int:
    errors: list[str] = (check_corrections() + check_marks() + check_vision_forms() + check_zones()
                         + check_markdown() + check_places() + check_signs() + check_rows())
    for case in CASES:
        errors += run_case(*case)
    for case in LAYOUT_CASES:
        errors += run_case(*case, exact=False)
    for name, a_text, b_text in SAME:
        units, _, _ = diff_units(Text(a_text), Text(b_text))
        if units:
            errors.append(f"{name}: expected no discrepancy, got {len(units)}")
    pages = 0
    manifest = load_manifest() if ROOT else {"documents": {}}
    for doc, meta in manifest["documents"].items():
        for entry in meta["pages"]:
            paths = page_paths(doc, entry["page"])
            if paths["read_a"].exists() and paths["read_b"].exists():
                pages += 1
                errors += run_case(f"{doc} p{entry['page']}", load_text(paths["read_a"]), load_text(paths["read_b"]), exact=False)
    for error in errors:
        print("FAIL", error)
    print(f"{len(CASES) + len(LAYOUT_CASES) + len(SAME) + 8} built-in cases, {pages} real pages: {'all passed' if not errors else f'{len(errors)} failures'}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
