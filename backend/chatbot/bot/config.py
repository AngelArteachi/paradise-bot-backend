import os
from dataclasses import dataclass

from chatbot.models import BotSetting


@dataclass(frozen=True)
class BotConfig:
    """Effective configuration: the panel's settings first, then environment variables."""

    telegram_token: str
    openai_api_key: str
    model: str
    vision_model: str
    temperature: float
    max_tokens: int
    assistant_name: str
    tone: str
    emojis_enabled: bool
    welcome_message: str
    system_instructions: str
    advisor_chat_id: str
    privacy_notice_url: str


def load_config() -> BotConfig:
    setting = BotSetting.objects.order_by('-updated_at').first()

    def pick(field: str, env: str, default: str = '') -> str:
        value = getattr(setting, field, '') if setting else ''
        return value or os.getenv(env, default)

    return BotConfig(
        telegram_token=pick('bot_token', 'TELEGRAM_TOKEN'),
        openai_api_key=pick('openai_api_key', 'OPENAI_API_KEY'),
        model=pick('ai_model', 'OPENAI_MODEL', 'gpt-4o-mini'),
        vision_model=os.getenv('OPENAI_VISION_MODEL', 'gpt-4o-mini'),
        temperature=setting.temperature if setting and setting.temperature is not None else 0.7,
        max_tokens=setting.max_tokens if setting and setting.max_tokens else 500,
        assistant_name=pick('assistant_name', 'ASSISTANT_NAME', 'Asistente Paradise'),
        tone=pick('conversation_tone', 'ASSISTANT_TONE', 'Amigable'),
        emojis_enabled=setting.emojis_enabled if setting is not None else True,
        welcome_message=pick(
            'welcome_message', 'WELCOME_MESSAGE',
            '¡Hola! 🌴 Gracias por comunicarte con Paradise Tour Travel Agency. ¿Cómo podemos ayudarle?',
        ),
        system_instructions=setting.system_instructions if setting else '',
        advisor_chat_id=pick('advisor_chat_id', 'ADVISOR_CHAT_ID'),
        privacy_notice_url=pick('privacy_notice_url', 'PRIVACY_NOTICE_URL'),
    )
