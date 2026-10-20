"""Validated runtime settings for the customer service Harness."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CustomerHarnessSettings:
    redis_url: str = "redis://127.0.0.1:6379/4"
    redis_prefix: str = "nanoclaw:customer-harness"
    session_ttl_seconds: int = 7 * 24 * 60 * 60
    response_cache_ttl_seconds: int = 300
    summary_turns: int = 15
    recent_turns: int = 5
    route_confidence_threshold: float = 0.70
    private_context_transfer_approved: bool = False
    tool_timeout_seconds: float = 45.0
    tool_max_attempts: int = 2
    backoff_base_seconds: float = 0.5
    circuit_window: int = 12
    circuit_failure_threshold: int = 8
    circuit_consecutive_failures: int = 5
    circuit_cooldown_seconds: int = 60

    def __post_init__(self) -> None:
        if self.session_ttl_seconds < 60:
            raise ValueError("customer_harness_session_ttl_invalid")
        if self.response_cache_ttl_seconds < 1:
            raise ValueError("customer_harness_response_cache_ttl_invalid")
        if self.summary_turns < 1 or self.recent_turns < 1:
            raise ValueError("customer_harness_history_window_invalid")
        if not 0 < self.route_confidence_threshold <= 1:
            raise ValueError("customer_harness_route_threshold_invalid")
        if self.tool_timeout_seconds <= 0 or self.tool_max_attempts < 1:
            raise ValueError("customer_harness_retry_invalid")
        if self.backoff_base_seconds < 0:
            raise ValueError("customer_harness_backoff_invalid")
        if not 1 <= self.circuit_failure_threshold <= self.circuit_window:
            raise ValueError("customer_harness_circuit_threshold_invalid")
        if not 1 <= self.circuit_consecutive_failures <= self.circuit_window:
            raise ValueError("customer_harness_circuit_consecutive_invalid")
        if self.circuit_cooldown_seconds < 1 or not self.redis_prefix:
            raise ValueError("customer_harness_runtime_settings_invalid")
