from datetime import datetime
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from chatbot.bot import office_hours
from chatbot.destinations import is_caribbean
from chatbot.models import Prospect

CANCUN = ZoneInfo('America/Cancun')


def at(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=CANCUN)


class CaribbeanDestinationsTest(SimpleTestCase):
    def test_accepts_caribbean_destinations_with_or_without_accents(self):
        for destination in ['Cancún', 'cancun', 'Riviera Maya', 'Punta Cana, República Dominicana', 'La Habana, Cuba', 'Bacalar']:
            self.assertTrue(is_caribbean(destination), destination)

    def test_rejects_everything_else(self):
        for destination in ['Tokio', 'París', 'Ciudad de México', 'Las Vegas', 'Machu Picchu']:
            self.assertFalse(is_caribbean(destination), destination)


class ProspectValidationTest(TestCase):
    def test_model_refuses_a_destination_outside_the_caribbean(self):
        with self.assertRaises(ValidationError) as raised:
            Prospect(telegram_id='1', destination='Japón').full_clean()
        self.assertIn('destination', raised.exception.message_dict)

    def test_is_complete_needs_every_field_an_advisor_uses(self):
        prospect = Prospect(telegram_id='1', name='Ana', phone='9981234567', email='ana@example.com',
                            destination='Cancún', dates='10-15 marzo', nights='5', passengers_adult=2)
        self.assertFalse(prospect.is_complete)  # no ID yet
        prospect.ine_file_id = 'telegram-file-id'
        self.assertTrue(prospect.is_complete)


class OfficeHoursTest(SimpleTestCase):
    # 2 November 2026 is a Monday.

    def test_open_during_the_morning_and_afternoon_blocks(self):
        self.assertTrue(office_hours.is_open(at(2026, 11, 2, 10, 0)))
        self.assertTrue(office_hours.is_open(at(2026, 11, 2, 17, 59)))

    def test_closed_over_lunch_and_after_hours(self):
        self.assertFalse(office_hours.is_open(at(2026, 11, 2, 14, 30)))
        self.assertEqual(office_hours.next_opening(at(2026, 11, 2, 14, 30)), at(2026, 11, 2, 16))
        self.assertEqual(office_hours.next_opening(at(2026, 11, 2, 19)), at(2026, 11, 3, 10))

    def test_saturday_afternoon_and_sunday_wait_until_monday(self):
        self.assertTrue(office_hours.is_open(at(2026, 11, 7, 12)))
        self.assertFalse(office_hours.is_open(at(2026, 11, 7, 13, 30)))
        self.assertEqual(office_hours.next_opening(at(2026, 11, 7, 13, 30)), at(2026, 11, 9, 10))
        self.assertEqual(office_hours.next_opening(at(2026, 11, 8, 11)), at(2026, 11, 9, 10))

    def test_the_prompt_sentence_states_the_fact(self):
        self.assertIn('at the office right now', office_hours.describe(at(2026, 11, 2, 11)))
        closed = office_hours.describe(at(2026, 11, 8, 11))
        self.assertIn('CLOSED', closed)
        self.assertIn('lunes a las 10:00', closed)

    def test_no_follow_ups_at_night(self):
        self.assertTrue(office_hours.is_quiet_time(at(2026, 11, 2, 22)))
        self.assertTrue(office_hours.is_quiet_time(at(2026, 11, 2, 7, 59)))
        self.assertFalse(office_hours.is_quiet_time(at(2026, 11, 2, 12)))
