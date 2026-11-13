"""Atomic local artifact publication."""
import json
import time
from uuid import uuid4


def read_json(path):
    """Allow brief Windows sharing conflicts during atomic file replacement."""
    for attempt in range(6):
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.01 * (attempt + 1))


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f'.{path.stem}.{uuid4().hex}.tmp')
    try:
        temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
        for attempt in range(6):
            try:
                temp.replace(path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.01 * (attempt + 1))
    finally:
        temp.unlink(missing_ok=True)


def save_workbook(workbook, path):
    temp = path.with_name(f'.{path.stem}.{uuid4().hex}.xlsx')
    try:
        workbook.save(temp)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
