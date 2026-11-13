import re
import zipfile
from pathlib import Path

def extract_zip(source: Path, destination: Path) -> Path:
    destination = destination.resolve()
    with zipfile.ZipFile(source) as archive:
        if len(archive.infolist()) > 10000:
            raise ValueError('Archive exceeds the 10,000 entry limit')
        names = set()
        if sum(i.file_size for i in archive.infolist()) > 2_000_000_000:
            raise ValueError("Archive exceeds the 2 GB intake limit")
        # Validate the entire archive before writing any member.
        for info in archive.infolist():
            parts = Path(info.filename).parts
            if any(p.rstrip(' .') != p or re.match(r'^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', p, re.I) for p in parts):
                raise ValueError('Unsafe Windows archive filename')
            name = (destination / info.filename).resolve().as_posix().casefold()
            if name in names:
                raise ValueError('Duplicate archive path')
            names.add(name)
            if info.flag_bits & 1:
                raise ValueError('Encrypted archives are unsupported')
            target = (destination / info.filename).resolve()
            if not target.is_relative_to(destination) or ":" in info.filename or "\\" in info.filename:
                raise ValueError(f"Unsafe archive member: {info.filename}")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Archive symlinks are unsupported")
        for info in archive.infolist():
            if info.is_dir():
                continue
            target = destination / info.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, target.open("wb") as dst:
                import shutil
                shutil.copyfileobj(src, dst)
    return destination
