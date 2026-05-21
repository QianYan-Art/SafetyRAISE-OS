from contextvars import ContextVar, Token

_TRACE_ID_CTX: ContextVar[str] = ContextVar("trace_id", default="-")


def get_trace_id() -> str:
    return _TRACE_ID_CTX.get()


def set_trace_id(trace_id: str) -> Token[str]:
    return _TRACE_ID_CTX.set(trace_id)


def reset_trace_id(token: Token[str]) -> None:
    _TRACE_ID_CTX.reset(token)
