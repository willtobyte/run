FROM python:3.13-slim AS base

FROM base AS venv
WORKDIR /opt/venv
COPY *.txt .
RUN <<EOF
#!/usr/bin/env bash
set -euo pipefail

python -m venv .
. bin/activate
pip install --no-cache-dir --requirement requirements.txt
EOF

FROM base
WORKDIR /opt/app
COPY --from=venv /opt/venv /opt/venv
COPY . .

ENV PATH="/opt/venv/bin:$PATH"
ENV ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN <<EOF
#!/usr/bin/env bash
set -euo pipefail

apt update
apt install p7zip-full --yes
EOF

ENTRYPOINT ["uvicorn"]
CMD ["main:app", "--host", "0.0.0.0", "--port", "3000", "--workers", "1"]
