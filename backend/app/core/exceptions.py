from typing import Any


class WorkflowError(Exception):
    """工作流通用异常。"""

    default_code = "INTERNAL_ERROR"
    default_status_code = 500
    default_retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
        public_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code or self.default_code
        self.status_code = status_code or self.default_status_code
        self.retryable = self.default_retryable if retryable is None else retryable
        self.details = details
        self.public_message = public_message or message


class ConfigurationError(WorkflowError):
    """配置异常。"""

    default_code = "INTERNAL_ERROR"
    default_status_code = 500


class ProviderError(WorkflowError):
    """模型或检索提供器异常。"""

    default_code = "PROVIDER_UNAVAILABLE"
    default_status_code = 503
    default_retryable = True


class ModelTimeoutError(ProviderError):
    """模型调用超时。"""

    default_code = "MODEL_TIMEOUT"
    default_status_code = 504


class InputValidationError(WorkflowError):
    """输入校验异常。"""

    default_code = "INVALID_REQUEST"
    default_status_code = 400


class UploadLimitExceededError(InputValidationError):
    """上传限制越界。"""

    default_code = "UPLOAD_LIMIT_EXCEEDED"


class UnsupportedMediaError(InputValidationError):
    """不支持的媒体类型。"""

    default_code = "UNSUPPORTED_MEDIA"


class InvalidAccidentDataError(InputValidationError):
    """事故信息 JSON 非法。"""

    default_code = "INVALID_ACCIDENT_DATA"


class SessionNotFoundError(InputValidationError):
    """会话不存在。"""

    default_code = "SESSION_NOT_FOUND"
    default_status_code = 404


class ArtifactNotFoundError(InputValidationError):
    """产物不存在。"""

    default_code = "ARTIFACT_NOT_FOUND"
    default_status_code = 404


class InvalidSessionStateError(WorkflowError):
    """会话状态不允许当前操作。"""

    default_code = "INVALID_SESSION_STATE"
    default_status_code = 409


class DependencyUnavailableError(WorkflowError):
    """可选依赖不可用。"""

    default_code = "DEPENDENCY_UNAVAILABLE"
    default_status_code = 503


class RequestCancelledError(WorkflowError):
    """客户端连接已断开或请求被主动取消。"""

    default_code = "REQUEST_CANCELLED"
    default_status_code = 409
    default_retryable = True


class AuthenticationError(WorkflowError):
    """认证失败。"""

    default_code = "AUTHENTICATION_FAILED"
    default_status_code = 401
    default_retryable = False


class PermissionDeniedError(WorkflowError):
    """无权限。"""

    default_code = "PERMISSION_DENIED"
    default_status_code = 403
    default_retryable = False
