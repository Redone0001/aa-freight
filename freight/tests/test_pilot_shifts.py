"""Timed availability is enforced by server time without relying on a browser."""

import datetime as dt
from unittest.mock import patch

from django.test import Client
from django.urls import reverse
from django.utils.timezone import now
from app_utils.testing import NoSocketsTestCase
from app_utils.testdata_factories import UserMainFactory

from freight.models import PilotAvailability
from freight.tests.testdata.factories_2 import PricingFactory


class TestPilotShifts(NoSocketsTestCase):
    def setUp(self):
        self.pilot = UserMainFactory(
            permissions__=[
                "freight.jf_pilot",
                "freight.view_contracts",
                "freight.use_calculator",
            ]
        )
        self.client = Client()
        self.client.force_login(self.pilot)
        self.url = reverse("freight:pilot_availability")

    def update(self, **data):
        return self.client.post(self.url, data, HTTP_ACCEPT="application/json")

    def test_manual_toggle_remains_indefinite_and_clears_previous_deadline(self):
        self.update(online="1", duration_minutes=60)
        response = self.update(online="0")
        shift = PilotAvailability.objects.get(user=self.pilot)
        self.assertFalse(shift.is_online)
        self.assertIsNone(shift.online_until)
        response = self.update(online="1")
        self.assertEqual(
            response.json(),
            {"is_online": True, "online_until": None, "rush_available": True},
        )

    def test_presets_and_custom_minutes(self):
        for minutes in [60, 120, 240, 95]:
            with self.subTest(minutes=minutes):
                start = now()
                response = self.update(online="1", duration_minutes=minutes)
                self.assertEqual(response.status_code, 200)
                shift = PilotAvailability.objects.get(user=self.pilot)
                self.assertAlmostEqual(
                    shift.online_until,
                    start + dt.timedelta(minutes=minutes),
                    delta=dt.timedelta(seconds=2),
                )
                self.assertTrue(response.json()["is_online"])
                self.assertEqual(
                    response.json()["online_until"], shift.online_until.isoformat()
                )

    def test_explicit_end_time_with_offset_is_same_instant(self):
        end = (now() + dt.timedelta(hours=2)).astimezone(
            dt.timezone(dt.timedelta(hours=1))
        )
        response = self.update(online="1", until=end.isoformat())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            PilotAvailability.objects.get(user=self.pilot).online_until, end
        )

    def test_invalid_shift_never_changes_existing_availability(self):
        self.update(online="1", duration_minutes=120)
        original = PilotAvailability.objects.get(user=self.pilot).online_until
        for params in [
            {"duration_minutes": "0"},
            {"duration_minutes": "-1"},
            {"duration_minutes": "1.5"},
            {"duration_minutes": "oops"},
            {"until": "invalid"},
            {"until": "2020-01-01T00:00:00Z"},
            {"until": "2099-01-01T00:00:00"},
            {
                "duration_minutes": 60,
                "until": (now() + dt.timedelta(hours=1)).isoformat(),
            },
        ]:
            with self.subTest(params=params):
                response = self.update(online="1", **params)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    PilotAvailability.objects.get(user=self.pilot).online_until,
                    original,
                )

    def test_expired_shift_is_offline_without_cleanup_task(self):
        self.update(online="1", duration_minutes=60)
        shift = PilotAvailability.objects.get(user=self.pilot)
        with patch("freight.models.routes.now", return_value=shift.online_until):
            self.assertFalse(shift.is_effectively_online)
            self.assertFalse(PilotAvailability.available())
            self.assertFalse(PilotAvailability.online().exists())
            response = self.client.get(reverse("freight:pilot_availability_status"))
            self.assertEqual(
                response.json(),
                {"is_online": False, "online_until": None, "rush_available": False},
            )

    def test_other_active_pilot_keeps_rush_available_after_expiration(self):
        self.update(online="1", duration_minutes=60)
        other = UserMainFactory(permissions__=["freight.jf_pilot"])
        PilotAvailability.objects.create(user=other, is_online=True)
        shift = PilotAvailability.objects.get(user=self.pilot)
        with patch("freight.models.routes.now", return_value=shift.online_until):
            response = self.client.get(reverse("freight:pilot_availability_status"))
            self.assertEqual(
                response.json(),
                {"is_online": False, "online_until": None, "rush_available": True},
            )

    def test_expired_shift_rejects_calculate_reward(self):
        self.update(online="1", duration_minutes=60)
        shift = PilotAvailability.objects.get(user=self.pilot)
        rush = PricingFactory(is_rush=True, price_base=30_000_000)
        with patch("freight.models.routes.now", return_value=shift.online_until):
            response = self.client.post(
                reverse("freight:calculator"),
                {"rush": "on", "pricing": rush.pk, "volume": 1000},
            )
            self.assertContains(response, "no JF pilot is online")
            self.assertNotContains(response, 'data-clipboard-text="RUSH CONTRACT"')

    def test_expired_shift_renders_offline_in_header(self):
        PilotAvailability.objects.create(
            user=self.pilot,
            is_online=True,
            online_until=now() - dt.timedelta(minutes=1),
        )
        response = self.client.get(reverse("freight:contract_list_all"))
        self.assertContains(response, 'aria-pressed="false"')
        self.assertNotContains(response, 'aria-pressed="true"')

    def test_status_endpoint_requires_pilot_permissions(self):
        member = UserMainFactory(permissions__=["freight.view_contracts"])
        self.client.force_login(member)
        self.assertEqual(
            self.client.get(reverse("freight:pilot_availability_status")).status_code,
            403,
        )
        self.assertEqual(self.update(online="1", duration_minutes=60).status_code, 403)
