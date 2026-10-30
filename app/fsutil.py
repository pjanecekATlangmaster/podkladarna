"""Pomocné funkce pro souborový systém."""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    """Zapíše soubor přes dočasné jméno ve stejné složce a ``os.replace``.

    Souběžný čtenář (jiný proces) vidí buď starý, nebo celý nový obsah, nikdy rozepsaný soubor.
    """
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_text(path: str | Path, text: str, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding))
