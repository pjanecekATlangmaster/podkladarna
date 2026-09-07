"""Doměření výšky srázu z výškového modelu.

Karttapullautin o výšce srázu nic neřekne. Rozhoduje se podle ní (`temp = h0 - ht`
v jeho `cliffs.rs`), ale do DXF zapíše jen geometrii, takže se z jeho výstupu
nedá poznat, jestli je nález 1,2 m stupínek nebo 15 m stěna. Tenhle modul výšku
doměří zpětně z DEM.

Bere jen lomenou čáru a funkci na vzorkování výšky, o zdroj geometrie se nestará.
Až KP nahradí vlastní detekce, měření i prahování zůstanou beze změny.

Pozor na svah: rozdíl výšek napříč čárou sám o sobě nic neznamená, protože i
rovnoměrný svah ho dá. Proto se vzorkuje ve dvou vzdálenostech a od bližšího
rozdílu se odečte trend spočítaný ze vzdálenějšího – zbyde jen ten schod.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Bližší sonda musí být za rozmazáním svislé stěny v metrovém rastru.
PROBE_NEAR_M = 2.5
# Vzdálenější sonda slouží jen k odhadu okolního svahu.
PROBE_FAR_M = 7.5
# Krok stanovišť podél čáry a strop, ať dlouhá stěna nestojí tisíce vzorků.
STATION_STEP_M = 3.0
MAX_STATIONS = 24

# Pod tímhle poklesem nemá smysl kreslit nic.
MIN_DROP_M = 1.0
# Od tolika metrů jde o výrazný sráz (ISOM 201 nepřekonatelný).
MAJOR_DROP_M = 2.0


@dataclass(frozen=True)
class DropMeasurement:
    """Výsledek měření jedné čáry. `drop_m is None` = nešlo změřit."""

    drop_m: float | None
    stations: int

    @property
    def measured(self) -> bool:
        return self.drop_m is not None


def measure_drop(
    points: list[tuple[float, float]],
    elev_at,
    *,
    probe_near_m: float = PROBE_NEAR_M,
    probe_far_m: float = PROBE_FAR_M,
    station_step_m: float = STATION_STEP_M,
    max_stations: int = MAX_STATIONS,
) -> DropMeasurement:
    """Medián výšky schodu napříč čárou, očištěný o okolní svah."""
    if elev_at is None or len(points) < 2:
        return DropMeasurement(None, 0)

    span = probe_far_m - probe_near_m
    if span <= 0:
        return DropMeasurement(None, 0)

    drops: list[float] = []
    for x, y, nx, ny in _stations(points, station_step_m, max_stations):
        near_lo = elev_at(x - nx * probe_near_m, y - ny * probe_near_m)
        near_hi = elev_at(x + nx * probe_near_m, y + ny * probe_near_m)
        far_lo = elev_at(x - nx * probe_far_m, y - ny * probe_far_m)
        far_hi = elev_at(x + nx * probe_far_m, y + ny * probe_far_m)
        if None in (near_lo, near_hi, far_lo, far_hi):
            continue
        # Spád okolí odhadnout na každé straně zvlášť – dvojice, která schod
        # přeskakuje, by do odhadu zatáhla právě ten schod, co chceme změřit.
        grade = ((near_lo - far_lo) + (far_hi - near_hi)) / (2 * span)
        expected = grade * 2 * probe_near_m
        drops.append(abs((near_hi - near_lo) - expected))

    if not drops:
        return DropMeasurement(None, 0)
    drops.sort()
    return DropMeasurement(drops[len(drops) // 2], len(drops))


def _stations(
    points: list[tuple[float, float]], step_m: float, max_stations: int
):
    """Body podél čáry i s jednotkovou normálou (kolmo na místní směr)."""
    total = sum(
        math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1])
        for i in range(1, len(points))
    )
    if total <= 0:
        return
    count = max(1, min(max_stations, int(total / step_m)))
    # Krajní body vynechat, tam normála utíká mimo útvar.
    for k in range(count):
        target = total * (k + 0.5) / count
        walked = 0.0
        for i in range(1, len(points)):
            ax, ay = points[i - 1]
            bx, by = points[i]
            seg = math.hypot(bx - ax, by - ay)
            if seg <= 0:
                continue
            if walked + seg < target:
                walked += seg
                continue
            t = (target - walked) / seg
            yield (
                ax + t * (bx - ax),
                ay + t * (by - ay),
                -(by - ay) / seg,
                (bx - ax) / seg,
            )
            break


def drop_is_mappable(
    measurement: DropMeasurement, *, min_drop_m: float = MIN_DROP_M
) -> bool:
    """Nezměřené nechat projít – bez DEM se nemá podle čeho rozhodovat."""
    if not measurement.measured:
        return True
    return measurement.drop_m >= min_drop_m


def filter_by_drop(
    lines: list[list[tuple[float, float]]],
    elev_at,
    *,
    min_drop_m: float = MIN_DROP_M,
) -> tuple[list[list[tuple[float, float]]], dict[str, int]]:
    """Vyhodí čáry s příliš malým schodem. Vrací i souhrn pro log."""
    if elev_at is None:
        return lines, {"celkem": len(lines), "nezmereno": len(lines)}

    kept: list[list[tuple[float, float]]] = []
    stats = {"celkem": len(lines), "nezmereno": 0, "zahozeno": 0, "vyrazne": 0}
    for pts in lines:
        m = measure_drop(pts, elev_at)
        if not m.measured:
            stats["nezmereno"] += 1
            kept.append(pts)
            continue
        if m.drop_m < min_drop_m:
            stats["zahozeno"] += 1
            continue
        if m.drop_m >= MAJOR_DROP_M:
            stats["vyrazne"] += 1
        kept.append(pts)
    return kept, stats
