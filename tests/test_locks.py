"""Store lock TTL is derived from the LLM settings and always exceeds the worst case."""

from __future__ import annotations

import pytest

from tests.conftest import make_settings
from ya_voy.llm.openai_compatible import DEFAULT_BACKOFF_BASE_SECONDS, OpenAICompatibleExtractor
from ya_voy.locks import (
    MIN_SAFETY_MARGIN_SECONDS,
    store_lock_key,
    store_lock_ttl_seconds,
    worst_case_llm_seconds,
)


def test_worst_case_formula_with_defaults() -> None:
    settings = make_settings()
    assert (settings.llm_timeout_seconds, settings.llm_max_retries) == (20, 3)
    # 20 s * 4 attempts + backoff 0.5 + 1 + 2
    assert worst_case_llm_seconds(settings) == pytest.approx(83.5)
    assert store_lock_ttl_seconds(settings) == pytest.approx(83.5 * 1.25)
    assert store_lock_ttl_seconds(settings) > worst_case_llm_seconds(settings)


@pytest.mark.parametrize(
    ("timeout", "retries"),
    [(0.5, 0), (5, 0), (10, 1), (20, 3), (30, 5), (60, 8)],
)
def test_ttl_strictly_exceeds_the_worst_case(timeout: float, retries: int) -> None:
    settings = make_settings(llm_timeout_seconds=timeout, llm_max_retries=retries)
    worst = timeout * (retries + 1) + DEFAULT_BACKOFF_BASE_SECONDS * (2**retries - 1)
    assert worst_case_llm_seconds(settings) == pytest.approx(worst)
    ttl = store_lock_ttl_seconds(settings)
    assert ttl > worst
    assert ttl - worst >= MIN_SAFETY_MARGIN_SECONDS


def test_worst_case_matches_the_client_backoff() -> None:
    client_default = OpenAICompatibleExtractor.__init__.__kwdefaults__["backoff_base_seconds"]
    assert client_default == DEFAULT_BACKOFF_BASE_SECONDS
    assert OpenAICompatibleExtractor.worst_case_seconds(10, 2, 1) == 10 * 3 + 1 + 2


def test_lock_key_is_per_store() -> None:
    assert store_lock_key(42) == "lock:store:42"
