# syntax=docker/dockerfile:1
#
# Multi-stage image:
#  1) Build OpenOrienteering Mapper CLI (PR #2523 / mfbehrens/oo-mapper cli)
#     on the same base as runtime (matching glibc)
#  2) Runtime: conda PDAL/GDAL stack + bundled Mapper (no Karttapullautin)
#
# GPL-3.0: shipping Mapper binary — corresponding source is the pinned git
# ref below; this Dockerfile is the build recipe. Offer on request: see LICENSES.md.

# --- Stage 1: Mapper CLI ---------------------------------------------------
FROM condaforge/mambaforge:24.9.2-0 AS mapper-builder

ENV DEBIAN_FRONTEND=noninteractive

# Pin: mfbehrens/oo-mapper branch cli (upstream PR #2523), spike-verified.
ARG MAPPER_REPO=https://github.com/mfbehrens/oo-mapper.git
ARG MAPPER_REF=6dc1fd72ce2815f47646c51102a7e8e4eedb3bb2

# Qt5 + GIS build deps via apt on the mambaforge Ubuntu base (not conda Qt).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    g++ \
    cmake \
    ninja-build \
    doxygen \
    git \
    ca-certificates \
    python3 \
    qtbase5-dev \
    qtbase5-dev-tools \
    qtbase5-private-dev \
    qttools5-dev \
    qttools5-dev-tools \
    qt5-image-formats-plugins \
    libcups2-dev \
    libgdal-dev \
    libpolyclipping-dev \
    libproj-dev \
    libgl-dev \
    libegl-dev \
    libqt5sensors5-dev \
    libqt5serialport5-dev \
    qtpositioning5-dev \
    libqt5sql5-sqlite \
    zlib1g-dev \
    gdal-bin \
    && (apt-get install -y --no-install-recommends libstdc++-14-dev || true) \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git init mapper \
    && cd mapper \
    && git remote add origin "${MAPPER_REPO}" \
    && git fetch --depth 1 origin "${MAPPER_REF}" \
    && git checkout --force FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${MAPPER_REF}"

WORKDIR /src/mapper/build
RUN cmake .. -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_C_COMPILER=gcc \
    -DCMAKE_CXX_COMPILER=g++ \
    -DCMAKE_INSTALL_PREFIX=/opt/mapper \
    -DMapper_USE_GDAL=ON \
    -DMapper_WITH_COVE=ON \
    -DMapper_BUILD_PACKAGE=OFF \
    && cmake --build . --parallel "$(nproc)" \
    && cmake --install . \
    && test -x /opt/mapper/bin/Mapper

# Bundle ELF + transitive .so + Qt plugins so runtime need not apt-install
# system GDAL/Qt into the conda PATH (avoids dual PROJ/GDAL).
RUN mkdir -p /opt/mapper/lib /opt/mapper/plugins \
    && cp -a /usr/lib/x86_64-linux-gnu/qt5/plugins/platforms /opt/mapper/plugins/ \
    && cp -a /usr/lib/x86_64-linux-gnu/qt5/plugins/imageformats /opt/mapper/plugins/ \
    && python3 <<'PY'
import shutil
import subprocess
from pathlib import Path

SKIP_NAMES = {
    "libc.so.6",
    "libm.so.6",
    "libdl.so.2",
    "libpthread.so.0",
    "librt.so.1",
    "libresolv.so.2",
    "libutil.so.1",
}

root = Path("/opt/mapper")
libdir = root / "lib"
libdir.mkdir(parents=True, exist_ok=True)
queue = [root / "bin" / "Mapper"]
queue.extend(p for p in root.glob("plugins/**/*") if p.suffix.startswith(".so") or ".so." in p.name)
seen: set[Path] = set()
while queue:
    path = queue.pop()
    if path in seen or not path.is_file():
        continue
    seen.add(path)
    try:
        out = subprocess.check_output(["ldd", str(path)], text=True, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        continue
    for line in out.splitlines():
        if "=>" not in line:
            continue
        parts = line.split()
        if len(parts) < 3 or not parts[2].startswith("/"):
            continue
        src = Path(parts[2])
        if not src.is_file() or src.name in SKIP_NAMES or "ld-linux" in src.name:
            continue
        dst = libdir / src.name
        if not dst.exists():
            shutil.copy2(src, dst)
            queue.append(dst)
print(f"bundled {len(list(libdir.iterdir()))} libs")
PY

# Wrapper: isolate Mapper from conda LD_LIBRARY_PATH / PROJ.
RUN mv /opt/mapper/bin/Mapper /opt/mapper/bin/Mapper.real \
    && printf '%s\n' \
    '#!/bin/sh' \
    'export LD_LIBRARY_PATH="/opt/mapper/lib"' \
    'export QT_PLUGIN_PATH="/opt/mapper/plugins"' \
    'export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"' \
    'unset PROJ_LIB PROJ_DATA || true' \
    'exec /opt/mapper/bin/Mapper.real "$@"' \
    > /opt/mapper/bin/Mapper \
    && chmod 755 /opt/mapper/bin/Mapper /opt/mapper/bin/Mapper.real \
    && /opt/mapper/bin/Mapper --cli export --help >/dev/null

# --- Stage 2: Podkladárna runtime (conda) ---------------------------------
FROM condaforge/mambaforge:24.9.2-0

# GDAL 3.9+: C++ CLI (gdal_translate, ogr2ogr, …) žije v libgdal-core;
# balíček gdal = Python bindings + Python utilities. Obojí explicitně —
# bez apt gdal-bin (dvojí GDAL/PROJ by rozbilo conda stack).
# mambaforge: /opt/conda/bin je už v PATH.
# Karttapullautin / pullauta: záměrně NENÍ v image (tip ≥1.26.0).
RUN mamba install -y -c conda-forge \
    pdal \
    python=3.11 \
    gdal \
    libgdal-core \
    proj \
    pyproj \
    pyyaml \
    curl \
    && mamba clean -afy \
    && command -v gdal_translate \
    && command -v ogr2ogr \
    && command -v gdalwarp \
    && command -v gdaldem \
    && command -v gdal_contour \
    && gdal_translate --version

# Mapper CLI (GPL-3.0) – headless georef PNG @ 600 DPI
COPY --from=mapper-builder /opt/mapper /opt/mapper
RUN test -x /opt/mapper/bin/Mapper \
    && /opt/mapper/bin/Mapper --cli export --help >/dev/null

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY configs ./configs
COPY app ./app
COPY web ./web

ARG BUILD_DATE=
ENV PODKLADARNA_BUILT_AT=${BUILD_DATE}

ENV PODKLADARNA_DATA=/data
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
# pyproj/GDAL: conda proj.db (zajistí ensure_proj_data i za běhu)
ENV PROJ_NETWORK=OFF

# Georef ZIP: Mapper CLI (PR #2523). Web náhled zůstává Pillow.
# {dpi} doplní app na 600 (GEOREF_MAPPER_DPI). Lokální Windows tip může
# tyto env nepřepsat / nechat prázdné → Pillow georef fallback.
# OCD12 do ZIPu: convert po .omap (bez flagu by default byl v9).
ENV PODKLADARNA_MAPPER=/opt/mapper/bin/Mapper
ENV PODKLADARNA_MAPPER_EXPORT='"{mapper}" --cli export --full-map -i "{omap}" -o "{png}" --dpi {dpi}'
ENV PODKLADARNA_MAPPER_CONVERT='"{mapper}" --cli convert -i "{omap}" -o "{ocd}" --output-format OCD12'
ENV QT_QPA_PLATFORM=offscreen
ENV PATH="/opt/mapper/bin:${PATH}"

EXPOSE 8672

VOLUME ["/data"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8672", "--app-dir", "/app"]
