"""The language model client, for whichever OpenAI-compatible provider is configured."""

from openai import AsyncOpenAI

from .config import BotConfig


def new_client(config: BotConfig) -> AsyncOpenAI:
    # The SDK refuses an empty key, but local servers such as Ollama ignore it.
    return AsyncOpenAI(api_key=config.ai_api_key or 'not-needed', base_url=config.ai_base_url or None)


def client_for(config: BotConfig, cache: dict) -> AsyncOpenAI:
    """
    One client per key and endpoint, kept in the caller's cache, so changing
    the key in the panel takes effect without a restart. Only for a process
    with a single event loop, like the Telegram bot: a client's connections
    belong to the loop that opened them.
    """
    key = (config.ai_api_key, config.ai_base_url)
    if key not in cache:
        cache[key] = new_client(config)
    return cache[key]
