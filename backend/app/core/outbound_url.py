from __future__ import annotations

import ipaddress
import json
import socket
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.exceptions import InputValidationError, ProviderError

DEFAULT_MAX_RESPONSE_BYTES = 1 * 1024 * 1024
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata",
    }
)


def create_safe_http_client(
    *,
    timeout: float | httpx.Timeout | None = None,
    trust_env: bool = False,
) -> httpx.Client:
    """创建默认禁用重定向与环境代理注入的 HTTP 客户端。"""
    return httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        trust_env=trust_env,
    )


def validate_user_outbound_url(
    raw_url: str,
    *,
    allow_http: bool = False,
    host_allowlist: Sequence[str] = (),
    resolve_dns: bool = True,
    require_dns_resolution: bool = False,
) -> str:
    """校验用户自定义出站模型 URL，阻断回环/私网/元数据等 SSRF 向量。"""
    normalized = str(raw_url or "").strip()
    if not normalized:
        raise InputValidationError("模型地址不能为空。")
    if len(normalized) > 255:
        raise InputValidationError("模型地址不能超过 255 个字符。")
    if any(ch.isspace() or ord(ch) < 32 for ch in normalized):
        raise InputValidationError("模型地址包含非法空白或控制字符。")

    parsed = urlsplit(normalized)
    scheme = (parsed.scheme or "").lower()
    if parsed.username is not None or parsed.password is not None:
        raise InputValidationError("模型地址不能包含用户名或密码（userinfo）。")
    if parsed.query or parsed.fragment:
        raise InputValidationError("模型地址不能包含查询参数或锚点片段。")

    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        raise InputValidationError("模型地址缺少有效主机名。")

    try:
        port = parsed.port
    except ValueError as exc:
        raise InputValidationError("模型地址端口不合法。") from exc

    allowlisted = _is_host_allowlisted(hostname, port, host_allowlist)
    if scheme not in {"http", "https"}:
        raise InputValidationError("模型地址必须使用 https:// 协议。")
    if scheme == "http" and not (allow_http or allowlisted):
        raise InputValidationError("模型地址必须使用 https://，除非目标主机已加入管理员白名单。")

    if allowlisted:
        return normalized

    if hostname in _BLOCKED_HOSTNAMES or hostname.endswith(".localhost") or hostname.endswith(".internal"):
        raise InputValidationError("模型地址不能指向回环或内部保留主机名。")

    literal_ip = _parse_ip_literal(hostname)
    if literal_ip is not None:
        if _is_blocked_ip(literal_ip):
            raise InputValidationError("模型地址不能指向回环、私网、链路本地或保留 IP 地址。")
        return normalized

    if not resolve_dns:
        return normalized

    effective_port = port or (443 if scheme == "https" else 80)
    try:
        addr_infos = socket.getaddrinfo(hostname, effective_port, type=socket.SOCK_STREAM)
    except OSError as exc:
        if require_dns_resolution:
            raise ProviderError(f"无法解析模型主机名 {hostname}: {exc}") from exc
        return normalized

    for info in addr_infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        resolved_ip = _parse_ip_literal(str(sockaddr[0]))
        if resolved_ip is not None and _is_blocked_ip(resolved_ip):
            raise InputValidationError("模型地址解析到受限的内网、回环或保留地址，已拒绝访问。")

    return normalized


def request_json_bounded(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    enforce_ssrf: bool = False,
    allow_http: bool = False,
    host_allowlist: Sequence[str] = (),
) -> Any:
    """以流式有界方式执行 JSON HTTP 请求，超限前立即中止，防止响应体 OOM。"""
    if enforce_ssrf:
        validate_user_outbound_url(
            url,
            allow_http=allow_http,
            host_allowlist=host_allowlist,
            resolve_dns=True,
            require_dns_resolution=True,
        )

    if not hasattr(client, "stream"):
        # 兼容仅实现 post/get 的轻量单测替身；真实 httpx.Client 始终走下方流式有界分支。
        method_fn = getattr(client, method.lower())
        response = method_fn(url, headers=headers, json=json_body)
        response.raise_for_status()
        data = response.json()
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if len(encoded) > max_bytes:
            raise ProviderError(
                f"模型响应体大小超过安全上限（{max_bytes} 字节）。",
                code="PROVIDER_RESPONSE_TOO_LARGE",
            )
        return data

    with client.stream(
        method,
        url,
        headers=headers,
        json=json_body,
        follow_redirects=False,
    ) as response:
        if 300 <= response.status_code < 400:
            raise ProviderError(f"模型端点返回未授权重定向（HTTP {response.status_code}）。")
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > max_bytes:
                    raise ProviderError(
                        f"模型响应体大小超过安全上限（{max_bytes} 字节）。",
                        code="PROVIDER_RESPONSE_TOO_LARGE",
                    )
            except ValueError:
                pass
        response.raise_for_status()
        raw_bytes = read_bounded_bytes(response, max_bytes=max_bytes)

    try:
        return json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError(f"模型响应 JSON 解析失败: {exc}") from exc


def read_bounded_bytes(response: httpx.Response, *, max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES) -> bytes:
    """流式读取响应体，超过上限立即中止，兼容 chunked 传输。"""
    chunks = bytearray()
    for chunk in response.iter_bytes():
        if not chunk:
            continue
        chunks.extend(chunk)
        if len(chunks) > max_bytes:
            raise ProviderError(
                f"模型响应体大小超过安全上限（{max_bytes} 字节）。",
                code="PROVIDER_RESPONSE_TOO_LARGE",
            )
    return bytes(chunks)


def _is_host_allowlisted(hostname: str, port: int | None, host_allowlist: Sequence[str]) -> bool:
    if not host_allowlist:
        return False
    host_port = f"{hostname}:{port}" if port is not None else hostname
    for entry in host_allowlist:
        normalized = str(entry or "").strip().lower()
        if not normalized:
            continue
        if "://" in normalized:
            parsed = urlsplit(normalized)
            entry_host = (parsed.hostname or "").strip().lower().rstrip(".")
            entry_port = parsed.port
            if entry_host and (
                (entry_port is None and hostname == entry_host)
                or (entry_port is not None and hostname == entry_host and port == entry_port)
            ):
                return True
            continue
        if normalized == hostname or normalized == host_port:
            return True
    return False


def _parse_ip_literal(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    cleaned = value.strip().strip("[]")
    if "%" in cleaned:
        cleaned = cleaned.split("%", 1)[0]
    try:
        return ipaddress.ip_address(cleaned)
    except ValueError:
        pass
    # 处理十进制整数或十六进制等非规范 IPv4 表达（如 2130706433 / 0x7f000001）。
    try:
        packed = socket.inet_aton(cleaned)
        return ipaddress.IPv4Address(packed)
    except OSError:
        return None


def _is_blocked_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        ip_obj = ip_obj.ipv4_mapped
    return bool(
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )
