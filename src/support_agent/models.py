"""Chooses the chat model from environment variables.

SUPPORT_MODEL_PROVIDER = offline (default) | deepseek | openai | azure
  openai: OPENAI_API_KEY, optional OPENAI_MODEL (default gpt-4o-mini)
  azure:  AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_DEPLOYMENT, optional OPENAI_API_VERSION
"""

from __future__ import annotations

import os

from langchain_core.language_models import BaseChatModel

from support_agent.offline_model import OfflineSupportModel


def get_model(provider: str | None = None) -> BaseChatModel:
    provider = (provider or os.getenv("SUPPORT_MODEL_PROVIDER", "offline")).lower()

    if provider == "offline":
        return OfflineSupportModel()

    try:
        from langchain_openai import AzureChatOpenAI, ChatOpenAI
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError("Install the OpenAI extra: pip install -e '.[openai]'") from exc

    if provider == "deepseek":
        # ChatOpenAI renames max_tokens to OpenAI's max_completion_tokens. DeepSeek
        # documents max_tokens; the common adapter preserves that wire contract.
        from langchain_openai.chat_models.base import BaseChatOpenAI

        key = os.getenv("DEEPSEEK_API_KEY")
        if not key or not key.strip():
            raise ValueError("DEEPSEEK_API_KEY must be set in the environment")
        timeout = float(os.getenv("DEEPSEEK_TIMEOUT", "60"))
        max_tokens = int(os.getenv("DEEPSEEK_MAX_TOKENS", "1024"))
        retries = int(os.getenv("DEEPSEEK_MAX_RETRIES", "0"))
        temperature = float(os.getenv("DEEPSEEK_TEMPERATURE", "0"))
        if not 0 < timeout < float("inf") or max_tokens <= 0 or retries < 0 or not 0 <= temperature <= 2:
            raise ValueError("Invalid DeepSeek timeout, output limit, retries or temperature")
        return BaseChatOpenAI(
            api_key=key,
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            timeout=timeout,
            max_tokens=max_tokens,
            max_retries=retries,
            temperature=temperature,
            extra_body={"thinking": {"type": "disabled"}},
            use_responses_api=False,
            streaming=False,
        )
    if provider == "openai":
        return ChatOpenAI(model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"), temperature=0)
    if provider == "azure":
        return AzureChatOpenAI(
            azure_deployment=os.environ["AZURE_OPENAI_DEPLOYMENT"],
            api_version=os.getenv("OPENAI_API_VERSION", "2024-10-21"),
            temperature=0,
        )
    raise ValueError(f"Unknown SUPPORT_MODEL_PROVIDER '{provider}'")
