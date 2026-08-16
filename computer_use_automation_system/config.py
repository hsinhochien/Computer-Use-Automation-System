from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def load_env() -> None:
    env_path = Path('.env')
    if env_path.exists():
        load_dotenv(env_path, override=False)
    else:
        load_dotenv(override=False)


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    api_key: str
    base_url: str
    model: str
    timeout_seconds: int
    azure_api_version: str | None


def get_llm_settings() -> LLMSettings:
    load_env()

    provider = os.getenv('LLM_PROVIDER', 'openai_compatible')
    api_key = os.getenv('LLM_API_KEY', '')
    base_url = os.getenv('LLM_BASE_URL', 'https://api.openai.com/v1')
    model = os.getenv('LLM_MODEL', 'gpt-4o-mini')
    timeout_seconds = int(os.getenv('LLM_TIMEOUT_SECONDS', '60'))
    azure_api_version = os.getenv('AZURE_OPENAI_API_VERSION')

    if not api_key:
        raise RuntimeError('Missing LLM_API_KEY. Please add it to your .env file.')

    if provider == 'azure_openai':
        if not base_url:
            raise RuntimeError('Missing LLM_BASE_URL for Azure OpenAI.')
        if not azure_api_version:
            raise RuntimeError('Missing AZURE_OPENAI_API_VERSION for Azure OpenAI.')
        if not model:
            raise RuntimeError('Missing LLM_MODEL for Azure OpenAI deployment name.')

    return LLMSettings(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout_seconds=timeout_seconds,
        azure_api_version=azure_api_version,
    )
