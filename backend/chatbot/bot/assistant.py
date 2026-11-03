"""One turn of the conversation: the model, its tool calls, and the history."""

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace

from asgiref.sync import sync_to_async

from chatbot.destinations import is_caribbean
from chatbot.models import Channel, Prospect

from .config import BotConfig
from .prompts import system_prompt
from .tools import SCHEMAS, ToolContext, ToolResult, run_tool

log = logging.getLogger(__name__)

# Enough rounds for "save, then save again, then answer"; stops a model that loops on tools.
MAX_TOOL_ROUNDS = 4
# Messages kept from earlier turns. The full transcript is in the database either way.
HISTORY_LIMIT = 30

FALLBACK_REPLY = 'Lo siento, tuve un problema para responder. ¿Me lo puedes repetir en un momento?'
TOOL_NAMES = {schema['function']['name'] for schema in SCHEMAS}

CONTACT_DATA = re.compile(r'[^\s@]+@[^\s@]+\.\w+|\d[\d\s().-]{6,}\d')
TRIP_DATA = re.compile(
    r'\b(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre|semana santa|'
    r'navidad|año nuevo|verano|adultos?|niñ[oa]s?|menores|noches?)\b|\d+\s*(adult|niñ|menor|persona|noche|d[ií]a)',
    re.IGNORECASE,
)
SAVE_REMINDER = (
    'The user just gave some of their data and you answered without calling save_prospect_info, so nothing was saved. '
    'Call save_prospect_info now with exactly what they said in their last message.'
)
# A reply that hands the traveller over to an advisor.
HANDOVER = re.compile(r'pondr[áa]n? en contacto|(te|le|lo|la) contactar[áa]n?|will (contact|reach out)|get in touch', re.IGNORECASE)
HANDOVER_REMINDER = (
    'You told the user an advisor will contact them, but their profile is not complete. Still missing: {missing}. '
    'Do not say an advisor will contact them yet: ask for the next missing item.'
)


def gives_data(text: str) -> bool:
    """Whether a message carries something save_prospect_info should store."""
    return bool(CONTACT_DATA.search(text) or TRIP_DATA.search(text) or is_caribbean(text))


def inline_tool_calls(content: str | None) -> list:
    """
    Tool calls a model wrote as JSON in its reply instead of using the API,
    which small open models (Qwen, Llama) sometimes do. Read them as real
    calls, so the data is saved and the visitor never sees the JSON.
    """
    calls, decoder, index = [], json.JSONDecoder(), 0
    text = content or ''
    while (start := text.find('{', index)) != -1:
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        index = end
        if isinstance(value, dict) and value.get('name') in TOOL_NAMES and isinstance(value.get('arguments', {}), dict):
            calls.append(SimpleNamespace(id=f'inline_{uuid.uuid4().hex[:12]}', function=SimpleNamespace(
                name=value['name'], arguments=json.dumps(value.get('arguments', {})))))
    return calls


def what_was_said(history: list[dict], user_content: str) -> str:
    """
    Everything the user wrote, plus the assistant's last message, so that a
    "sí" confirms a proposal such as "serían 5 noches, ¿correcto?".
    """
    said = [m['content'] for m in history if m.get('role') == 'user' and isinstance(m.get('content'), str)]
    last_reply = next((m['content'] for m in reversed(history)
                       if m.get('role') == 'assistant' and isinstance(m.get('content'), str)), '')
    return '\n'.join([*said, user_content, last_reply])


def unique_calls(calls) -> list:
    """Drops repeats: small models sometimes send the same call several times in one response."""
    seen, unique = set(), []
    for call in calls or []:
        key = (call.function.name, call.function.arguments)
        if key not in seen:
            seen.add(key)
            unique.append(call)
    return unique


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
    def __init__(self, client, config: BotConfig, channel: str = Channel.TELEGRAM):
        self.client = client
        self.config = config
        self.channel = channel

    async def respond(self, telegram_id: str, history: list[dict], user_content: str, now: datetime) -> Turn:
        messages = [*history, {'role': 'user', 'content': user_content}]
        context = ToolContext(telegram_id=telegram_id, channel=self.channel, said=what_was_said(history, user_content))
        system = {'role': 'system', 'content': system_prompt(self.config, now, self.channel)}

        reply = None
        # One-off reminders for a model that answers "saved!" without saving, or hands over an unfinished profile.
        # They are never stored in the history.
        reminder: list[dict] = []
        may_remind = gives_data(user_content)
        may_check_handover = True
        for _round in range(MAX_TOOL_ROUNDS):
            response = await self.client.chat.completions.create(
                model=self.config.model,
                messages=[system, *messages, *reminder],
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                tools=SCHEMAS,
                tool_choice='auto',
            )
            message = response.choices[0].message
            tool_calls = unique_calls(message.tool_calls or inline_tool_calls(message.content))
            reminder = []
            if not tool_calls:
                if may_remind and not context.results:
                    may_remind = False
                    reminder = [{'role': 'assistant', 'content': message.content or ''}, {'role': 'system', 'content': SAVE_REMINDER}]
                    continue
                if may_check_handover and HANDOVER.search(message.content or ''):
                    may_check_handover = False
                    missing = await self._missing_fields(telegram_id)
                    if missing:
                        reminder = [{'role': 'assistant', 'content': message.content or ''},
                                    {'role': 'system', 'content': HANDOVER_REMINDER.format(missing=', '.join(missing))}]
                        continue
                reply = (message.content or '').strip()
                break

            messages.append({
                'role': 'assistant',
                'content': message.content if message.tool_calls else None,
                'tool_calls': [
                    {'id': call.id, 'type': 'function', 'function': {'name': call.function.name, 'arguments': call.function.arguments}}
                    for call in tool_calls
                ],
            })
            for call in tool_calls:
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

    @staticmethod
    async def _missing_fields(telegram_id: str) -> list[str]:
        prospect = await Prospect.objects.filter(telegram_id=telegram_id).afirst()
        return (prospect or Prospect(telegram_id=telegram_id)).missing_fields

    async def follow_up(self, history: list[dict], instruction: str, now: datetime) -> str:
        """A message the bot sends on its own, such as a nudge after a long silence."""
        response = await self.client.chat.completions.create(
            model=self.config.model,
            messages=[
                {'role': 'system', 'content': system_prompt(self.config, now, self.channel)},
                *history,
                {'role': 'system', 'content': instruction},
            ],
            temperature=0.7,
            max_tokens=150,
        )
        return (response.choices[0].message.content or '').strip()
