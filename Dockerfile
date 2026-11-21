# -----------------------------
# Stage 1: Build frontend assets
# -----------------------------
FROM node:26.10.0-alpine AS frontend-builder

COPY ./src/parseus/webapp/ /app/webapp/
WORKDIR /app/webapp

RUN npm install
RUN npm run build

# -----------------------------
# Stage 2: Build Python deps
# -----------------------------
FROM python:3.12.15-slim AS python-builder

WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# build-essential: hdbscan and libsass publish no linux/arm64 wheels, so pip compiles them there.
# Builder stage only; the runtime image copies just the venv.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        git \
    && apt-get clean \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# using requirements to speed building images multiple times
COPY requirements.txt constraints.txt ./
RUN pip config set global.trusted-host "pypi.org files.pythonhosted.org" \
    && pip config set global.no-cache-dir true \
    && pip install --upgrade pip \
    && pip install -c constraints.txt -r requirements.txt

COPY ./src/ ./src/
COPY ./README.md ./
COPY ./pyproject.toml ./

# constraints.txt pins the full resolved set so PEAT's hard pins (requests,
# cryptography, ...) don't send pip into resolution-too-deep backtracking.
RUN pip install -c constraints.txt -e .[all]

COPY --from=frontend-builder /app/webapp/dist ./src/parseus/webapp/dist
COPY --from=frontend-builder /app/webapp/node_modules ./src/parseus/webapp/node_modules

RUN PARSEUS_STATIC_BUILD=True parseus collectstatic --noinput \
    && PARSEUS_STATIC_BUILD=True parseus compress

# -----------------------------
# Stage 3: Final runtime image
# -----------------------------
FROM python:3.12.15-slim

ARG PARSEUS_VERSION=dev

LABEL org.opencontainers.image.title="Parseus" \
      org.opencontainers.image.description="Graphical User Interface for Operational Technology Security Analysis." \
      org.opencontainers.image.version="${PARSEUS_VERSION}"

ENV PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:/home/webapp/.local/bin/:${PATH}"

WORKDIR /app/

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpcap0.8 \
    && rm -rf /var/lib/apt/lists/*

COPY ./src/ ./src/
COPY ./entrypoint.sh /app/entrypoint.sh
COPY --from=python-builder /opt/venv/ /opt/venv/
COPY --from=python-builder /root/.parseus/artifacts/static/ /home/webapp/.parseus/artifacts/static/

RUN useradd -m webapp \
    && chown -R webapp:webapp /app \
    && chown -R webapp:webapp /home/webapp \
    && chmod +x /app/entrypoint.sh

USER webapp

RUN mkdir -p /home/webapp/.parseus/artifacts/database \
    && mkdir -p /home/webapp/.parseus/artifacts/logs \
    && mkdir -p /home/webapp/.parseus/artifacts/media \
    && mkdir -p /home/webapp/.parseus/artifacts/static \
    && mkdir -p /home/webapp/.parseus/artifacts/pcaps

ENTRYPOINT [ "/app/entrypoint.sh" ]
