FROM python:3.12-slim

WORKDIR /code

# Installed before the rest of the source is copied so `docker-compose build`
# only re-runs this (slow, torch/sentence-transformers heavy) layer when
# requirements.txt actually changes, not on every code edit.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runs as a non-root user matching the host user's UID/GID (1000:1000, the
# default first-user WSL2 UID) so files the process writes into the
# bind-mounted project dir (e.g. __pycache__ on every --reload) land owned
# by the host user, not root.
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --no-create-home appuser \
    && chown -R appuser:appuser /code
USER appuser

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
