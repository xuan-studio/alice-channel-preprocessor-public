FROM ubuntu:24.04 AS tdlib-build

ARG DEBIAN_FRONTEND=noninteractive
ARG TDLIB_COMMIT=d1085f9cebc5a62379991ae1652673954f229c1f
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git cmake ninja-build g++ make pkg-config \
    libssl-dev zlib1g-dev gperf php-cli \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --filter=blob:none https://github.com/tdlib/td.git /src/td \
    && cd /src/td \
    && git checkout "$TDLIB_COMMIT"
RUN cmake -S /src/td -B /build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=/opt/tdlib \
    -DTD_ENABLE_JNI=OFF \
    && cmake --build /build --target install -j2

FROM ubuntu:24.04
ARG DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    TDLIB_CORE_PATH=/app/vendor \
    TDLIB_JSON_LIBRARY=/opt/tdlib/lib/libtdjson.so
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates python3 python3-venv curl sqlite3 libssl3t64 zlib1g libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv
COPY --from=tdlib-build /opt/tdlib/lib/libtdjson.so* /opt/tdlib/lib/
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY app /app/app
COPY templates /app/templates
COPY static /app/static
COPY vendor /app/vendor
COPY demo /app/demo
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin preprocessor \
    && mkdir -p /app/data /app/runtime/accounts /app/exports /app/backups \
    && chown -R preprocessor:preprocessor /app/data /app/runtime /app/exports /app/backups \
    && chmod 700 /app/data /app/runtime /app/runtime/accounts /app/exports /app/backups
USER preprocessor
EXPOSE 8848
CMD ["python", "-m", "uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8848"]
