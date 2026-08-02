FROM python:3.12-slim

WORKDIR /code

# Installed before the rest of the source is copied so `docker-compose build`
# only re-runs this (slow, torch/sentence-transformers heavy) layer when
# requirements.txt actually changes, not on every code edit.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
