"""Include options compatible with Yosys versions that retain quotes after -I."""
import hashlib
import os
from pathlib import Path
import re


def include_option(directory):
    path = Path(directory).resolve()
    if not path.is_dir():
        raise ValueError(f'Include directory does not exist: {path}')
    if re.fullmatch(r'[\w/.-]+', str(path), re.ASCII):
        return '-I' + str(path)
    # Yosys 0.52 does not unquote -I paths. Use stable local aliases so saved
    # scripts remain replayable, including when the clone path contains spaces.
    base = Path('/tmp') / f'cogito-yosys-includes-{os.getuid()}'
    base.mkdir(mode=0o700, exist_ok=True)
    alias = base / hashlib.sha256(str(path).encode()).hexdigest()
    try:
        alias.symlink_to(path, target_is_directory=True)
    except FileExistsError:
        if not alias.is_symlink() or alias.resolve() != path:
            raise ValueError(f'Conflicting Yosys include alias: {alias}')
    return '-I' + str(alias)
