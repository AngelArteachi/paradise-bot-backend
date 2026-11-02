"""One turn of the conversation: the model, its tool calls, and the history."""

import json
import logging
from dataclasses import dataclass
from datetime import datetime

from asgiref.sync import sync_to_async

from .config import BotConfig
from .prompts import system_prompt
from .tools import SCHEMAS, ToolContext, ToolResult, run_tool

log = logging.getLogger(__name__)

# Enough rounds for "save, then save again, then answer"; stops a model that loops on tools.
MAX_TOOL_ROUNDS = 4
# Messages kept from earlier turns. The full transcript is in the database either way.
HISTORY_LIMIT = 30

FALLBACK_REPLY = 'Lo siento, tuve un problema para responder. ¿Me lo puedes repetir en un momento?'


def trim_history(messages: list[dict], limit: int = HISTORY_LIMIT) -> list[dict]:
    """
    Keeps the latest messages, starting at a user turn. Cutting in the middle
    of a tool exchange would leave a `tool` message without the assistant call
    it answers, which the API rejects.
    """
    if len(messages) <= limit:
        return messages
    trimmed = messages[-limit:]
    while trimmed and trimmed[0].get('role') != 'user':
        trimmed = trimmed[1:]
    return trimmed


@dataclass
class Turn:
    reply: str
    messages: list[dict]
    tools: list[ToolResult]

    @property
    def finished(self) -> bool:
        return any(result.finished for result in self.tools)

    @property
    def completed_prospects(self):
        return [result.newly_complete for result in self.tools if result.newly_complete is not None]


class Assistant:
    def __init__(self, client, config: BotConfig):
        self.client = client
        self.config = config

    async def respond(self, telegram_id: str, history: list[dict], user_content: str, now: datetime) -> Turn:
        messages = [*history, {'role': 'user', 'content': user_content}]
        context = ToolContext(telegram_id=telegram_id)
        system = {'role': 'system', 'content': system_prompt(self.config, now)}

        reply = None
        for _round in range(MAX_TOOL_ROUNDS):
            response = await self.client.chat.completions.create(
                model=self.config.model,
                messages=[system, *messages],
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                tools=SCHEMAS,
                tool_choice='auto',
            )
            message = response.choices[0].message
            if not message.tool_calls:
                reply = (message.content or '').strip()
                break

            messages.append({
                'role': 'assistant',
                'content': message.content,
                'tool_calls': [
                    {'id': call.id, 'type': 'function', 'function': {'name': call.function.name, 'arguments': call.function.arguments}}
                    for call in message.tool_calls
                ],
            })
            for call in message.tool_calls:
                try:
                    args = json.loads(call.function.arguments or '{}')
                except json.JSONDecodeError:
                    args = {}
                result = await sync_to_async(run_tool)(context, call.function.name, args)
                messages.append({'role': 'tool', 'tool_call_id': call.id, 'content': result.message})
        else:
            log.warning('model kept calling tools for %s; answering with the fallback', telegram_id)

        reply = reply or FALLBACK_REPLY
        messages.append({'role': 'assistant', 'content': reply})
        return Turn(reply=reply, messages=trim_history(messages), tools=context.results)

    async def follow_up(self, history: list[dict], instruction: str, now: datetime) -> str:
        """A message the bot sends on its own, such as a nudge after a long silence."""
        response = await self.client.chat.completions.create(
            model=self.config.model,
            messages=[
                {'role': 'system', 'content': system_prompt(self.config, now)},
                *history,
                {'role': 'system', 'content': instruction},
            ],
            temperature=0.7,
            max_tokens=150,
        )
        return (response.choices[0].message.content or '').strip()
