FROM python:3.13-slim AS base

ENV PATH=/opt/venv/bin:$PATH
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

FROM base AS venv
WORKDIR /opt/venv
COPY *.txt .
RUN <<EOF
set -euxo
python -m venv .
. bin/activate
pip install --no-cache-dir --requirement requirements.txt
EOF

FROM base
WORKDIR /opt/venv
COPY --from=venv /opt/venv .
WORKDIR /opt/app
COPY . .

RUN <<EOF
  apt-get update
  apt-get install --yes --no-install-recommends mime-support
  apt-get clean
  rm -rf /var/lib/apt/lists/*
EOF

ENTRYPOINT ["uvicorn"]
CMD ["main:app", "--host", "0.0.0.0", "--port", "3000", "--workers", "4"]
