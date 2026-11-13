"""Bounded, dependency-free WIP imports. XLSX files are read without extraction."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path, PurePosixPath
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_EXPANDED_BYTES = 80 * 1024 * 1024
MAX_ROWS = 100000
MAX_COLUMNS = 256
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
REQUIRED = {'country', 'business_unit', 'invoice_status', 'start_date',
            'audit_type', 'contract_number', 'amount'}
ALIASES = {'flex_contract_number': 'contract_number', 'contract_no': 'contract_number',
           'contracting_country': 'country', 'man_days': 'man_days', 'mandays': 'man_days',
           'po_no': 'po_number', 'purchase_order_number': 'po_number',
           'businessunit': 'business_unit', 'invoice_amount': 'amount'}


def _header(value):
    name = re.sub(r'[^a-z0-9]+', '_', str(value).strip().lower()).strip('_')
    return ALIASES.get(name, name)


def _date(value, epoch1904=False):
    value = str(value).strip()
    if re.fullmatch(r'\d{4,5}(?:\.\d+)?', value):
        try:
            base = datetime(1904, 1, 1) if epoch1904 else datetime(1899, 12, 30)
            return (base + timedelta(days=float(value))).date().isoformat()
        except (OverflowError, ValueError):
            pass
    for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%d/%m/%Y', '%d-%m-%Y', '%d-%b-%Y'):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return value


def _normalize(rows, epoch1904=False):
    iterator = iter(rows)
    header = next(iterator, None)
    if header is None:
        raise ValueError('WIP file is empty')
    first = header
    # Smart Cert exports can contain report titles and filter rows before the
    # actual table. Only accept an unambiguous full set of known column names.
    for _ in range(50):
        if REQUIRED.issubset({_header(value) for value in header}):
            break
        candidate = next(iterator, None)
        if candidate is None:
            header = first
            break
        header = candidate
    else:
        raise ValueError('WIP table header was not found within the first 50 rows')
    names = [_header(v) for v in header]
    populated = [n for n in names if n]
    if len(populated) != len(set(populated)):
        raise ValueError('WIP contains duplicate column names')
    missing = REQUIRED - set(names)
    if missing:
        raise ValueError('WIP missing columns: ' + ', '.join(sorted(missing)))
    result = []
    for row_number, row in enumerate(iterator, 2):
        if row_number > MAX_ROWS + 1:
            raise ValueError('WIP exceeds row limit')
        if not any(str(v).strip() for v in row):
            continue
        if len(row) > MAX_COLUMNS:
            raise ValueError('WIP exceeds column limit')
        record = {name: str(row[i]).strip() if i < len(row) else ''
                  for i, name in enumerate(names) if name}
        for name in ('currency', 'po_number', 'comments'):
            record.setdefault(name, '')
        record['start_date'] = _date(record['start_date'], epoch1904)
        result.append(record)
    return result


def _xml(archive, name):
    data = archive.read(name)
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('XML entities are not allowed in WIP files')
    return ET.fromstring(data)


def _xlsx(path):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(i.file_size for i in entries) > MAX_EXPANDED_BYTES:
            raise ValueError('XLSX archive exceeds expansion limit')
        for info in entries:
            name = info.filename.replace('\\', '/')
            if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:
                raise ValueError('Unsafe XLSX archive path')
        workbook = _xml(archive, 'xl/workbook.xml')
        props = workbook.find('s:workbookPr', NS)
        epoch1904 = props is not None and props.get('date1904') in ('1', 'true')
        sheet = workbook.find('s:sheets/s:sheet', NS)
        if sheet is None:
            raise ValueError('XLSX contains no worksheets')
        rel_id = sheet.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
        relationships = _xml(archive, 'xl/_rels/workbook.xml.rels')
        relation = next((r for r in relationships if r.get('Id') == rel_id), None)
        if relation is None or relation.get('TargetMode') == 'External':
            raise ValueError('Invalid worksheet relationship')
        target = relation.get('Target', '')
        name = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
        if not name.startswith('xl/') or '..' in PurePosixPath(name).parts:
            raise ValueError('Unsafe worksheet relationship')
        shared = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            root = _xml(archive, 'xl/sharedStrings.xml')
            shared = [''.join(el.itertext()) for el in root.findall('s:si', NS)]
        root = _xml(archive, name)
        rows = []
        for row in root.findall('s:sheetData/s:row', NS):
            values = []
            for cell in row.findall('s:c', NS):
                match = re.fullmatch(r'([A-Z]+)[0-9]+', cell.get('r', ''))
                if not match:
                    raise ValueError('Invalid XLSX cell reference')
                col = 0
                for letter in match[1]:
                    col = col * 26 + ord(letter) - 64
                if col > MAX_COLUMNS:
                    raise ValueError('WIP exceeds column limit')
                while len(values) < col:
                    values.append('')
                if cell.find('s:f', NS) is not None:
                    raise ValueError('WIP formulas must be replaced with values before import')
                raw = cell.findtext('s:v', default='', namespaces=NS)
                kind = cell.get('t')
                if kind == 's':
                    try:
                        index = int(raw)
                        if index < 0:
                            raise ValueError()
                        raw = shared[index]
                    except (ValueError, IndexError):
                        raise ValueError('Invalid XLSX shared string') from None
                elif kind == 'inlineStr':
                    raw = ''.join(cell.find('s:is', NS).itertext()) if cell.find('s:is', NS) is not None else ''
                values[col - 1] = raw
            rows.append(values)
            if len(rows) > MAX_ROWS + 1:
                raise ValueError('WIP exceeds row limit')
        return _normalize(rows, epoch1904)


def load_wip(path):
    """Load first XLSX worksheet or UTF-8 CSV; return normalized string records."""
    path = Path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('WIP file exceeds 25 MB limit')
    if path.suffix.lower() == '.csv':
        return _normalize(csv.reader(StringIO(path.read_text(encoding='utf-8-sig'), newline='')))
    if path.suffix.lower() == '.xlsx':
        try:
            return _xlsx(path)
        except (zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
            raise ValueError('Invalid XLSX WIP file') from exc
    raise ValueError('Choose an XLSX or UTF-8 CSV WIP report')
