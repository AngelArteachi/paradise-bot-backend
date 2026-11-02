"""The functions the model can call, and what they do to the database."""

from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import transaction

from chatbot.models import Channel, Prospect

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "save_prospect_info",
            "description": "Call this whenever the user provides ANY piece of contact or travel details (name, phone, dates, etc.) to save it into the database for the human advisor.",
            "parameters": {
                "type": "object",
                "properties": {
                    "language": {"type": "string", "description": "Language used by user (es/en)"},
                    "name": {"type": "string"},
                    "phone": {"type": "string", "description": "Format +1234567890 digits only"},
                    "email": {"type": "string"},
                    "dates": {"type": "string"},
                    "nights": {"type": "string"},
                    "destination": {"type": "string", "description": "The travel destination of interest"},
                    "passengers_adult": {"type": "integer"},
                    "passengers_child": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_conversation_finished",
            "description": "Call this function ONLY when the user explicitly indicates they do not need help with anything else, to close the conversation flow.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

TEXT_FIELDS = ('language', 'name', 'email', 'dates', 'destination')


@dataclass
class ToolResult:
    """What the model is told, plus side effects the caller has to carry out."""

    message: str
    finished: bool = False
    newly_complete: Prospect | None = None


@dataclass
class ToolContext:
    telegram_id: str
    channel: str = Channel.TELEGRAM
    results: list[ToolResult] = field(default_factory=list)


def save_prospect_info(telegram_id: str, args: dict, channel: str = Channel.TELEGRAM) -> ToolResult:
    """
    Applies the fields the model extracted. The whole update is validated
    before anything is written, so one bad field (say, a destination outside
    the Caribbean) does not leave half a record behind.
    """
    with transaction.atomic():
        prospect, _ = Prospect.objects.select_for_update().get_or_create(
            telegram_id=telegram_id, defaults={'channel': channel})
        was_complete = prospect.is_complete

        for name in TEXT_FIELDS:
            if args.get(name) not in (None, ''):
                setattr(prospect, name, str(args[name]).strip())
        if args.get('phone') not in (None, ''):
            prospect.phone = ''.join(ch for ch in str(args['phone']) if ch.isdigit() or ch == '+')
        # Models fill fields they do not know yet with 0; a trip has at least one night and one adult.
        if args.get('nights') not in (None, '', 0, '0'):
            prospect.nights = str(args['nights']).strip()
        for name in ('passengers_adult', 'passengers_child'):
            unknown = (None, '', 0, '0') if name == 'passengers_adult' else (None, '')
            if args.get(name) not in unknown:
                try:
                    setattr(prospect, name, int(args[name]))
                except (TypeError, ValueError):
                    return ToolResult(f"Validation error: '{name}' must be a whole number. Ask the user again.")

        try:
            prospect.full_clean()
        except ValidationError as error:
            fields = '; '.join(f"{key}: {' '.join(messages)}" for key, messages in error.message_dict.items())
            return ToolResult(
                f"Validation error, nothing was saved. Tell the user which field was rejected and ask for it again: {fields}"
            )
        prospect.save()

    if prospect.is_complete and not was_complete:
        return ToolResult(
            "Info has been securely saved. The prospect profile is now complete. Tell the user a human advisor will "
            "contact them, and ask if they need help with anything else. Do NOT call mark_conversation_finished yet; "
            "only call it if they explicitly say they do not need further help.",
            newly_complete=prospect,
        )
    return ToolResult("Info has been securely saved in the database.")


def run_tool(context: ToolContext, name: str, args: dict) -> ToolResult:
    if name == 'save_prospect_info':
        result = save_prospect_info(context.telegram_id, args, context.channel)
    elif name == 'mark_conversation_finished':
        result = ToolResult("Conversation marked as finished.", finished=True)
    else:
        result = ToolResult(f"Unknown function {name}.")
    context.results.append(result)
    return result
