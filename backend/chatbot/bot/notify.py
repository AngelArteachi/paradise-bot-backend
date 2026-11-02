"""Tells the advisors' Telegram chat that a traveller is ready to be called."""

from chatbot.models import Prospect


def advisor_summary(prospect: Prospect) -> str:
    children = prospect.passengers_child or 0
    passengers = f"{prospect.passengers_adult} adulto(s)" + (f" y {children} menor(es)" if children else "")
    # The ID itself is never forwarded to the group; advisors open it from the panel.
    return "\n".join([
        "Nuevo prospecto completo",
        "",
        f"Nombre: {prospect.name}",
        f"Teléfono: {prospect.phone}",
        f"Correo: {prospect.email}",
        f"Destino: {prospect.destination}",
        f"Fechas: {prospect.dates} ({prospect.nights} noches)",
        f"Pasajeros: {passengers}",
        "Identificación: recibida",
        "",
        "Contáctalo desde el panel de prospectos.",
    ])
