from app.core.exceptions import RequestCancelledError, SessionVersionConflictError


def test_session_conflict_does_not_change_cancellation_contract():
    cancelled = RequestCancelledError("已取消")
    conflict = SessionVersionConflictError("版本冲突")
    assert cancelled.code == "REQUEST_CANCELLED"
    assert cancelled.status_code == 409
    assert cancelled.retryable is True
    assert conflict.code == "SESSION_VERSION_CONFLICT"
    assert conflict.status_code == 409
    assert conflict.retryable is True
