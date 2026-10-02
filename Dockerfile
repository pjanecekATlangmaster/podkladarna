# syntax=docker/dockerfile:1

FROM condaforge/mambaforge:24.9.2-0

# GDAL 3.9+: C++ CLI (gdal_translate, ogr2ogr, …) žije v libgdal-core;
# balíček gdal = Python bindings + Python utilities. Obojí explicitně —
# bez apt gdal-bin (dvojí GDAL/PROJ by rozbilo conda stack).
# mambaforge: /opt/conda/bin je už v PATH.
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

# Oficiální KP v2.15.1 obsahuje OOB clamp (PR #271).
# https://github.com/karttapullautin/karttapullautin/releases/tag/v2.15.1
ARG KP_VERSION=v2.15.1
ARG KP_DOWNLOAD_URL=https://github.com/karttapullautin/karttapullautin/releases/download/${KP_VERSION}/karttapullautin-x86_64-linux.tar.gz
RUN curl -fsSL -o /tmp/kp.tgz "${KP_DOWNLOAD_URL}" \
    && mkdir -p /tmp/kp \
    && tar xzf /tmp/kp.tgz -C /tmp/kp \
    && KP_BIN="$(find /tmp/kp -type f -name pullauta | head -n1)" \
    && test -n "$KP_BIN" \
    && install -m 755 "$KP_BIN" /usr/local/bin/pullauta \
    && rm -rf /tmp/kp /tmp/kp.tgz \
    && test -x /usr/local/bin/pullauta

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY configs ./configs
COPY app ./app
COPY web ./web

ARG BUILD_DATE=
ENV PODKLADARNA_BUILT_AT=${BUILD_DATE}

ENV PODKLADARNA_DATA=/data
ENV PULLAUTA_BIN=/usr/local/bin/pullauta
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
# pyproj/GDAL: conda proj.db (zajistí ensure_proj_data i za běhu)
ENV PROJ_NETWORK=OFF

EXPOSE 8672

VOLUME ["/data"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8672", "--app-dir", "/app"]
