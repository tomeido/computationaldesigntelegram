FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock ./
RUN python -m pip install -r requirements.lock

COPY pyproject.toml ./
COPY compdesign_bot ./compdesign_bot
RUN python -m pip install --no-deps .

COPY config ./config
RUN mkdir -p /app/data

CMD ["python", "-m", "compdesign_bot", "run"]
