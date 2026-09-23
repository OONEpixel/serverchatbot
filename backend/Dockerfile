FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests
RUN python -m unittest discover -s tests -v

RUN groupadd --gid 10001 bridge \
    && useradd --uid 10001 --gid bridge --no-create-home bridge \
    && mkdir -p /data \
    && chown bridge:bridge /data

USER bridge

CMD ["python", "-m", "app"]
