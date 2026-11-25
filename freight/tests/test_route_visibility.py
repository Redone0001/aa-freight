"""Route group restrictions apply to calculator users while operators see all routes."""

from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.forms.models import model_to_dict
from django.urls import reverse
from django.utils.html import escape

from app_utils.testing import NoSocketsTestCase

from freight import constants
from freight.forms import CalculatorForm
from freight.models import Contract, PilotAvailability, Pricing
from freight.tests.testdata.factories_2 import (
    ContractFactory,
    ContractHandlerFactory,
    LocationStationFactory,
    LocationStructureFactory,
    PricingFactory,
    UserMainDefaultFactory,
)


class TestRouteVisibility(NoSocketsTestCase):
    def setUp(self):
        self.user = UserMainDefaultFactory()
        self.client.force_login(self.user)
        self.group = Group.objects.create(name="Restricted route access")
        self.public = PricingFactory(
            start_location=LocationStationFactory(name="Jita - Public pickup"),
            end_location=LocationStructureFactory(name="Amamake - Public delivery"),
            price_base=5_000_000,
        )
        self.restricted = PricingFactory(
            start_location=LocationStationFactory(name="Private - Pickup"),
            end_location=LocationStructureFactory(name="Restricted - Delivery"),
            price_base=10_000_000,
            is_default=True,
        )
        self.restricted.visibility_groups.add(self.group)

    def assert_restricted_hidden(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, escape(self.restricted.name))
        self.assertNotContains(response, self.restricted.start_location.name)
        self.assertNotContains(response, self.restricted.end_location.name)

    def test_calculator_only_user_sees_open_routes_and_skips_hidden_default(self):
        self.assertEqual(list(Pricing.objects.visible_to(self.user)), [self.public])
        response = self.client.get(reverse("freight:calculator"))
        self.assert_restricted_hidden(response)
        self.assertEqual(response.context["pricing"], self.public)
        self.assertEqual(
            list(response.context["form"].fields["pricing"].queryset), [self.public]
        )

    def test_any_selected_group_grants_access_without_duplicate_routes(self):
        second_group = Group.objects.create(name="Second allowed group")
        self.restricted.visibility_groups.add(second_group)
        self.user.groups.add(second_group)
        self.assertIn(self.restricted, Pricing.objects.visible_to(self.user))
        self.user.groups.add(self.group)
        self.assertEqual(Pricing.objects.visible_to(self.user).count(), 2)
        response = self.client.post(
            reverse("freight:calculator"), {"pricing": self.restricted.pk}
        )
        self.assertEqual(response.context["price"], 10_000_000)

    def test_anonymous_or_unspecified_actor_gets_only_open_route_choices(self):
        self.assertEqual(
            list(Pricing.objects.visible_to(AnonymousUser())), [self.public]
        )
        self.assertEqual(
            list(CalculatorForm().fields["pricing"].queryset), [self.public]
        )

    def test_group_membership_does_not_grant_module_permissions(self):
        user = UserMainDefaultFactory(permissions__=[])
        user.groups.add(self.group)
        self.client.force_login(user)
        response = self.client.get(
            reverse("freight:calculator", args=[self.restricted.pk])
        )
        self.assertEqual(response.status_code, 302)

    def test_additional_freight_permissions_always_bypass_group_restrictions(self):
        for permission in (
            "view_contracts",
            "jf_pilot",
            "view_statistics",
            "add_location",
            "setup_contract_handler",
            "view_pricing",
            "change_pricing",
        ):
            with self.subTest(permission=permission):
                user = UserMainDefaultFactory(
                    permissions__=[
                        "freight.basic_access",
                        "freight.use_calculator",
                        f"freight.{permission}",
                    ]
                )
                self.assertFalse(user.groups.filter(pk=self.group.pk).exists())
                self.assertIn(self.restricted, Pricing.objects.visible_to(user))
                self.client.force_login(user)
                response = self.client.post(
                    reverse("freight:calculator"), {"pricing": self.restricted.pk}
                )
                self.assertEqual(response.context["price"], 10_000_000)

    def test_operator_permissions_inherited_from_a_group_bypass_restrictions(self):
        operators = Group.objects.create(name="Freight operators")
        operators.permissions.add(
            Permission.objects.get(
                content_type__app_label="freight", codename="view_contracts"
            )
        )
        self.user.groups.add(operators)
        response = self.client.get(
            reverse("freight:calculator", args=[self.restricted.pk])
        )
        self.assertEqual(response.context["pricing"], self.restricted)

    def test_other_app_permissions_and_staff_flag_do_not_bypass_restrictions(self):
        user = UserMainDefaultFactory(
            is_staff=True,
            permissions__=[
                "freight.basic_access",
                "freight.use_calculator",
                "auth.view_user",
            ],
        )
        self.assertEqual(list(Pricing.objects.visible_to(user)), [self.public])

    def test_superuser_always_sees_all_routes(self):
        user = UserMainDefaultFactory(is_superuser=True)
        self.assertIn(self.restricted, Pricing.objects.visible_to(user))

    def test_direct_hidden_route_url_uses_a_visible_route(self):
        response = self.client.get(
            reverse("freight:calculator", args=[self.restricted.pk])
        )
        self.assert_restricted_hidden(response)
        self.assertEqual(response.context["pricing"], self.public)

    @patch("freight.views.JANICE_API_KEY", "test-key")
    @patch("freight.forms.create_janice_appraisal")
    def test_forged_calculation_cannot_quote_or_appraise_a_hidden_route(
        self, appraisal
    ):
        response = self.client.post(
            reverse("freight:calculator"),
            {"pricing": self.restricted.pk, "janice_items": "Tritanium 1000"},
        )
        self.assert_restricted_hidden(response)
        self.assertIsNone(response.context["price"])
        self.assertIn("pricing", response.context["form"].errors)
        appraisal.assert_not_called()

    def test_hidden_route_switch_keeps_cargo_but_uses_a_visible_route(self):
        response = self.client.post(
            reverse("freight:calculator"),
            {
                "pricing": self.restricted.pk,
                "calculator_action": "change_route",
                "volume": "12345",
                "collateral": "25000000",
            },
        )
        self.assert_restricted_hidden(response)
        self.assertEqual(response.context["pricing"], self.public)
        self.assertEqual(response.context["form"]["volume"].value(), "12345")
        self.assertIsNone(response.context["price"])

    def test_all_routes_restricted_shows_an_empty_state_without_route_details(self):
        self.public.visibility_groups.add(self.group)
        response = self.client.get(
            reverse("freight:calculator", args=[self.restricted.pk])
        )
        self.assert_restricted_hidden(response)
        self.assertIsNone(response.context["pricing"])
        self.assertContains(response, "No routes are available for your account.")
        self.assertFalse(response.context["form"].fields["pricing"].queryset.exists())

    def test_membership_removal_takes_effect_on_the_next_request(self):
        self.user.groups.add(self.group)
        url = reverse("freight:calculator", args=[self.restricted.pk])
        self.assertEqual(self.client.get(url).context["pricing"], self.restricted)
        self.user.groups.remove(self.group)
        self.assert_restricted_hidden(self.client.get(url))

    def test_clearing_group_selection_makes_the_route_open_again(self):
        self.restricted.visibility_groups.clear()
        response = self.client.get(
            reverse("freight:calculator", args=[self.restricted.pk])
        )
        self.assertEqual(response.context["pricing"], self.restricted)

    def test_dashboard_hides_restricted_routes_and_excludes_them_from_totals(self):
        handler = ContractHandlerFactory()
        ContractFactory(handler=handler, pricing=self.public, volume=100)
        ContractFactory(handler=handler, pricing=self.restricted, volume=900)
        response = self.client.get(reverse("freight:route_dashboard"))
        self.assert_restricted_hidden(response)
        self.assertEqual(response.context["route_dashboard_summary"]["routes"], 1)
        self.assertEqual(response.context["route_dashboard_summary"]["volume"], 100)
        self.user.groups.add(self.group)
        response = self.client.get(reverse("freight:route_dashboard"))
        self.assertEqual(response.context["route_dashboard_summary"]["routes"], 2)
        self.assertEqual(response.context["route_dashboard_summary"]["volume"], 1000)

    def test_trucker_keeps_all_routes_in_dashboard_contract_lists_and_planner(self):
        trucker = UserMainDefaultFactory(
            permissions__=[
                "freight.basic_access",
                "freight.use_calculator",
                "freight.view_contracts",
            ]
        )
        handler = ContractHandlerFactory(user=trucker)
        contract = ContractFactory(
            handler=handler,
            user=self.user,
            pricing=self.restricted,
            volume=1000,
            collateral=1000,
            reward=10_000_000,
        )
        self.client.force_login(trucker)
        response = self.client.get(reverse("freight:route_dashboard"))
        self.assertContains(response, escape(self.restricted.name))
        for url in (
            reverse("freight:contract_list_data", args=[constants.CONTRACT_LIST_ALL]),
            reverse("freight:jf_planner_data"),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    contract.contract_id,
                    [row["contract_id"] for row in response.json()["data"]],
                )

    def test_visibility_does_not_affect_contract_matching_or_price_checks(self):
        contract = ContractFactory(
            handler=ContractHandlerFactory(),
            user=self.user,
            start_location=self.restricted.start_location,
            end_location=self.restricted.end_location,
            reward=10_000_000,
        )
        Contract.objects.update_pricing()
        contract.refresh_from_db()
        self.assertEqual(contract.pricing, self.restricted)
        self.assertFalse(contract.has_pricing_errors)
        self.assertNotIn(self.restricted, Pricing.objects.visible_to(self.user))

    def test_rush_routes_apply_the_same_group_rules(self):
        pilot = UserMainDefaultFactory(permissions__=["freight.jf_pilot"])
        PilotAvailability.objects.create(user=pilot, is_online=True)
        public_rush = PricingFactory(
            start_location=self.public.start_location,
            end_location=self.public.end_location,
            is_rush=True,
            price_base=15_000_000,
        )
        restricted_rush = PricingFactory(
            start_location=self.restricted.start_location,
            end_location=self.restricted.end_location,
            is_rush=True,
            price_base=30_000_000,
        )
        restricted_rush.visibility_groups.add(self.group)
        response = self.client.get(reverse("freight:calculator") + "?rush=1")
        self.assertEqual(
            list(response.context["form"].fields["pricing"].queryset), [public_rush]
        )
        response = self.client.post(
            reverse("freight:calculator"), {"pricing": restricted_rush.pk, "rush": "on"}
        )
        self.assertIsNone(response.context["price"])
        self.user.groups.add(self.group)
        response = self.client.post(
            reverse("freight:calculator"), {"pricing": restricted_rush.pk, "rush": "on"}
        )
        self.assertEqual(response.context["price"], 30_000_000)

    def test_admin_can_edit_visibility_groups(self):
        admin = UserMainDefaultFactory(is_superuser=True, is_staff=True)
        self.client.force_login(admin)
        url = reverse("admin:freight_pricing_change", args=[self.restricted.pk])
        self.assertContains(self.client.get(url), 'name="visibility_groups"')
        other_group = Group.objects.create(name="Replacement access group")
        data = {
            key: "" if value is None else value
            for key, value in model_to_dict(self.restricted).items()
        }
        data.update(visibility_groups=[other_group.pk], _save="Save")
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(list(self.restricted.visibility_groups.all()), [other_group])
