FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py db.py matcher.py backfill.py contacts.py instantly.py sheets.py schema.sql ./

CMD ["python", "main.py"]
