"""Shared paths and artifact helpers for the Cogito lifecycle."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    with Path(path).open("x") as out:
        json.dump(value, out, indent=2)
        out.write("\n")
