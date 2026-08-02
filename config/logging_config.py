import json
import logging
from datetime import datetime, timezone

# Attributes every LogRecord carries regardless of what was logged — used
# to figure out which attributes on a given record are "extra" fields the
# caller attached via logger.info(msg, extra={...}), so only those get
# flattened into the JSON output.
_BASE_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    """Renders each log record as one JSON line. `extra={...}` fields
    passed to the logging call (e.g. cache_status, latency_ms, batch_size,
    qps) are flattened directly into the object, so log lines stay
    machine-parseable instead of needing regex over a formatted string.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _BASE_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    """Shared by the api and worker processes so both emit the same
    structured JSON format instead of each configuring its own plain-text
    logging independently.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    # Quiet the HTTP request logs from the one-time model download at
    # startup so they don't drown out the fields this phase cares about.
    logging.getLogger("httpx").setLevel(logging.WARNING)
