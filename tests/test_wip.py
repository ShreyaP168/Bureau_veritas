import csv
import zipfile
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from veritas.wip import load_wip


HEADERS = ['Country', 'Business Unit', 'Invoice Status', 'Start Date',
           'Audit Type', 'Flex Contract Number', 'Amount']
ROW = ['Dubai', 'Dubai CER', 'Confirmed', '2026-11-01', 'Special Audit', '000123', '1,250.00']


@pytest.fixture
def tmp_path():
    # Default mkdir permissions keep fixtures readable in Windows managed workspaces.
    path = Path('runtime') / ('wip-test-' + uuid.uuid4().hex)
    path.mkdir(parents=True)
    return path


def test_csv_normalizes_and_preserves_identifiers(tmp_path):
    path = tmp_path / 'wip.csv'
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        csv.writer(stream).writerows([HEADERS, ROW])
    record, = load_wip(path)
    assert record['contract_number'] == '000123'
    assert record['amount'] == '1,250.00'
    assert record['start_date'] == '2026-11-01'
    assert record['po_number'] == ''


def _workbook(path, formula=False):
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="WIP" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        rows = []
        for num, values in enumerate([HEADERS, ROW], 1):
            cells = []
            for index, value in enumerate(values):
                ref = chr(65 + index) + str(num)
                if num == 2 and index == 3:
                    cells.append(f'<c r="{ref}"><v>46327</v></c>')
                elif formula and num == 2 and index == 6:
                    cells.append(f'<c r="{ref}"><f>1+1</f><v>2</v></c>')
                else:
                    cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
            rows.append(f'<row r="{num}">{"".join(cells)}</row>')
        archive.writestr('xl/worksheets/sheet1.xml', '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + ''.join(rows) + '</sheetData></worksheet>')


def test_xlsx_reads_inline_strings_and_excel_dates(tmp_path):
    path = tmp_path / 'wip.xlsx'
    _workbook(path)
    record, = load_wip(path)
    assert record['contract_number'] == '000123'
    assert record['start_date'] == '2026-11-01'


def test_formula_is_rejected(tmp_path):
    path = tmp_path / 'wip.xlsx'
    _workbook(path, formula=True)
    with pytest.raises(ValueError, match='formulas'):
        load_wip(path)


def test_missing_and_duplicate_headers(tmp_path):
    path = tmp_path / 'wip.csv'
    path.write_text('Country,Country\nDubai,Dubai', encoding='utf-8')
    with pytest.raises(ValueError, match='duplicate'):
        load_wip(path)
    path.write_text('Country\nDubai', encoding='utf-8')
    with pytest.raises(ValueError, match='missing'):
        load_wip(path)


def test_unsafe_archive_rejected(tmp_path):
    path = tmp_path / 'bad.xlsx'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('../outside', 'bad')
    with pytest.raises(ValueError, match='Unsafe'):
        load_wip(path)


def test_report_preamble_and_contracting_country_alias(tmp_path):
    path = tmp_path / 'export.csv'
    with path.open('w', encoding='utf-8', newline='') as stream:
        csv.writer(stream).writerows([
            ['WIP not fully invoiced with filters'], ['Exported 02 April 2026'], [],
            ['Contracting Country', *HEADERS[1:], 'Service Resource', 'Man Days'],
            ['AE', *ROW[1:], 'Auditor', '5'],
        ])
    record, = load_wip(path)
    assert record['country'] == 'AE'
    assert record['service_resource'] == 'Auditor'
