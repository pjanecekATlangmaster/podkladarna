# syntax=docker/dockerfile:1

FROM condaforge/mambaforge:24.9.2-0

RUN mamba install -y -c conda-forge \
    pdal \
    python=3.11 \
    gdal \
    proj \
    pyproj \
    pyyaml \
    curl \
    && mamba clean -afy

ARG KP_VERSION=v2.12.1
RUN curl -fsSL -o /tmp/kp.tgz \
    "https://github.com/karttapullautin/karttapullautin/releases/download/${KP_VERSION}/karttapullautin-x86_64-linux.tar.gz" \
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

ENV PODKLADARNA_DATA=/data
ENV PULLAUTA_BIN=/usr/local/bin/pullauta
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
# pyproj/GDAL: conda proj.db (zajistí ensure_proj_data i za běhu)
ENV PROJ_NETWORK=OFF

EXPOSE 8672

VOLUME ["/data"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8672", "--app-dir", "/app"]
