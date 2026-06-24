FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

RUN apt-get update \
 && apt-get install -y --no-install-recommends iputils-ping ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /srv/app

ENV PYTHONPATH=/srv

EXPOSE 8000

CMD ["gunicorn", "-w", "2", "--max-requests", "1000", "--max-requests-jitter", "200", "-b", "0.0.0.0:8000", "app.main:create_app()"]
