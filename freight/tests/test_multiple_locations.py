"""Approved locations share one route without broadening it to a whole system."""

from unittest.mock import patch

from bs4 import BeautifulSoup

from django.core.exceptions import ValidationError
from django.db import transaction
from django.forms.models import model_to_dict
from django.urls import reverse

from app_utils.testing import NoSocketsTestCase

from freight.forms import CalculatorForm, PricingAdminForm
from freight.models import Contract, PilotAvailability, Pricing
from freight.tests.testdata.factories_2 import (
    ContractFactory,
    ContractHandlerFactory,
    LocationStructureFactory,
    PricingFactory,
    UserMainDefaultFactory,
)


class TestMultipleLocations(NoSocketsTestCase):
    def setUp(self):
        self.user = UserMainDefaultFactory()
        self.handler = ContractHandlerFactory(user=self.user)
        self.start = LocationStructureFactory(
            name="Jita - Pickup", solar_system_id=30000142
        )
        self.end = LocationStructureFactory(
            name="Amamake - Delivery", solar_system_id=30002537
        )
        self.alternative_start = LocationStructureFactory(
            name="Jita - Other pickup", solar_system_id=self.start.solar_system_id
        )
        self.alternative_end = LocationStructureFactory(
            name="Amamake - Other delivery", solar_system_id=self.end.solar_system_id
        )
        self.unapproved = LocationStructureFactory(
            name="Amamake - Unapproved", solar_system_id=self.end.solar_system_id
        )
        self.pricing = PricingFactory(
            start_location=self.start, end_location=self.end, price_base=10_000_000
        )
        self.pricing.additional_start_locations.add(self.alternative_start)
        self.pricing.additional_end_locations.add(self.alternative_end)

    def contract(self, start, end, **kwargs):
        return ContractFactory(
            handler=self.handler,
            user=self.user,
            start_location=start,
            end_location=end,
            **kwargs,
        )

    def admin_data(self, pricing=None, **kwargs):
        data = {
            key: "" if value is None else value
            for key, value in model_to_dict(pricing or self.pricing).items()
        }
        for side in ("start", "end"):
            key = f"additional_{side}_locations"
            data[key] = [location.pk for location in data[key]]
        data.update(kwargs)
        return data

    def test_matches_every_approved_combination_in_both_directions(self):
        approved = []
        for start in (self.start, self.alternative_start):
            for end in (self.end, self.alternative_end):
                approved.extend([self.contract(start, end), self.contract(end, start)])
        unapproved = self.contract(self.start, self.unapproved)
        Contract.objects.update_pricing()
        for contract in approved:
            contract.refresh_from_db()
            self.assertEqual(contract.pricing, self.pricing)
        unapproved.refresh_from_db()
        self.assertIsNone(unapproved.pricing)
        self.assertEqual(Pricing.objects.count(), 1)

    def test_direction_and_active_flag_apply_to_alternatives(self):
        self.pricing.is_bidirectional = False
        self.pricing.save()
        forward = self.contract(self.alternative_start, self.alternative_end)
        reverse_contract = self.contract(self.alternative_end, self.alternative_start)
        Contract.objects.update_pricing()
        forward.refresh_from_db()
        reverse_contract.refresh_from_db()
        self.assertEqual(forward.pricing, self.pricing)
        self.assertIsNone(reverse_contract.pricing)
        self.pricing.is_active = False
        self.pricing.save()
        Contract.objects.update_pricing()
        forward.refresh_from_db()
        self.assertIsNone(forward.pricing)

    def test_rush_and_standard_remain_separate(self):
        rush = PricingFactory(
            start_location=self.start,
            end_location=self.end,
            is_rush=True,
            price_base=30_000_000,
        )
        rush.additional_end_locations.add(self.alternative_end)
        contract = self.contract(
            self.start, self.alternative_end, title="RUSH CONTRACT", reward=10_000_000
        )
        Contract.objects.update_pricing()
        contract.refresh_from_db()
        self.assertEqual(contract.pricing, rush)
        self.assertTrue(contract.has_pricing_errors)
        contract.title = "Normal delivery"
        contract.save()
        Contract.objects.update_pricing()
        contract.refresh_from_db()
        self.assertEqual(contract.pricing, self.pricing)
        self.assertFalse(contract.has_pricing_errors)

    def test_membership_changes_refresh_after_commit_and_remove_stale_pricing(self):
        contract = self.contract(self.start, self.alternative_end)
        Contract.objects.update_pricing()
        with patch("freight.tasks.update_contracts_pricing.delay") as refresh:
            with self.captureOnCommitCallbacks(execute=True):
                self.pricing.additional_end_locations.clear()
                refresh.assert_not_called()
            refresh.assert_called_once_with()
        Contract.objects.update_pricing()
        contract.refresh_from_db()
        self.assertIsNone(contract.pricing)
        self.assertIsNone(contract.issues)

    def test_model_rejects_cross_system_and_unknown_alternatives(self):
        unknown = LocationStructureFactory(solar_system_id=None)
        for location in (self.alternative_start, unknown):
            with self.subTest(location=location), self.assertRaises(ValidationError):
                with transaction.atomic():
                    self.pricing.additional_end_locations.add(location)

    def test_refreshed_system_metadata_does_not_expand_route(self):
        contract = self.contract(self.start, self.alternative_end)
        self.alternative_end.solar_system_id = self.start.solar_system_id
        self.alternative_end.save()
        Contract.objects.update_pricing()
        contract.refresh_from_db()
        self.assertIsNone(contract.pricing)

    def test_admin_accepts_group_and_validates_systems(self):
        form = PricingAdminForm(self.admin_data(), instance=self.pricing)
        self.assertTrue(form.is_valid(), form.errors)
        form = PricingAdminForm(
            self.admin_data(additional_end_locations=[self.alternative_start.pk]),
            instance=self.pricing,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("additional_end_locations", form.errors)

    def test_admin_saves_approved_locations_and_queues_refresh_after_commit(self):
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save()
        self.client.force_login(self.user)
        with patch("freight.tasks.update_contracts_pricing.delay") as refresh:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("admin:freight_pricing_change", args=[self.pricing.pk]),
                    self.admin_data(additional_start_locations=[], _save="Save"),
                )
                self.assertEqual(response.status_code, 302)
                refresh.assert_not_called()
            self.assertTrue(refresh.called)
        self.assertFalse(self.pricing.additional_start_locations.exists())
        self.assertEqual(
            list(self.pricing.additional_end_locations.all()), [self.alternative_end]
        )

    def test_rolled_back_membership_change_does_not_queue_refresh(self):
        with patch("freight.tasks.update_contracts_pricing.delay") as refresh:
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        self.pricing.additional_end_locations.clear()
                        raise RuntimeError("Roll back the edit")
                except RuntimeError:
                    pass
            refresh.assert_not_called()
        self.assertTrue(self.pricing.additional_end_locations.exists())

    def test_admin_prevents_overlapping_forward_and_reverse_routes(self):
        for start, end in (
            (self.start, self.alternative_end),
            (self.alternative_end, self.alternative_start),
        ):
            with self.subTest(start=start, end=end):
                form = PricingAdminForm(
                    self.admin_data(
                        start_location=start.pk,
                        end_location=end.pk,
                        additional_start_locations=[],
                        additional_end_locations=[],
                    )
                )
                self.assertFalse(form.is_valid())
                self.assertIn("overlap", str(form.non_field_errors()))

    def test_admin_allows_separate_rush_and_opposite_one_way_routes(self):
        form = PricingAdminForm(self.admin_data(is_rush=True))
        self.assertTrue(form.is_valid(), form.errors)
        self.pricing.is_bidirectional = False
        self.pricing.save()
        form = PricingAdminForm(
            self.admin_data(
                start_location=self.alternative_end.pk,
                end_location=self.alternative_start.pk,
                additional_start_locations=[],
                additional_end_locations=[],
                is_bidirectional=False,
            )
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_calculator_renders_approved_choices_and_copies_selected_locations(self):
        self.client.force_login(self.user)
        url = reverse("freight:calculator", args=[self.pricing.pk])
        response = self.client.get(url)
        self.assertContains(response, 'name="start_location"')
        self.assertContains(response, 'name="end_location"')
        self.assertContains(response, 'type="hidden" name="start_location"')
        self.assertContains(response, 'type="hidden" name="end_location"')
        self.assertNotContains(response, self.unapproved.name)
        response = self.client.post(
            reverse("freight:calculator"),
            {
                "pricing": self.pricing.pk,
                "start_location": self.alternative_start.pk,
                "end_location": self.alternative_end.pk,
            },
        )
        self.assertContains(response, 'data-clipboard-text="Jita - Other pickup"')
        self.assertContains(response, 'data-clipboard-text="Amamake - Other delivery"')
        self.assertEqual(response.context["price"], 10_000_000)
        document = BeautifulSoup(response.content, "html.parser")
        for side, location in (
            ("start", self.alternative_start),
            ("end", self.alternative_end),
        ):
            selector = document.select_one(f'[data-field-id="id_{side}_location"]')
            self.assertEqual(
                selector.select_one("[data-selected-location]").get_text(),
                location.name,
            )
            self.assertEqual(
                selector.select_one('[aria-pressed="true"]')["data-location-id"],
                str(location.pk),
            )
            self.assertEqual(
                document.select_one(f"#id_{side}_location")["value"], str(location.pk)
            )

    def test_structure_arrows_default_to_primary_route_locations(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("freight:calculator"), {"pricing": self.pricing.pk}
        )
        document = BeautifulSoup(response.content, "html.parser")
        for side, location in (("start", self.start), ("end", self.end)):
            selector = document.select_one(f'[data-field-id="id_{side}_location"]')
            self.assertIsNotNone(selector.select_one('[data-bs-toggle="dropdown"]'))
            self.assertEqual(
                selector.select_one('[aria-pressed="true"]')["data-location-id"],
                str(location.pk),
            )
            self.assertEqual(
                selector.select_one(".copy_to_clipboard")["data-clipboard-text"],
                location.name,
            )

    def test_single_location_has_copy_button_without_an_arrow(self):
        self.pricing.additional_start_locations.clear()
        self.pricing.additional_end_locations.clear()
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("freight:calculator"), {"pricing": self.pricing.pk}
        )
        document = BeautifulSoup(response.content, "html.parser")
        selectors = document.select("[data-contract-location]")
        self.assertEqual(len(selectors), 2)
        for selector in selectors:
            self.assertIsNone(selector.select_one('[data-bs-toggle="dropdown"]'))
            self.assertIsNotNone(selector.select_one(".copy_to_clipboard"))

    def test_calculator_rejects_unapproved_location_and_invalid_route(self):
        form = CalculatorForm(
            {"pricing": self.pricing.pk, "end_location": self.unapproved.pk}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("end_location", form.errors)
        form = CalculatorForm({"pricing": "invalid"})
        self.assertFalse(form.is_valid())

    def test_calculator_defaults_to_primary_locations_for_existing_clients(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("freight:calculator"), {"pricing": self.pricing.pk}
        )
        self.assertEqual(response.context["price"], 10_000_000)
        self.assertEqual(response.context["contract_start_location"], self.start)
        self.assertEqual(response.context["contract_end_location"], self.end)

    def test_rush_calculator_uses_approved_locations(self):
        self.user.is_superuser = True
        self.user.save()
        PilotAvailability.objects.create(user=self.user, is_online=True)
        self.pricing.is_rush = True
        self.pricing.save()
        form = CalculatorForm(
            {
                "pricing": self.pricing.pk,
                "rush": "on",
                "end_location": self.alternative_end.pk,
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.get_calculated_data(self.pricing)[2], 10_000_000)
