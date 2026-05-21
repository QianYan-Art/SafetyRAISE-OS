import logging

from app.core.request_context import get_trace_id


class _RequestTraceFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id()
        return True


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | trace=%(trace_id)s | %(name)s | %(message)s",
    )
    trace_filter = _RequestTraceFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(trace_filter)
