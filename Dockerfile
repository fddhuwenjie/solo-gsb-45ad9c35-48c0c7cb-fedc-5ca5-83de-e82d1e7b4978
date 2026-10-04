FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/data/oral_history.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py evaluator.py remediation.py credentials.py placements.py ./
COPY templates ./templates
COPY static ./static

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "app:app"]
