"""Extract selected Korean tax form fields from one filing at a time."""
import argparse
from datetime import date
from itertools import groupby
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unicodedata

import numpy as np
import pdfplumber
from pdfplumber.table import Table, TableFinder
from pdfplumber.utils import cluster_objects

from table_model import TableModel
from scan_ocr import ScanOCR

ROOT = Path(__file__).resolve().parent
CODES = dict.fromkeys(("51", "52", "53", "55", "56", "57", "58"), "근로소득")
CODES.update({"66": "연금소득", "60": "기타소득", "61": "기타소득", "67": "기타소득"})
ID = r"(?<!\d)(?:\d{3}\s*-\s*\d{2}\s*-\s*\d{5}|\d{6}\s*-\s*[\d*]{7})(?!\d)"
FINANCIAL = {
    "재무상태표": ("자산총계", "부채총계", "자본총계"),
    "손익계산서": ("매출액", "당기순이익", "당기순손실", "당기순손익"),
}


def key(value):
    value = re.sub(r"[\u2460-\u24ff\u2776-\u2793\ue000-\uf8ff]", "", value or "")
    return re.sub(r"[^가-힣a-zA-Z0-9]", "", value)


def amount(value):
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"[\s*]", "", value).replace("−", "-")
    value = value.removesuffix("원")
    if re.fullmatch(r"\([0-9,]+\)", value):
        value = "-" + value[1:-1]
    if not re.fullmatch(r"-?(?:\d+|\d{1,3}(?:,\d{3})+)", value):
        return None
    number = int(value.replace(",", ""))
    return number if abs(number) < 10**15 else None


def payer(value):
    value = re.sub(ID, "", value or "")
    value = re.sub(r"(?<!\d)(?:\d{10}|\d{13})(?!\d)", "", value)
    value = re.split(r"\s+/\s+|\n(?=\s*[\d*])", value, maxsplit=1)[0]
    return " ".join(value.split()).strip(" /")


def raster_edges(image, width, height):
    dark = np.asarray(image.convert("L")) < 190
    edges = []
    for matrix, orientation in ((dark, "h"), (dark.T, "v")):
        minimum = max(25, int(matrix.shape[1] * 0.035))
        padded = np.pad(matrix, ((0, 0), (1, 1)), constant_values=False)
        rows, columns = np.nonzero(padded[:, 1:] != padded[:, :-1])
        for row, pairs in groupby(zip(rows, columns), key=lambda pair: pair[0]):
            limits = [column for _, column in pairs]
            for start, end in zip(limits[::2], limits[1::2]):
                if end - start < minimum:
                    continue
                if orientation == "h":
                    x0, x1 = start * width / image.width, end * width / image.width
                    top = bottom = row * height / image.height
                else:
                    x0 = x1 = row * width / image.width
                    top, bottom = start * height / image.height, end * height / image.height
                edges.append(dict(x0=x0, x1=x1, top=top, bottom=bottom,
                                  width=x1-x0, height=bottom-top, orientation=orientation))
    return edges


def grid_edges(edges):
    # Some printed forms omit the two outside vertical borders.
    horizontal = [edge for edge in edges if edge["orientation"] == "h" and edge["width"] > 100]
    for side in ("x0", "x1"):
        for aligned in cluster_objects(horizontal, side, 2):
            if len(aligned) < 3:
                continue
            x = sum(edge[side] for edge in aligned) / len(aligned)
            top = min(edge["top"] for edge in aligned)
            bottom = max(edge["top"] for edge in aligned)
            edges.append(dict(x0=x, x1=x, top=top, bottom=bottom,
                              width=0, height=bottom-top, orientation="v"))
    return edges


def intersects(a, b):
    width = max(0, min(a[2], b[2])-max(a[0], b[0]))
    height = max(0, min(a[3], b[3])-max(a[1], b[1]))
    return width * height > 0.30 * min((a[2]-a[0])*(a[3]-a[1]), (b[2]-b[0])*(b[3]-b[1]))


class Page:
    def __init__(self, pdf_page, ocr, model):
        self.words = pdf_page.extract_words(return_chars=True)
        self.ocr = not any(re.search("[가-힣]", word["text"]) for word in self.words)
        self.edges = list(pdf_page.edges)
        image = None
        if self.ocr:
            image = pdf_page.to_image(resolution=225).original
            self.words = ocr.words(image, pdf_page.width, pdf_page.height)
            self.edges = raster_edges(image, pdf_page.width, pdf_page.height)
        else:
            # Individual glyph boxes handle neighboring columns with almost no gap.
            self.words = [char for word in self.words for char in word["chars"]]
        self.lines = cluster_objects(self.words, lambda word: (word["top"] + word["bottom"]) / 2, 3)
        self.line_text = [self.text(line) for line in self.lines]
        self.tables = TableFinder(SimpleNamespace(edges=grid_edges(self.edges)),
                                  {"edge_min_length": 5}).tables
        if model is not None:
            if image is None:
                image = pdf_page.to_image(resolution=155).original
            for xs, ys in model.layouts(image, pdf_page.width, pdf_page.height):
                if any(intersects(table.bbox, (xs[0], ys[0], xs[-1], ys[-1]))
                       for table in self.tables):
                    continue
                cells = [(left, top, right, bottom) for top, bottom in zip(ys, ys[1:])
                         for left, right in zip(xs, xs[1:])]
                self.tables.append(Table(None, cells))


    def text(self, words):
        parts = []
        for word in sorted(words, key=lambda word: word["x0"]):
            if parts and (self.ocr or word["x0"] - previous["x1"] > 1.6):
                parts.append(" ")
            parts.append(word["text"])
            previous = word
        return "".join(parts)

    def cell(self, box, upper=None):
        if box is None:
            return ""
        x0, top, x1, bottom = box
        if upper is not None:
            top, bottom = max(top, upper[0]), min(bottom, upper[1])
        result = []
        for line in self.lines:
            inside = [word for word in line if x0 <= (word["x0"] + word["x1"]) / 2 < x1
                      and top <= (word["top"] + word["bottom"]) / 2 < bottom]
            if inside:
                result.append(self.text(inside))
        return "\n".join(result)

    def rows(self, table):
        return [(row, [self.cell(box) for box in row.cells]) for row in table.rows]


def heading(page, pattern):
    return any(re.fullmatch(pattern, key(line)) for line in page.line_text[:24])


def tax(page):
    for table in page.tables:
        tax_header = None
        tax_group = None
        for row, values in page.rows(table):
            cells = [(box, key(value), value) for box, value in zip(row.cells, values)
                     if box is not None]
            for box, label, _ in cells:
                if label == "종합소득세":
                    tax_header = box
                if label == "결정세액" and tax_header and box[2] <= tax_header[0]:
                    tax_group = box
            for box, _, raw_label in cells:
                label_without_formula = re.sub(r"[（(][^）)]*[）)]", "", raw_label)
                markers = re.findall(r"[①-⑳㉑-㉟㊱-㊿]", label_without_formula)
                if markers and any(mark != "㉘" for mark in markers):
                    continue
                label = key(label_without_formula)
                subtotal = (
                    tax_group is not None and box != tax_group
                    and box[0] >= tax_group[2] - 1 and box[1] >= tax_group[1] - 1
                    and box[3] <= tax_group[3] + 1 and re.fullmatch(r"(?:28)?합계(?:28)?", label)
                )
                direct = re.fullmatch(r"(?:28)?결정세액합계(?:28)?", label)
                if not (direct or subtotal):
                    continue
                if tax_header:
                    candidates = [amount(raw) for other, _, raw in cells if other[0] >= box[2] - 1
                                  and other[0] >= tax_header[0] - 1
                                  and other[2] <= tax_header[2] + 1 and amount(raw) is not None]
                elif "종합소득세액의계산" in key("".join(page.line_text)):
                    supplied = [raw for other, _, raw in cells if other[0] >= box[2] - 1 and raw.strip()]
                    candidates = [amount(supplied[0])] if len(supplied) == 1 else []
                else:
                    candidates = []
                yield candidates[0] if len(candidates) == 1 else None


def detail(page, name_pattern, money_pattern, mixed=False):
    found = False
    result = []
    incomplete = 0
    for table in page.tables:
        rows = page.rows(table)
        name_box = money_box = code_box = None
        start = None
        for row, values in rows[:9]:
            for box, value in zip(row.cells, values):
                label = key(value)
                if re.search(name_pattern, label):
                    if name_box is None or box[0] <= name_box[0]:
                        name_box = (box[0], box[2])
                if re.match(money_pattern, label):
                    money_box = box
                if mixed and re.match(r"\d*소득구분(?:코드|번호)", label):
                    code_box = box
            if name_box and money_box and (code_box or not mixed):
                start = max(money_box[3], code_box[3] if code_box else 0, row.bbox[3])
                # The payer's registration-number header can extend further down.
                for next_row, next_values in rows:
                    if next_row.bbox[1] >= start and any(
                        "등록번호" in key(value) and not amount(value) for value in next_values
                    ):
                        if next_row.bbox[1] - start < 25:
                            start = next_row.bbox[3]
                break
        if start is None:
            continue
        found = True
        for row, _ in rows:
            box = next((cell for cell in row.cells if cell and
                        abs(cell[0] - money_box[0]) < 3 and abs(cell[2] - money_box[2]) < 3), None)
            if box is None or box[1] < start - 2:
                continue
            top, bottom = box[1], box[3]
            payer_text = page.cell((name_box[0], top, name_box[1], bottom))
            name = payer(payer_text)
            raw = page.cell(box)
            code = page.cell((code_box[0], top, code_box[2], bottom)).strip() if mixed else None
            if key(name) in {"합계", "소계", "계", "총계"} or re.search(r"합계|소계", code or ""):
                continue
            if not (name or raw or code):
                continue
            value = amount(raw)
            if not name or value is None:
                incomplete += 1
                continue
            result.append((name, value, code, registration(payer_text) if mixed else ""))
    return found, result, incomplete


def business(page):
    result = []
    found = False
    for table in page.tables:
        fields = {}
        for row, values in page.rows(table):
            for box, value in zip(row.cells, values):
                label = key(value)
                for field, pattern in {"name": r"4?상호", "gross": r"9?총수입금액",
                                       "net": r"(?:11)?소득금액\d*",
                                       "id": r"5?사업자등록번호"}.items():
                    if re.fullmatch(pattern, label):
                        fields.setdefault(field, box)
        if not {"name", "gross", "net"} <= fields.keys():
            continue
        found = True
        name_box = fields["name"]
        row = next(row for row in table.rows if name_box in row.cells)
        for box in row.cells:
            if box is None or box[0] < name_box[2] - 2:
                continue
            values = {field: page.cell((box[0], r[1], box[2], r[3]))
                      for field, r in fields.items()}
            name = payer(values["name"])
            gross, net = amount(values["gross"]), amount(values["net"])
            if name or values["gross"] or values["net"] or values.get("id"):
                result.append((name, gross, net, registration(values.get("id", ""))))
    return found, result


def registration(text):
    match = re.search(r"(?<!\d)\d{3}\s*-?\s*\d{2}\s*-?\s*\d{5}(?!\d)", text)
    return re.sub(r"\D", "", match[0]) if match else ""


def accounting_period(lines):
    date_pattern = r"(?<!\d)(20\d{2})(?:년|[./-]|\s)+(\d{1,2})(?:월|[./-]|\s)+(\d{1,2})(?!\d)"
    dates = []
    for line in lines:
        if "계정과목" in key(line):
            break
        if "개정" in line:
            continue
        for match in re.finditer(date_pattern, line):
            try:
                dates.append(date(*map(int, match.groups())))
            except ValueError:
                return None
    dates = list(dict.fromkeys(dates))
    if len(dates) == 2 and dates[0] < dates[1]:
        return tuple(dates)
    return None


def financial(page, kind, source):
    full_text = "\n".join(page.line_text)
    header = []
    for line in page.line_text[:16]:
        if "계정과목" in key(line):
            break
        header.append(line)
    ids = set(re.sub(r"\D", "", match[0]) for match in re.finditer(
        r"(?<!\d)\d{3}\s*-?\s*\d{2}\s*-?\s*\d{5}(?!\d)", "\n".join(header)
    ))
    period = accounting_period(header)
    corporate = any(re.search(r"일반법인용|주식회사|유한회사|법인명", key(line)) for line in header)
    unit = re.search(r"단위\s*[:：]?\s*(백만원|천원|원)", full_text)
    scale = {"원": 1, "천원": 1000, "백만원": 1000000}.get(unit[1], 1) if unit else 1
    values = {}
    ambiguous = set()
    for table in page.tables:
        columns = []
        for row, raw in page.rows(table):
            cells = [(box, value) for box, value in zip(row.cells, raw) if box]
            heads = [box for box, value in cells if key(value) == "계정과목"]
            if heads:
                columns = []
                for left, right in zip(heads, [*heads[1:], None]):
                    for box, value in cells:
                        if left[2] <= box[0] < (right[0] if right else table.bbox[2]) and key(value) in {"금액", "당기", "당기금액", "당기말"}:
                            columns.append((left, box))
                            break
                continue
            pairs = []
            for account, money in columns:
                top, bottom = row.bbox[1], row.bbox[3]
                pairs.append((page.cell((account[0], top, account[2], bottom)),
                              page.cell((money[0], top, money[2], bottom)),
                              page.cell((account[2], top, money[0], bottom))))
            if not pairs and kind == "재무상태표":
                for index, (box, label) in enumerate(cells):
                    if key(label) in FINANCIAL[kind]:
                        following = [v for _, v in cells[index+1:] if amount(v) is not None]
                        if len(following) == 1:
                            pairs.append((label, following[0], ""))
            for raw_label, raw_value, account_code in pairs:
                label = re.sub(r"^\s*(?:[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+\.|\d+\.|[가-하]\.|\(\d+\))\s*", "", raw_label)
                label = " ".join(label.split())
                normalized = key(label)
                if kind == "재무상태표":
                    label = next((field for field in FINANCIAL[kind] if normalized.startswith(field)), "")
                if kind == "손익계산서" and normalized == "기타" and re.fullmatch(r"\d{1,4}", account_code):
                    label = f"{label} (코드 {account_code})"
                value = amount(raw_value.replace(":", ""))
                if not label or not re.search("[가-힣]", label) or value is None:
                    continue
                value *= scale
                if normalized == "당기순손실":
                    value = -abs(value)
                if label in values and values[label] != value:
                    ambiguous.add(label)
                values[label] = value
    return (kind, ids, period, corporate, {
        label: value for label, value in values.items()
        if label not in ambiguous and abs(value) < 10**15
    }, source)


def match_statements(statements, identifier, year, personal):
    periods = {}
    for kind, ids, period, corporate, values, source in statements:
        if not identifier or ids != {identifier} or period is None or (personal and corporate):
            continue
        if year is not None and period[1].year != year:
            continue
        periods.setdefault(period, {}).setdefault(kind, []).append((values, source))
    complete = [entries for entries in periods.values() if all(
        any(values for values, _ in entries.get(kind, ()))
        and len({source.rsplit(" ", 1)[0] for _, source in entries[kind]}) == 1
        for kind in FINANCIAL
    )]
    return complete[0] if len(complete) == 1 else {}


def append_statement_rows(output, warnings, matched, category, name, source):
    for kind, fields in FINANCIAL.items():
        entries = matched[kind]
        values = {field for entry, _ in entries for field in entry}
        accounts = fields if kind == "재무상태표" else dict.fromkeys(
            field for entry, _ in entries for field in entry
        )
        for field in accounts:
            candidates = {entry[field] for entry, _ in entries if field in entry}
            if len(candidates) == 1:
                output.append((category, None, name, field, candidates.pop()))
            elif candidates:
                warnings.append(f"{source} — {kind}의 {field} 금액이 여러 개입니다.")
        required = (all(field in values for field in fields) if kind == "재무상태표"
                    else "매출액" in values and bool(set(fields[1:]) & values))
        if not required:
            warnings.append(f"{source} — 연결된 {kind}의 일부 지정 계정을 확인하세요.")


def collect(paths, ocr, model):
    output = []
    businesses = []
    statements = []
    employers = {}
    filing_year = None
    warnings = []
    for file_index, path in enumerate(paths):
        with pdfplumber.open(path) as pdf:
            for number, pdf_page in enumerate(pdf.pages, 1):
                page = Page(pdf_page, ocr, model)
                source = f"{path.name} {number}쪽"
                if page.ocr:
                    warnings.append(f"{source} — 스캔 OCR 결과를 원본과 대조하세요.")
                if file_index == 0 and number == 1:
                    year_match = re.search(r"(20\d{2})년귀속", key("".join(page.line_text[:12])))
                    filing_year = int(year_match[1]) if year_match else None
                    values = list(tax(page))
                    value = values[0] if len(values) == 1 else None
                    if value is None:
                        warnings.append(f"{source} — 종합소득세 결정세액을 확정하지 못했습니다.")
                    else:
                        output.append(("종합소득세", None, None, "결정세액", value))
                        # Integer arithmetic rounds half a won away from zero.
                        confirmed = (abs(value) * 11 + 5) // 10
                        output.append(("종합소득세", None, None, "확정세액",
                                       -confirmed if value < 0 else confirmed))
                definitions = (
                    ("이자소득", r"이자소득명세(?:서)?", r"상호성명", r"\d*이자소득금액", "이자소득금액"),
                    ("배당소득", r"배당소득명세(?:서)?", r"법인명", r"\d*배당액$", "배당액"),
                    ("통합", r"근로소득연금소득기타소득명세(?:서)?",
                     r"상호성명|^\d*지급자(?:\d*등록번호)?$", r"\d*총수입금액", "총수입금액"),
                )
                if file_index == 0:
                    for category, title, name, money, item in definitions:
                        if not heading(page, rf"\d*{title}"):
                            continue
                        found, records, incomplete = detail(page, name, money, category == "통합")
                        if not found:
                            warnings.append(f"{source} — {title.replace('(?:서)?', '서')}의 표 머리글을 찾지 못했습니다.")
                        if incomplete:
                            warnings.append(f"{source} — 지급자 또는 금액이 불명확한 {incomplete}행을 확인하세요.")
                        for name, value, code, identifier in records:
                            group = category
                            if category == "통합":
                                code = unicodedata.normalize("NFKC", code or "").strip()
                                if not re.fullmatch(r"\d{2}", code):
                                    warnings.append(f"{source} — 소득구분번호가 불명확한 지급자 행이 있습니다.")
                                    code = None
                                group = CODES.get(code, "구분 확인 필요")
                                if group == "구분 확인 필요" and code:
                                    warnings.append(f"{source} — 미등록 소득구분번호 {code}를 확인하세요.")
                            output.append((group, code, name, item, value))
                            if group == "근로소득" and identifier:
                                employers.setdefault(identifier, name)
                    if heading(page, r"\d*사업소득명세(?:서)?"):
                        found, records = business(page)
                        businesses.extend((record, source) for record in records)
                        if not found:
                            warnings.append(f"{source} — 사업소득명세서의 표 머리글을 찾지 못했습니다.")
                for kind in FINANCIAL:
                    if any(re.search(rf"(?:표준)?{kind}", key(line)) for line in page.line_text[:6]):
                        statements.append(financial(page, kind, source))
    linked_employer = False
    for identifier, employer in employers.items():
        matched = match_statements(statements, identifier, filing_year, personal=False)
        if matched:
            append_statement_rows(output, warnings, matched, "지급 법인 재무", employer, employer)
            linked_employer = True
        elif statements:
            warnings.append(f"{employer} — 사업자등록번호와 회계기간이 일치하는 재무제표 한 쌍을 확인하지 못했습니다.")
    for (name, gross, net, identifier), source in businesses:
        if not name:
            warnings.append(f"{source} — 사업장 상호가 비어 있습니다.")
        matched = match_statements(statements, identifier, filing_year, personal=True)
        if matched:
            append_statement_rows(output, warnings, matched, "사업소득", name, source)
        else:
            if statements:
                warnings.append(f"{source} — 이 사업장에 연결된 재무제표 한 쌍을 확인하지 못했습니다.")
            for label, value in (("총수입금액", gross), ("소득금액", net)):
                if value is None:
                    warnings.append(f"{source} — 사업소득명세서의 {label}을 확인하세요.")
                else:
                    output.append(("사업소득", None, name, label, value))
    if len(paths) > 1 and not businesses and not linked_employer:
        warnings.append("재무제표를 연결할 사업장 또는 지급 법인을 확인하지 못했습니다.")
    return output, list(dict.fromkeys(warnings))


OUTPUT_COLUMNS = ("항목", "금액(원)", "비고")


def output_row(row):
    category, code, name, label, value = row
    if category == "지급 법인 재무" and name:
        name = re.sub(r"^(?:주식회사|㈜|\(주\))\s*|\s*(?:주식회사|㈜|\(주\))$", "", name).strip() or name
        return label, value, name
    elif category == "사업소득" and name:
        label = f"{name} · {label}"
    elif name:
        label = name
    return label, value, code


def save_output(rows, target):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    if target.suffix.lower() != ".xlsx" or target.exists():
        raise ValueError("기존 파일과 겹치지 않는 .xlsx 저장 경로를 지정하세요.")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "추출 결과"
    sheet.append(OUTPUT_COLUMNS)
    for cell in sheet[1]:
        cell.fill = PatternFill("solid", fgColor="17344C")
        cell.font = Font(bold=True, color="FFFFFF")
    sheet.row_dimensions[1].height = 25
    sheet.freeze_panes = "A2"
    for column, width in zip("ABC", (45, 20, 12)):
        sheet.column_dimensions[column].width = width
    for row in rows:
        label, value, note = output_row(row)
        sheet.append((label, value, note))
        sheet.cell(sheet.max_row, 1).data_type = "s"
        if note is not None:
            sheet.cell(sheet.max_row, 3).data_type = "s"
        sheet.cell(sheet.max_row, 2).number_format = "#,##0;[Red](#,##0);0"
    try:
        with tempfile.TemporaryDirectory(dir=target.resolve().parent) as work:
            temporary = Path(work) / "result.xlsx"
            workbook.save(temporary)
            # An exclusive destination also works on Windows drives without hard links.
            with temporary.open("rb") as source, target.open("xb") as destination:
                try:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        destination.write(chunk)
                except OSError:
                    destination.close()
                    target.unlink()
                    raise
    finally:
        workbook.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("filing", type=Path, help="종합소득세 신고서 PDF")
    parser.add_argument("statements", type=Path, nargs="*", help="같은 신고 건의 재무제표 PDF")
    parser.add_argument("-o", "--output", type=Path, required=True, help="새 XLSX 경로")
    parser.add_argument("--models", type=Path, default=ROOT / "models", help="로컬 모델 상위 폴더")
    parser.add_argument("--grid-only", action="store_true", help="Table Transformer 없이 인쇄된 표 선으로 진단")
    args = parser.parse_args()
    if args.output.suffix.lower() != ".xlsx" or args.output.exists():
        parser.error("기존 파일과 겹치지 않는 .xlsx 저장 경로를 지정하세요.")
    paths = [args.filing, *args.statements]
    if any(path.suffix.lower() != ".pdf" or not path.is_file() for path in paths):
        parser.error("입력 파일은 읽을 수 있는 PDF여야 합니다.")
    try:
        model = None if args.grid_only else TableModel(args.models)
        rows, warnings = collect(paths, ScanOCR(args.models / "ppocr"), model)
        for warning in warnings:
            print(f"확인 필요 — {warning}", file=sys.stderr)
        if not rows:
            raise RuntimeError("엑셀에 저장할 값을 찾지 못했습니다.")
        save_output(rows, args.output)
        print(f"{len(rows)}개 항목 저장 — {args.output}")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"추출 실패 — {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
