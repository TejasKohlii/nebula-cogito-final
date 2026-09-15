"""Automatic decoding of bundled guidance; obfuscation, not access control."""
from pathlib import Path
import zlib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_MAGIC = b'COGITO-GUIDANCE-1\x00'
# Bundled deliberately for unattended local operation. Anyone with the
# application can recover this key and the plaintext.
_KEY = bytes.fromhex('defe69561d9f64889b41b3ff433cd2dc63ddacaa22acec38e94580ea518d27ec')


def asset_name(path):
    path = Path(path)
    return path.stem if path.suffix == '.enc' else path.name


def asset_text(path):
    path = Path(path)
    if path.suffix != '.enc':
        return path.read_text()
    data = path.read_bytes()
    offset = len(_MAGIC)
    if not data.startswith(_MAGIC) or len(data) < offset + 12 + 16:
        raise ValueError(f'Invalid bundled guidance asset: {path.name}')
    try:
        compressed = AESGCM(_KEY).decrypt(data[offset:offset + 12], data[offset + 12:], _MAGIC)
        return zlib.decompress(compressed).decode('utf-8')
    except (InvalidTag, zlib.error, UnicodeError) as exc:
        raise ValueError(f'Bundled guidance is corrupt or incompatible: {path.name}') from exc
