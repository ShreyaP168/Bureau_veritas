"""Content hashing: SHA-256 for exact duplicate detection, a perceptual
average-hash for near-duplicate page detection (rescans, re-faxes)."""

from __future__ import annotations

import hashlib

from PIL import Image

_PHASH_SIZE = 8  # 8x8 -> 64-bit hash


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def perceptual_hash(image: Image.Image) -> str:
    """Average hash (aHash): robust to scan re-compression/minor noise,
    not to rotation or major layout change — sufficient for "is this the
    same scanned page again" duplicate detection, not for classification."""

    small = image.convert("L").resize((_PHASH_SIZE, _PHASH_SIZE), Image.LANCZOS)
    pixels = list(small.tobytes())  # 8-bit grayscale bytes == per-pixel ints
    avg = sum(pixels) / len(pixels)
    bits = "".join("1" if p >= avg else "0" for p in pixels)
    return f"{int(bits, 2):0{_PHASH_SIZE * _PHASH_SIZE // 4}x}"


def hamming_distance(hash_a: str, hash_b: str) -> int:
    int_a, int_b = int(hash_a, 16), int(hash_b, 16)
    return (int_a ^ int_b).bit_count()
