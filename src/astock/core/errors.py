"""Stable error taxonomy and bounded public impact mapping."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from astock.core.logging import current_correlation_id, emit_operational_event
from astock.schemas.operational import (
    OperationalEventKind,
    OperationalSeverity,
    PublicErrorSummary,
)


class FailureClass(StrEnum):
    NETWORK = "NETWORK"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    ACCESS_RESTRICTED = "ACCESS_RESTRICTED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    PAGINATION_CYCLE = "PAGINATION_CYCLE"
    DATA_QUALITY = "DATA_QUALITY"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    CONFLICT = "CONFLICT"
    POLICY_REJECTED = "POLICY_REJECTED"
    STORAGE = "STORAGE"
    INTERNAL = "INTERNAL"


class AStockError(Exception):
    """Base error with a stable machine-readable classification."""

    def __init__(
        self,
        message: str,
        *,
        failure_class: FailureClass = FailureClass.INTERNAL,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.retryable = retryable
        self.details = details or {}


class ProviderError(AStockError):
    """External provider request or contract error."""


class DataQualityError(AStockError):
    """Data failed a hard quality gate."""


class PolicyError(AStockError):
    """A requested write violates a project policy."""


class StorageError(AStockError):
    """Durable state could not be read or written safely."""


_PUBLIC_MESSAGES: dict[str, tuple[str, str]] = {
    FailureClass.NETWORK.value: (
        "当前无法取得部分最新数据，涉及实时信息的判断暂不下结论。",
        "外部网络请求失败；请按关联号查看内部来源、重试与回退记录。",
    ),
    FailureClass.TIMEOUT.value: (
        "部分最新数据暂时没有在允许时间内取得，相关判断会保持保守。",
        "外部请求超时；请检查来源延迟、重试预算和备用路径。",
    ),
    FailureClass.RATE_LIMITED.value: (
        "当前外部数据服务暂时限流，涉及最新数据的判断可能延后。",
        "外部来源触发限流；请检查 Retry-After、熔断状态和备用来源。",
    ),
    "REMOTE_5XX": (
        "当前外部数据服务暂时异常，涉及最新数据的部分先不下判断。",
        "上游服务返回服务器错误；请检查重试、熔断和备用路径。",
    ),
    FailureClass.AUTH_REQUIRED.value: (
        "当前有一项授权数据暂时不可用，依赖该数据的部分不会强行给结论。",
        "数据源需要有效认证；请检查凭证配置和最小权限，不要把凭证写入日志。",
    ),
    FailureClass.ACCESS_RESTRICTED.value: (
        "当前有一项外部数据无法正常访问，相关判断会保持不确定。",
        "数据源访问受限；请检查权限、地区限制、风控和可替代正式来源。",
    ),
    FailureClass.INVALID_RESPONSE.value: (
        "当前数据格式出现异常，相关数字在重新核实前不会用于投资判断。",
        "上游响应不符合已登记合同；请检查数据合同变化并保持原始响应可追溯。",
    ),
    "SCHEMA_DRIFT": (
        "当前数据格式发生变化，相关数字在重新核实前不会用于投资判断。",
        "检测到数据合同漂移；请按关联号检查原始响应和受控修复记录。",
    ),
    "INVALID_PAYLOAD": (
        "当前取得的数据无法可靠解析，相关数字暂不用于投资判断。",
        "上游响应无法通过解析或验证；请检查原始快照和解析合同。",
    ),
    FailureClass.PAGINATION_CYCLE.value: (
        "当前资料枚举没有可靠完成，依赖其覆盖范围的判断会保持不确定。",
        "枚举过程出现分页循环或终止条件异常；请按关联号检查分页记录。",
    ),
    FailureClass.DATA_QUALITY.value: (
        "当前数据质量没有通过校验，相关结论会等待可靠数据后再更新。",
        "数据质量门失败；请检查冲突、缺失、时间边界和质量报告。",
    ),
    FailureClass.CONFLICT.value: (
        "不同可靠来源的数据目前存在冲突，冲突解决前不会选择对结论更有利的一方。",
        "来源之间存在实质冲突；请按关联号检查各自快照、时间和归一化口径。",
    ),
    "CONFLICTED": (
        "不同可靠来源的数据目前存在冲突，冲突解决前不会选择对结论更有利的一方。",
        "来源之间存在实质冲突；请按关联号检查各自快照、时间和归一化口径。",
    ),
    "STALE_DATA": (
        "现有数据已经超过本次判断允许的新鲜度，涉及当前状态的部分暂不下结论。",
        "缓存或冻结数据超过能力新鲜度要求；请刷新或使用合格备用来源。",
    ),
    FailureClass.CAPABILITY_UNAVAILABLE.value: (
        "当前缺少一项会影响判断的数据能力，相关部分只能保持不确定。",
        "请求能力当前没有可用且合格的自动来源；请检查资格、健康和备用路径。",
    ),
    FailureClass.POLICY_REJECTED.value: (
        "当前请求超出了已验证的数据或安全边界，因此不会强行执行。",
        "请求被确定性策略拒绝；请检查来源资格、时间边界或安全约束。",
    ),
    FailureClass.STORAGE.value: (
        "本地研究状态暂时无法可靠保存或读取，本次不会把未确认结果当成已完成。",
        "本地存储异常；请按关联号检查对象存储、状态库和文件系统一致性。",
    ),
    FailureClass.INTERNAL.value: (
        "当前有一项内部处理没有可靠完成，本次不会展示未经确认的结果。",
        "内部处理失败；请按关联号查看受控日志并定位具体组件。",
    ),
}

_FAILURE_ALIASES: dict[str, str] = {
    "429": FailureClass.RATE_LIMITED.value,
    "HTTP_429": FailureClass.RATE_LIMITED.value,
    "TOO_MANY_REQUESTS": FailureClass.RATE_LIMITED.value,
    "RATE_LIMIT": FailureClass.RATE_LIMITED.value,
    "RATE_LIMITED": FailureClass.RATE_LIMITED.value,
    "500": "REMOTE_5XX",
    "502": "REMOTE_5XX",
    "503": "REMOTE_5XX",
    "504": "REMOTE_5XX",
    "HTTP_500": "REMOTE_5XX",
    "HTTP_502": "REMOTE_5XX",
    "HTTP_503": "REMOTE_5XX",
    "HTTP_504": "REMOTE_5XX",
    "REMOTE_5XX": "REMOTE_5XX",
    "TRANSIENT_NETWORK": FailureClass.NETWORK.value,
    "NETWORK_ERROR": FailureClass.NETWORK.value,
    "CONNECT_ERROR": FailureClass.NETWORK.value,
    "CONNECTION_ERROR": FailureClass.NETWORK.value,
    "TIMEOUT_ERROR": FailureClass.TIMEOUT.value,
    "TIMEOUTEXCEPTION": FailureClass.TIMEOUT.value,
    "TIMEOUTERROR": FailureClass.TIMEOUT.value,
    "AUTH_CONFIG": FailureClass.AUTH_REQUIRED.value,
    "COVERAGE_INCOMPLETE": FailureClass.CAPABILITY_UNAVAILABLE.value,
    "SCHEMA_ERROR": "SCHEMA_DRIFT",
    "SCHEMA_DRIFT": "SCHEMA_DRIFT",
    "DIALECT_DRIFT": "SCHEMA_DRIFT",
    "PARSE_ERROR": "INVALID_PAYLOAD",
    "PARSING_ERROR": "INVALID_PAYLOAD",
    "INVALID_PAYLOAD": "INVALID_PAYLOAD",
    "CONFLICTED": "CONFLICTED",
    "STALE": "STALE_DATA",
    "STALE_DATA": "STALE_DATA",
    "LOCAL_CORRUPTION": FailureClass.STORAGE.value,
}


class PublicErrorMapper:
    """Map failures to a bounded user impact and correlation-addressable diagnostics."""

    @classmethod
    def summarize(
        cls,
        failure: FailureClass | str | int | BaseException,
        *,
        correlation_id: str | None = None,
    ) -> PublicErrorSummary:
        key = cls.failure_key(failure)
        investor, developer = _PUBLIC_MESSAGES.get(
            key,
            _PUBLIC_MESSAGES[FailureClass.INTERNAL.value],
        )
        return PublicErrorSummary(
            correlation_id=correlation_id or current_correlation_id(),
            failure_class=key,
            investor_message=investor,
            developer_summary=developer,
        )

    @classmethod
    def record(
        cls,
        failure: FailureClass | str | int | BaseException,
        *,
        component: str,
        event: str = "public_error_mapped",
        correlation_id: str | None = None,
        context: Mapping[str, Any] | None = None,
        severity: OperationalSeverity = OperationalSeverity.ERROR,
    ) -> PublicErrorSummary:
        """Create the public summary and record internal impact under the same id."""

        summary = cls.summarize(failure, correlation_id=correlation_id)
        diagnostic_context: dict[str, Any] = {
            "investor_impact": summary.investor_message,
            "error_type": type(failure).__name__ if isinstance(failure, BaseException) else None,
        }
        if isinstance(failure, AStockError):
            diagnostic_context["retryable"] = failure.retryable
            diagnostic_context["details"] = failure.details
        if context:
            diagnostic_context.update(dict(context))
        emit_operational_event(
            component=component,
            event=event,
            event_kind=OperationalEventKind.OPERATIONAL,
            severity=severity,
            failure_class=summary.failure_class,
            correlation_id=summary.correlation_id,
            context=diagnostic_context,
        )
        return summary

    @staticmethod
    def failure_key(failure: FailureClass | str | int | BaseException) -> str:
        if isinstance(failure, AStockError):
            return failure.failure_class.value
        if isinstance(failure, FailureClass):
            return failure.value
        if isinstance(failure, TimeoutError):
            return FailureClass.TIMEOUT.value
        if isinstance(failure, ConnectionError):
            return FailureClass.NETWORK.value
        if isinstance(failure, OSError):
            return FailureClass.NETWORK.value
        if isinstance(failure, BaseException):
            name = type(failure).__name__.upper()
            if "TIMEOUT" in name:
                return FailureClass.TIMEOUT.value
            if any(token in name for token in ("CONNECT", "NETWORK", "TRANSPORT")):
                return FailureClass.NETWORK.value
            return FailureClass.INTERNAL.value

        normalized = str(failure).strip().upper().replace("-", "_").replace(" ", "_")
        if not normalized:
            return FailureClass.INTERNAL.value
        if normalized in _PUBLIC_MESSAGES:
            return normalized
        if normalized in _FAILURE_ALIASES:
            return _FAILURE_ALIASES[normalized]
        if normalized.isdecimal() and 500 <= int(normalized) <= 599:
            return "REMOTE_5XX"
        if "TIMEOUT" in normalized:
            return FailureClass.TIMEOUT.value
        if "429" in normalized or "RATE_LIMIT" in normalized:
            return FailureClass.RATE_LIMITED.value
        if any(token in normalized for token in ("AUTH", "401", "403")):
            return FailureClass.AUTH_REQUIRED.value
        if any(token in normalized for token in ("SCHEMA", "DIALECT")):
            return "SCHEMA_DRIFT"
        if any(token in normalized for token in ("PARSE", "PAYLOAD", "INVALID_RESPONSE")):
            return "INVALID_PAYLOAD"
        if "CONFLICT" in normalized:
            return "CONFLICTED"
        if "STALE" in normalized or "FRESHNESS" in normalized:
            return "STALE_DATA"
        if any(token in normalized for token in ("NETWORK", "CONNECT", "TRANSPORT")):
            return FailureClass.NETWORK.value
        if any(token in normalized for token in ("500", "502", "503", "504", "5XX")):
            return "REMOTE_5XX"
        return FailureClass.INTERNAL.value


__all__ = [
    "AStockError",
    "DataQualityError",
    "FailureClass",
    "PolicyError",
    "ProviderError",
    "PublicErrorMapper",
    "StorageError",
]
