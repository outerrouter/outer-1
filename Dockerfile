FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DATA_DIR=/srv/data
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY web ./web
COPY config ./config
RUN useradd -m app && mkdir -p /srv/data && chown -R app /srv/data
USER app
EXPOSE 8000
# ONE worker on purpose: the Telegram reader, AI budget and SQLite live inside this single process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
