"""The functions the model can call, and what they do to the database."""

import re
from dataclasses import dataclass, field
from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

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
                    "check_in": {"type": "string", "description": "Arrival date as YYYY-MM-DD, only when the user gave exact dates. The nights are calculated from check_in and check_out."},
                    "check_out": {"type": "string", "description": "Departure date as YYYY-MM-DD, only when the user gave exact dates."},
                    # No example values here: small models copy them in as if the user had said them.
                    "dates": {"type": "string", "description": "Approximate travel dates, in the user's own words, when they are not exact"},
                    "nights": {"type": "integer", "description": "Number of nights the user said, when the dates are not exact"},
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
MAX_NIGHTS = 60
NUMBER_WORDS = {
    word: number
    for number, words in enumerate([
        (), ('un', 'una', 'uno', 'one'), ('dos', 'two'), ('tres', 'three'), ('cuatro', 'four'), ('cinco', 'five'),
        ('seis', 'six'), ('siete', 'seven'), ('ocho', 'eight'), ('nueve', 'nine'), ('diez', 'ten'), ('once', 'eleven'),
        ('doce', 'twelve'), ('trece',), ('catorce',), ('quince',),
    ])
    for word in words
}


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
    said: str | None = None
    results: list[ToolResult] = field(default_factory=list)


def save_prospect_info(telegram_id: str, args: dict, channel: str = Channel.TELEGRAM, said: str | None = None) -> ToolResult:
    """
    Applies the fields the model extracted. The whole update is validated
    before anything is written, so one bad field (say, a destination outside
    the Caribbean) does not leave half a record behind.

    `said` is what the user wrote in the conversation. When given, nights and
    exact days only count if the user actually said those numbers: small
    models fill them in with guesses.
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
        notes = []
        # Models fill fields they do not know yet with 0; a trip has at least one night and one adult.
        if args.get('nights') not in (None, '', 0, '0'):
            # Models send "5", 5 or "5 noches"; anything without a number is not an answer yet.
            number = re.search(r'\d+', str(args['nights']))
            if not number:
                return ToolResult("Validation error: 'nights' must be a number. Ask the user how many nights.")
            if said is None or mentioned(int(number.group()), said):
                prospect.nights = number.group()
            else:
                notes.append(f'nights was NOT saved: the user never said {number.group()} nights. Do not guess; ask them.')
        for name in ('passengers_adult', 'passengers_child'):
            unknown = (None, '', 0, '0') if name == 'passengers_adult' else (None, '')
            if args.get(name) not in unknown:
                try:
                    setattr(prospect, name, int(args[name]))
                except (TypeError, ValueError):
                    return ToolResult(f"Validation error: '{name}' must be a whole number. Ask the user again.")

        check_in, check_out = args.get('check_in'), args.get('check_out')
        if check_in and check_out:
            try:
                prospect.dates, nights = exact_stay(check_in, check_out, timezone.localdate(), said)
            except ValueError as error:
                if not args.get('dates'):
                    return ToolResult(f"Validation error, nothing was saved: {error}")
                # The user's own words are in dates; the exact days were only the model's reading of them.
                notes.append('check_in and check_out were not valid and were ignored; the dates were saved as the user said them.')
            else:
                # Models get this subtraction wrong; the dates decide over any nights sent in the same call.
                prospect.nights = str(nights)
                notes.append(f"Nights calculated from the dates: {nights}. Confirm it in your reply (e.g. 'serían {nights} noches, ¿correcto?').")
        elif check_in or check_out:
            notes.append("To calculate the nights send both check_in and check_out; if the user only knows one, "
                         "save the approximate dates in 'dates' and ask for the nights.")

        try:
            prospect.full_clean()
        except ValidationError as error:
            fields = '; '.join(f"{key}: {' '.join(messages)}" for key, messages in error.message_dict.items())
            return ToolResult(
                f"Validation error, nothing was saved. Tell the user which field was rejected and ask for it again: {fields}"
            )
        prospect.save()

    saved = ' '.join(['Info has been securely saved.', *notes])
    if prospect.is_complete and not was_complete:
        return ToolResult(
            f"{saved} The prospect profile is now complete. Tell the user a human advisor will "
            "contact them, and ask if they need help with anything else. Do NOT call mark_conversation_finished yet; "
            "only call it if they explicitly say they do not need further help.",
            newly_complete=prospect,
        )
    if prospect.is_complete:
        return ToolResult(f"{saved} The prospect profile is complete.")
    # Without this, small models take "saved" to mean "done" and hand over to an advisor with fields still empty.
    return ToolResult(
        f"{saved} The profile is NOT complete yet. Still missing: {', '.join(prospect.missing_fields)}. "
        "Ask the user for the next missing item. Do NOT say an advisor will contact them until nothing is missing."
    )


def mentioned(number: int, said: str) -> bool:
    """Whether the conversation contains this number, in digits or (up to fifteen) in words."""
    words = re.findall(r'\w+', said.lower())
    return str(number) in re.findall(r'\d+', said) or any(NUMBER_WORDS.get(word) == number for word in words)


def exact_stay(check_in: str, check_out: str, today: date, said: str | None = None) -> tuple[str, int]:
    """The dates as the advisors read them, and the nights between them. The ValueError says what to ask again."""
    try:
        arrival, departure = date.fromisoformat(str(check_in).strip()), date.fromisoformat(str(check_out).strip())
    except ValueError:
        raise ValueError('check_in and check_out must be dates as YYYY-MM-DD.') from None
    # From a real chat: asked about "Semana Santa", a small model made up exact days.
    if said is not None and not (mentioned(arrival.day, said) and mentioned(departure.day, said)):
        raise ValueError("the user never gave those exact days. Save the dates in the user's own words instead.")
    if arrival < today:
        raise ValueError(f'check_in {arrival.isoformat()} is in the past (today is {today.isoformat()}). '
                         'Confirm the dates and the year with the user.')
    nights = (departure - arrival).days
    if not 0 < nights <= MAX_NIGHTS:
        raise ValueError(f'check_out must be after check_in, and a stay is at most {MAX_NIGHTS} nights. '
                         'Confirm the dates with the user.')
    return f'{arrival:%d/%m/%Y} al {departure:%d/%m/%Y}', nights


def run_tool(context: ToolContext, name: str, args: dict) -> ToolResult:
    if name == 'save_prospect_info':
        result = save_prospect_info(context.telegram_id, args, context.channel, context.said)
    elif name == 'mark_conversation_finished':
        result = ToolResult("Conversation marked as finished.", finished=True)
    else:
        result = ToolResult(f"Unknown function {name}.")
    context.results.append(result)
    return result
