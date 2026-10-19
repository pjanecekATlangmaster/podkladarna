"""Sdílený zámek těžkého jobu mezi kontejnery (A = stabilní, B = test).

Obě instance běží na stejném HW a jedou z jednoho připojeného adresáře. Zámek je
souborový (``flock`` na Linuxu, ``msvcrt.locking`` na Windows pro vývoj): jádro ho
uvolní i po pádu procesu, takže nemůže uvíznout. Zapíná se jen nastavením
``PODKLADARNA_SHARED_SLOTS``; bez něj se nic nemění.

Druhá instance se o běžícím jobu nic nedozví (jen že je server vytížený) – fronta
každé instance zůstává vlastní, sdílí se jen „kdo teď smí počítat“.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

try:  # POSIX
    import fcntl
except ImportError:  # Windows (jen vývoj)
    fcntl = None  # type: ignore[assignment]
    import msvcrt  # type: ignore[import-not-found]


def _lock_fd(fd: int) -> bool:
    """Zkusí neblokující exkluzivní zámek; True = získán."""
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _unlock_fd(fd: int) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


class SharedSlot:
    """Jeden sdílený slot (soubor ``<adresář>/job.lock``)."""

    def __init__(self, directory: Path, *, name: str = "job", owner: str = "") -> None:
        self.path = Path(directory) / f"{name}.lock"
        self.owner = owner
        self._fd: int | None = None
        self._mutex = threading.Lock()

    @property
    def held(self) -> bool:
        return self._fd is not None

    def _open(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\n")  # msvcrt.locking potřebuje bajt, který se dá zamknout
        return fd

    def try_acquire(self) -> bool:
        with self._mutex:
            if self._fd is not None:
                return True
            fd = self._open()
            if not _lock_fd(fd):
                os.close(fd)
                return False
            self._fd = fd
            return True

    def acquire(
        self,
        *,
        poll_s: float = 2.0,
        timeout_s: float | None = None,
        on_wait: Callable[[], None] | None = None,
    ) -> bool:
        """Počká na slot (polling). False = vypršel ``timeout_s``."""
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        notified = False
        while not self.try_acquire():
            if not notified and on_wait is not None:
                on_wait()
                notified = True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(poll_s)
        return True

    def release(self) -> None:
        with self._mutex:
            if self._fd is None:
                return
            _unlock_fd(self._fd)
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    def held_by_other(self) -> bool:
        """True, když slot drží někdo jiný (jiný proces / kontejner). Nic nezamyká trvale."""
        if self.held:
            return False
        with self._mutex:
            fd = self._open()
            try:
                if _lock_fd(fd):
                    _unlock_fd(fd)
                    return False
                return True
            finally:
                os.close(fd)

    def __enter__(self) -> "SharedSlot":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def shared_slot_from_env(directory: Path | None, owner: str = "") -> SharedSlot | None:
    """Slot podle nastavení; ``None`` = sdílení vypnuté (jedna instance)."""
    if not directory:
        return None
    return SharedSlot(directory, owner=owner)
