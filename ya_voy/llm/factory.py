"""Build the configured ``OrderExtractor`` (RNF-13: provider and model are configuration)."""

from __future__ import annotations

from ya_voy.config import Settings
from ya_voy.llm.extractor import OrderExtractor, ScheduleIntentParser
from ya_voy.llm.fake import FakeOrderExtractor
from ya_voy.llm.fake_schedule import FakeScheduleParser
from ya_voy.llm.openai_compatible import OpenAICompatibleExtractor, OpenAICompatibleScheduleParser

FAKE_PROVIDER = "fake"
OPENAI_COMPATIBLE_PROVIDERS = frozenset({"openai", "openai-compatible"})


class LLMConfigError(ValueError):
    pass


def _provider(settings: Settings) -> str:
    provider = settings.llm_provider.strip().lower()
    if provider == FAKE_PROVIDER:
        if not settings.is_development:
            raise LLMConfigError("LLM_PROVIDER=fake is for development only")
        return provider
    if provider in OPENAI_COMPATIBLE_PROVIDERS:
        if not settings.llm_api_key.get_secret_value() or not settings.llm_model:
            raise LLMConfigError("LLM_API_KEY and LLM_MODEL are required for this provider")
        return provider
    raise LLMConfigError(f"unsupported LLM_PROVIDER: {settings.llm_provider!r}")


def build_order_extractor(settings: Settings) -> OrderExtractor:
    if _provider(settings) == FAKE_PROVIDER:
        return FakeOrderExtractor()
    return OpenAICompatibleExtractor.from_settings(settings)


def build_schedule_parser(settings: Settings) -> ScheduleIntentParser:
    if _provider(settings) == FAKE_PROVIDER:
        return FakeScheduleParser()
    return OpenAICompatibleScheduleParser.from_settings(settings)
