"""Official ID photos (INE or passport) that travellers send."""

import base64
import logging
from dataclasses import dataclass

from django.core.files.base import ContentFile

from chatbot.models import Prospect

from . import prompts

log = logging.getLogger(__name__)

REJECT_MARKER = 'INVALID_IMAGE_REJECT'

VISION_INSTRUCTIONS = (
    "Verify if this image is an official ID card (INE/Passport) representing a real person. If it IS an official ID "
    "card, extract the Full Name and Date of Birth strictly. If it is NOT an official ID card (e.g. a drawing, a "
    f"landscape, a random object, a pet), output EXACTLY the phrase '{REJECT_MARKER}'."
)

IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/webp'}


@dataclass
class Upload:
    file_id: str
    filename: str
    content: bytes
    mime_type: str


def store(telegram_id: str, upload: Upload) -> Prospect:
    prospect, _ = Prospect.objects.get_or_create(telegram_id=telegram_id)
    if prospect.ine_file:
        # Keep one ID per traveller: a new photo replaces the old one on disk too.
        prospect.ine_file.delete(save=False)
    prospect.ine_file_id = upload.file_id
    prospect.ine_file.save(upload.filename, ContentFile(upload.content), save=True)
    return prospect


async def describe_for_model(client, vision_model: str, upload: Upload, caption: str) -> str:
    """
    The note the conversation model sees instead of the image. The image goes
    to the vision model as inline data: Telegram's download URL contains the
    bot token and must never leave this process.
    """
    if upload.mime_type not in IMAGE_TYPES:
        return prompts.DOCUMENT_RECEIVED.format(caption=caption)
    data_url = f"data:{upload.mime_type};base64,{base64.b64encode(upload.content).decode()}"
    try:
        response = await client.chat.completions.create(
            model=vision_model,
            messages=[{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': VISION_INSTRUCTIONS},
                    {'type': 'image_url', 'image_url': {'url': data_url}},
                ],
            }],
            max_tokens=300,
        )
        extracted = (response.choices[0].message.content or '').strip()
    except Exception:
        log.exception('ID verification failed')
        return prompts.ID_UNVERIFIED.format(caption=caption)
    if REJECT_MARKER in extracted.upper():
        return prompts.ID_REJECTED.format(caption=caption)
    return prompts.ID_ACCEPTED.format(data=extracted, caption=caption)
