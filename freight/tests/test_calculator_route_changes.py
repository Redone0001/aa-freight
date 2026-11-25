"""Changing route keeps cargo input without calculating a stale quote."""

from unittest.mock import patch

from django.urls import reverse

from app_utils.testing import NoSocketsTestCase

from freight.models import PilotAvailability
from freight.tests.testdata.factories_2 import (
    LocationStructureFactory,
    PricingFactory,
    UserMainDefaultFactory,
)


class TestCalculatorRouteChanges(NoSocketsTestCase):
    def setUp(self):
        self.user = UserMainDefaultFactory(is_superuser=True)
        self.client.force_login(self.user)
        self.first = PricingFactory(
            price_per_volume=650, price_per_collateral_percent=1
        )
        self.second = PricingFactory(
            price_per_volume=1200, price_per_collateral_percent=2
        )
        self.cargo = {
            "volume": "12345",
            "collateral": "2450000000",
            "janice_items": "Tritanium\t2000\nIsogen\t500",
            "janice_extra_collateral": "25000000",
            "janice_extra_volume": "150",
        }
        key_patch = patch("freight.views.JANICE_API_KEY", "test-key")
        key_patch.start()
        self.addCleanup(key_patch.stop)

    def change_route(self, pricing=None, **kwargs):
        data = {
            **self.cargo,
            "pricing": (pricing or self.second).pk,
            "calculator_action": "change_route",
            **kwargs,
        }
        return self.client.post(reverse("freight:calculator"), data)

    def assert_cargo_preserved(self, response, cargo=None):
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        for name, value in (self.cargo if cargo is None else cargo).items():
            with self.subTest(field=name):
                self.assertEqual(form[name].value(), value)
        self.assertFalse(form.errors)
        self.assertIsNone(response.context["price"])
        self.assertContains(response, "No price calculated yet")

    @patch("freight.forms.create_janice_appraisal")
    def test_switch_preserves_all_cargo_without_creating_an_appraisal(self, appraisal):
        response = self.change_route()
        self.assert_cargo_preserved(response)
        self.assertEqual(response.context["pricing"], self.second)
        self.assertIsNone(response.context["janice_appraisal_url"])
        appraisal.assert_not_called()

    def test_incomplete_and_zero_inputs_survive_without_premature_validation(self):
        self.cargo.update(volume="", collateral="0", janice_extra_volume="0")
        response = self.change_route()
        self.assert_cargo_preserved(response)

    @patch("freight.forms.create_janice_appraisal")
    def test_preserved_items_are_appraised_only_when_calculating_the_new_route(
        self, appraisal
    ):
        appraisal.return_value = {
            "effectivePrices": {"totalSellPrice": 100_000_000},
            "totalPackagedVolume": 1000,
            "code": "retained-items",
        }
        response = self.change_route()
        appraisal.assert_not_called()
        form = response.context["form"]
        response = self.client.post(
            reverse("freight:calculator"),
            {
                **{name: form[name].value() for name in self.cargo},
                "pricing": self.second.pk,
                "calculator_action": "calculate",
            },
        )
        appraisal.assert_called_once_with("test-key", self.cargo["janice_items"])
        self.assertEqual(response.context["volume"], 1150)
        self.assertEqual(response.context["collateral"], 125_000_000)
        self.assertEqual(response.context["price"], 4_000_000)

    def test_hidden_cargo_survives_switching_through_a_fixed_price_route(self):
        fixed = PricingFactory(price_base=10_000_000)
        response = self.change_route(fixed)
        self.assert_cargo_preserved(response)
        form = response.context["form"]
        self.assertTrue(form.fields["volume"].widget.is_hidden)
        self.assertTrue(form.fields["collateral"].widget.is_hidden)
        response = self.change_route(
            self.first, **{name: form[name].value() for name in self.cargo}
        )
        self.assert_cargo_preserved(response)
        self.assertFalse(response.context["form"].fields["volume"].widget.is_hidden)

    def test_switch_resets_structure_choices_to_the_new_routes_defaults(self):
        self.second.end_location.solar_system_id = 30000142
        self.second.end_location.save()
        LocationStructureFactory(solar_system_id=30000142)
        response = self.change_route(end_location=self.first.end_location_id)
        self.assert_cargo_preserved(response)
        self.assertEqual(
            response.context["form"]["end_location"].value(),
            self.second.end_location_id,
        )

    def test_next_calculation_checks_the_new_routes_limits(self):
        self.second.volume_max = 1000
        self.second.save()
        self.assert_cargo_preserved(self.change_route())
        response = self.client.post(
            reverse("freight:calculator"),
            {
                **self.cargo,
                "pricing": self.second.pk,
                "calculator_action": "calculate",
                "janice_items": "",
            },
        )
        self.assertIsNone(response.context["price"])
        self.assertContains(response, "exceeds the maximum allowed volume")

    def test_rush_and_standard_switches_preserve_cargo(self):
        PilotAvailability.objects.create(user=self.user, is_online=True)
        rush = PricingFactory(is_rush=True, price_per_volume=2500)
        response = self.change_route(self.first, rush="on")
        self.assert_cargo_preserved(response)
        self.assertEqual(response.context["pricing"], rush)
        self.assertTrue(response.context["form"]["rush"].value())
        response = self.change_route(rush)
        self.assert_cargo_preserved(response)
        self.assertFalse(response.context["pricing"].is_rush)

    def test_empty_rush_route_list_keeps_inputs_and_allows_return_to_standard(self):
        PilotAvailability.objects.create(user=self.user, is_online=True)
        response = self.change_route(rush="on")
        self.assert_cargo_preserved(response)
        self.assertIsNone(response.context["pricing"])
        self.assertContains(response, 'name="janice_items"')
        self.assertContains(response, 'name="rush"')
        form = response.context["form"]
        response = self.change_route(
            self.first, **{name: form[name].value() for name in self.cargo}
        )
        self.assert_cargo_preserved(response)
        self.assertEqual(response.context["pricing"], self.first)

    def test_offline_rush_switch_falls_back_to_standard_and_keeps_cargo(self):
        response = self.change_route(rush="on")
        self.assert_cargo_preserved(response)
        self.assertFalse(response.context["pricing"].is_rush)

    def test_invalid_or_inactive_route_falls_back_without_losing_cargo(self):
        inactive = PricingFactory(is_active=False)
        for value in ("invalid", "", str(inactive.pk)):
            with self.subTest(pricing=value):
                response = self.client.post(
                    reverse("freight:calculator"),
                    {
                        **self.cargo,
                        "pricing": value,
                        "calculator_action": "change_route",
                    },
                )
                self.assert_cargo_preserved(response)
                self.assertTrue(response.context["pricing"].is_active)
