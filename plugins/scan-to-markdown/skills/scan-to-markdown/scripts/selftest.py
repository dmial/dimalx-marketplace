#!/usr/bin/env python3
"""Self-test of the logic that turns decisions and corrections into the final text.

Invariants:
  * choosing A everywhere reproduces text A character for character;
  * choosing B everywhere reproduces text B: character for character on the built-in cases
    (their agreed parts are identical), and as the same sequence of words, numbers, signs,
    brackets and colons on real pages (agreed parts may differ in spacing there);
  * any mix of choices keeps every table row whole;
  * a reviewer's correction replaces exactly its fragment and moves later marks along;
  * stamps, logos and requisite lines fall into the service zone, test results do not.

Usage: python3 selftest.py   (inside a project it also checks every page that has both readings)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # lets `python3 -I` find common.py
from build import Edit, apply_corrections, apply_edits  # noqa: E402
from common import ROOT, in_ranges, load_manifest, load_text, page_paths, service_ranges, tokenize  # noqa: E402
from compare import Text, diff_units, edit_for_unit  # noqa: E402

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
]
# Cases where line breaks legitimately stay as in A, so B is matched by structure, not character by character.
LAYOUT_CASES = [
    ("signature moved", "12 мес\n\n[подпись]\nврач ( терапевт )", "12 мес\n\nврач [подпись] ( терапевт )"),
]
# Pairs that must produce no discrepancy at all: they differ only in ways the comparison ignores.
SAME = [
    ("homoglyphs and decimal comma", "Т3(общ) = 2,45 нмоль/л", "T3(общ) = 2.45 нмоль/л"),
    ("table spacing and case", "| Показатель | 128 г/л |", "|Показатель|128 Г/Л|"),
]


def squeeze(text: str) -> str:
    """Text with blank lines and repeated spaces collapsed, no spaces around [ ] (they carry no data)."""
    text = re.sub(r"\n\s*\n", "\n\n", text).strip()
    return re.sub(r" *([\[\]]) *", r"\1", re.sub(r" +", " ", text))


STRUCTURE_RE = re.compile(r"[\[\]:]")


def structure(text: str) -> list[str]:
    """Words, numbers and signs together with brackets and colons, in order."""
    marks = [(m.start(), m.group()) for m in STRUCTURE_RE.finditer(text)]
    return [norm for _, norm in sorted(marks + [(t.start, t.norm) for t in tokenize(text)])]


def run_case(name: str, a_text: str, b_text: str, exact: bool = True) -> list[str]:
    a, b = Text(a_text), Text(b_text)
    units, _, _ = diff_units(a, b)
    edits = [edit_for_unit(a, b, u) for u in units]
    errors = []

    keep_a = [Edit(e["a_span"][0], e["a_span"][1], a_text[e["a_span"][0]:e["a_span"][1]], "resolved", f"u{n}") for n, e in enumerate(edits)]
    final_a, _, problems = apply_edits(a_text, keep_a, 1000, 1000)
    if problems or final_a != a_text:
        errors.append(f"{name}: choosing A changed the text: {problems or repr(final_a)}")

    # Any mix of choices must keep table rows whole: each row's number of cells as in A or in B.
    allowed = {line.count("|") for line in (a_text + "\n" + b_text).split("\n") if line.lstrip().startswith("|")}
    for mask in range(min(2 ** len(edits), 256)):
        mixed = [
            Edit(e["a_span"][0], e["a_span"][1], e["b_text"] if mask >> n & 1 else a_text[e["a_span"][0]:e["a_span"][1]], "resolved", f"u{n}")
            for n, e in enumerate(edits)
        ]
        final_mix, _, _ = apply_edits(a_text, mixed, 1000, 1000)
        rows = {line.count("|") for line in final_mix.split("\n") if line.lstrip().startswith("|")}
        if not rows <= allowed:
            errors.append(f"{name}: mixed choice {mask:b} broke a table row: {final_mix!r}")
            break

    take_b = [Edit(e["a_span"][0], e["a_span"][1], e["b_text"], "resolved", f"u{n}") for n, e in enumerate(edits)]
    final_b, _, problems = apply_edits(a_text, take_b, 1000, 1000)
    matches = squeeze(final_b) == squeeze(b_text) if exact else structure(final_b) == structure(b_text)
    if problems or not matches:
        errors.append(f"{name}: choosing B gave {final_b!r}, expected {b_text!r} {problems}")
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


def check_zones() -> list[str]:
    errors = []
    text = ("| Показатель | 4.87 | (3.5 - 5.0) |\n"
            "[печать: ОГРН 1027700000000]\n"
            "Лицензия № Л000-00000-00 от 01.02.2020\n"
            "Сертификат: 1234567890[?8]\n"
            "Дата рождения: 01.01.1970\n"
            "- Диета №5, выданы рекомендации. Подробнее https://example.org")
    zones = service_ranges(text)
    for fragment, zone in [("4.87", "medical"), ("1027700000000", "service"), ("01.02.2020", "service"),
                           ("[?8]", "service"), ("01.01.1970", "medical"), ("5", "medical"),
                           ("https://example.org", "service")]:
        start = text.index(fragment, text.index("Диета №") if fragment == "5" else 0)
        got = "service" if in_ranges(start, start + len(fragment), zones) else "medical"
        if got != zone:
            errors.append(f"zone of «{fragment}» is {got}, expected {zone}")
    return errors


def main() -> int:
    errors: list[str] = check_corrections() + check_zones()
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
    print(f"{len(CASES) + len(LAYOUT_CASES) + len(SAME) + 2} built-in cases, {pages} real pages: {'all passed' if not errors else f'{len(errors)} failures'}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
