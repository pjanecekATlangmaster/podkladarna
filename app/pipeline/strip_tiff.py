"""Okno z velkého vzdáleného GeoTIFFu se striptem po řádcích (ne COG) jedním dotazem Range.

ICV (Valencie) dává MDT a MDS 1 m jako listy MTN50 ~420 MB: LZW, strip = 1 řádek, IFD na konci souboru. GDAL přes
/vsicurl čte řádek po řádku (tisíce dotazů, 2 km okna ~4,5 min z cloudu). Řádky leží v souboru za sebou, takže stačí
stáhnout hlavičku, IFD s tabulkami a souvislý úsek bajtů řádků okna a zapsat je na jejich místa do řídkého lokálního
souboru stejné velikosti. Místní GDAL pak čte jen zapsané části (zbytek jsou nuly, ty se nečtou).

Podporuje jen klasický TIFF (ne BigTIFF) s georeferencí ModelPixelScale + ModelTiepoint bez rotace.
"""

from __future__ import annotations

import struct
import time
import urllib.request
from pathlib import Path

HEAD_BYTES = 65536
ROW_MARGIN = 3  # řádky navíc nad a pod oknem (převzorkování, pixel jako bod / plocha)
MAX_GAP_FACTOR = 1.5  # úsek bajtů smí být nejvýš 1,5× součet stripů okna, jinak se stahuje po stripech

_TYPES = {1: ("B", 1), 2: ("c", 1), 3: ("H", 2), 4: ("I", 4), 11: ("f", 4), 12: ("d", 8), 16: ("Q", 8)}
TAG_WIDTH, TAG_HEIGHT, TAG_STRIP_OFFSETS, TAG_ROWS_PER_STRIP, TAG_STRIP_BYTES = 256, 257, 273, 278, 279
TAG_TILE_WIDTH, TAG_PIXEL_SCALE, TAG_TIEPOINT = 322, 33550, 33922


class StripTiffError(RuntimeError):
    pass


def _default_fetch(url: str, start: int, end: int, sink=None) -> tuple[bytes, int]:
    """Bajty ``start``–``end`` (včetně) a celková velikost souboru z Content-Range.

    Se ``sink`` (otevřený soubor nastavený na ``start``) se data po 1 MB zapisují rovnou do něj a vrací se prázdné bajty."""
    from app.pipeline.fetch_openzu import USER_AGENT

    last: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Range": f"bytes={start}-{end}"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                if resp.status != 206:
                    raise StripTiffError(f"server nevrátil část souboru (HTTP {resp.status})")
                total = int((resp.headers.get("Content-Range") or "/0").rsplit("/", 1)[-1] or 0)
                if sink is None:
                    return resp.read(), total
                sink.seek(start)
                while chunk := resp.read(1 << 20):
                    sink.write(chunk)
                return b"", total
        except StripTiffError:
            raise
        except Exception as exc:  # síť, 5xx
            last = exc
            time.sleep(2 * (attempt + 1))
    raise StripTiffError(f"stažení části souboru selhalo: {last}")


class _Remote:
    """Vzdálený soubor: malé části (hlavička, IFD, tabulky) v paměti, všechno stažené i v řídkém souboru ``dest``."""

    def __init__(self, url: str, dest: Path, fetch):
        self.url, self.dest, self.fetch = url, dest, fetch
        data, self.size = fetch(url, 0, HEAD_BYTES - 1)
        if not self.size:
            raise StripTiffError("neznámá velikost souboru")
        self.fh = open(dest, "wb")
        self.fh.truncate(self.size)
        self.chunks: list[tuple[int, bytes]] = []
        self.downloaded = 0
        self._keep(0, data)

    def _keep(self, off: int, data: bytes) -> None:
        self.fh.seek(off)
        self.fh.write(data)
        self.chunks.append((off, data))
        self.downloaded += len(data)

    def read(self, off: int, n: int) -> bytes:
        for start, data in self.chunks:
            if start <= off and off + n <= start + len(data):
                return data[off - start : off - start + n]
        data, _ = self.fetch(self.url, off, min(self.size, off + max(n, 4096)) - 1)
        self._keep(off, data)
        return data[:n]

    def copy(self, off: int, n: int) -> None:
        """Úsek dat rovnou do souboru (bez držení v paměti)."""
        data, _ = self.fetch(self.url, off, off + n - 1, sink=self.fh)
        if data:  # fetch vrátil data místo zápisu do ``sink``
            self.fh.seek(off)
            self.fh.write(data)
        self.downloaded += n

    def close(self) -> None:
        self.fh.close()


def _ifd(r: _Remote) -> tuple[str, dict[int, tuple[int, int, int]]]:
    head = r.read(0, 8)
    bo = {b"II": "<", b"MM": ">"}.get(head[:2])
    if bo is None or struct.unpack(bo + "H", head[2:4])[0] != 42:
        raise StripTiffError("není klasický TIFF")
    off = struct.unpack(bo + "I", head[4:8])[0]
    n = struct.unpack(bo + "H", r.read(off, 2))[0]
    raw = r.read(off + 2, 12 * n + 4)
    tags = {}
    for i in range(n):
        tag, typ, cnt, val = struct.unpack(bo + "HHII", raw[12 * i : 12 * i + 12])
        tags[tag] = (typ, cnt, val)
    return bo, tags


def _values(r: _Remote, bo: str, entry: tuple[int, int, int]) -> list:
    typ, cnt, val = entry
    fmt, size = _TYPES.get(typ, (None, 0))
    if fmt is None:
        raise StripTiffError(f"nepodporovaný typ značky {typ}")
    if size * cnt <= 4:
        raw = struct.pack(bo + "I", val)[: size * cnt]
    else:
        raw = r.read(val, size * cnt)
    return list(struct.unpack(bo + fmt * cnt, raw))


def fetch_window(url: str, window: tuple[float, float, float, float], dest: Path, *, fetch=None, log=None) -> Path | None:
    """Řídká lokální kopie ``url`` s hlavičkou a řádky okna ``window`` (v CRS souboru); None, když okno list mine."""
    r = _Remote(url, dest, fetch or _default_fetch)
    try:
        return _fetch_window(r, url, window, log)
    except BaseException:
        r.close()
        dest.unlink(missing_ok=True)
        raise


def _fetch_window(r: _Remote, url: str, window, log) -> Path | None:
    dest = r.dest
    bo, tags = _ifd(r)
    if TAG_TILE_WIDTH in tags or TAG_STRIP_OFFSETS not in tags:
        raise StripTiffError("soubor není po stripech")
    height = _values(r, bo, tags[TAG_HEIGHT])[0]
    width = _values(r, bo, tags[TAG_WIDTH])[0]
    rows_per = _values(r, bo, tags[TAG_ROWS_PER_STRIP])[0] if TAG_ROWS_PER_STRIP in tags else height
    sx, sy = _values(r, bo, tags[TAG_PIXEL_SCALE])[:2]
    tie = _values(r, bo, tags[TAG_TIEPOINT])
    x0 = tie[3] - tie[0] * sx
    y0 = tie[4] + tie[1] * sy
    xmin, ymin, xmax, ymax = window
    if xmax <= x0 or xmin >= x0 + width * sx or ymin >= y0 or ymax <= y0 - height * sy:
        r.close()
        dest.unlink(missing_ok=True)
        return None
    row0 = max(0, int((y0 - ymax) / sy) - ROW_MARGIN)
    row1 = min(height - 1, int((y0 - ymin) / sy) + ROW_MARGIN)
    offsets = _values(r, bo, tags[TAG_STRIP_OFFSETS])
    counts = _values(r, bo, tags[TAG_STRIP_BYTES])
    s0, s1 = row0 // rows_per, row1 // rows_per
    sel = [(offsets[i], counts[i]) for i in range(s0, s1 + 1) if counts[i]]
    if sel:
        lo = min(o for o, _ in sel)
        hi = max(o + c for o, c in sel)
        if hi - lo <= MAX_GAP_FACTOR * sum(c for _, c in sel):
            r.copy(lo, hi - lo)
        else:
            for o, c in sel:
                r.copy(o, c)
    r.close()
    if log:
        log(f"  {url.rsplit('/', 1)[-1]}: řádky {row0}–{row1}, staženo {r.downloaded / 1e6:.1f} MB")
    return dest
