import logging
import time

import truststore
from django.core.management.base import BaseCommand, CommandError

from chatbot.bot.config import load_config
from chatbot.bot.telegram_app import build_application


class Command(BaseCommand):
    help = 'Run the Paradise Tour Telegram assistant'

    def handle(self, *args, **options):
        # Verify TLS against the operating system's certificate store. This is
        # what makes the bot work behind an antivirus or corporate proxy that
        # inspects HTTPS, without turning certificate verification off.
        truststore.inject_into_ssl()
        logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
        logging.getLogger('httpx').setLevel(logging.WARNING)

        token = load_config().telegram_token
        if not token:
            raise CommandError('Set TELEGRAM_TOKEN in .env or the bot token in the panel.')

        while True:
            try:
                self.stdout.write(self.style.SUCCESS('Bot Paradise Tour escuchando a Telegram. Ctrl+C para detener.'))
                build_application(token).run_polling()
                break
            except KeyboardInterrupt:
                self.stdout.write(self.style.WARNING('Detenido manualmente.'))
                break
            except Exception as error:
                self.stdout.write(self.style.ERROR(f'Error crítico en el bot: {error}. Reiniciando en 5 segundos...'))
                time.sleep(5)
