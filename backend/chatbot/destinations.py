"""Paradise Tour only sells trips to the Caribbean."""

import unicodedata

CARIBBEAN_KEYWORDS = (
    'cancun', 'riviera maya', 'riviera', 'cozumel', 'punta cana', 'cuba', 'habana', 'varadero', 'bahamas',
    'jamaica', 'montego bay', 'puerto rico', 'caribe', 'tulum', 'playa del carmen', 'isla mujeres', 'holbox',
    'bacalar', 'mahahual', 'costa mujeres', 'aruba', 'curazao', 'curacao', 'dominicana', 'santo domingo',
    'samana', 'barbados', 'cayman', 'caiman', 'saint martin', 'san martin', 'st. martin', 'santa lucia',
    'saint lucia', 'turks', 'caicos', 'antigua', 'granada', 'grenada', 'martinica', 'guadalupe', 'san andres',
)


def _normalize(text: str) -> str:
    """Lowercase and drop accents, so 'Cancún' and 'cancun' match."""
    decomposed = unicodedata.normalize('NFD', text.lower())
    return ''.join(ch for ch in decomposed if unicodedata.category(ch) != 'Mn')


def is_caribbean(destination: str) -> bool:
    normalized = _normalize(destination)
    return any(keyword in normalized for keyword in CARIBBEAN_KEYWORDS)
