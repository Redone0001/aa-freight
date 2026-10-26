"""Rush route selection, pilot authorization and notification regression tests."""

from unittest.mock import patch

from django.test import Client
from django.urls import reverse

from app_utils.testing import NoSocketsTestCase
from app_utils.testdata_factories import UserMainFactory

from freight.forms import CalculatorForm
from freight.models import Contract, PilotAvailability
from freight.tests.testdata.factories_2 import (
    ContractFactory,
    ContractHandlerFactory,
    PricingFactory,
)


class TestRush(NoSocketsTestCase):
    def setUp(self):
        self.pilot = UserMainFactory(
            permissions__=[
                "freight.jf_pilot",
                "freight.view_contracts",
                "freight.use_calculator",
            ]
        )
        self.standard = PricingFactory(price_base=10_000_000)
        self.rush = PricingFactory(
            start_location=self.standard.start_location,
            end_location=self.standard.end_location,
            is_rush=True,
            price_base=30_000_000,
        )
        self.availability = PilotAvailability.objects.create(user=self.pilot)

    def online(self):
        self.availability.is_online = True
        self.availability.save()

    def test_distinct_pricing_for_same_route_is_valid(self):
        self.rush.full_clean()
        self.standard.full_clean()

    def test_offline_hides_rush_and_rejects_forged_submission(self):
        self.assertNotIn("rush", CalculatorForm().fields)
        form = CalculatorForm({"pricing": self.rush.pk, "rush": "on"})
        self.assertFalse(form.is_valid())

    def test_online_filters_routes_and_calculates_rush_price(self):
        self.online()
        form = CalculatorForm(
            {"pricing": self.rush.pk, "rush": "on", "volume": 1000, "collateral": 1000}
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.get_calculated_data(self.rush)[2], 30_000_000)
        self.assertEqual(list(form.fields["pricing"].queryset), [self.rush])
        self.assertEqual(
            list(CalculatorForm().fields["pricing"].queryset), [self.standard]
        )

    def test_inactive_pilot_does_not_enable_rush(self):
        self.online()
        self.pilot.is_active = False
        self.pilot.save()
        self.assertFalse(PilotAvailability.available())

    def test_availability_post_is_authorized_and_get_does_not_mutate(self):
        client = Client()
        client.force_login(self.pilot)
        url = reverse("freight:pilot_availability")
        self.assertEqual(client.get(url).status_code, 405)
        self.assertEqual(client.post(url, {"online": "1"}).status_code, 302)
        self.assertTrue(PilotAvailability.available())
        client.post(url, {"online": "0"})
        self.assertFalse(PilotAvailability.available())
        member = UserMainFactory(permissions__=["freight.view_contracts"])
        client.force_login(member)
        self.assertEqual(client.post(url, {"online": "1"}).status_code, 403)

    def test_description_selects_rush_pricing_even_while_offline(self):
        handler = ContractHandlerFactory(user=self.pilot)
        contract = ContractFactory(
            handler=handler,
            user=self.pilot,
            start_location=self.standard.start_location,
            end_location=self.standard.end_location,
            title="RUSH CONTRACT",
            reward=10_000_000,
        )
        Contract.objects.update_pricing()
        contract.refresh_from_db()
        self.assertEqual(contract.pricing, self.rush)
        self.assertTrue(contract.has_pricing_errors)
        contract.title = "https://janice.e-351.com/a/example"
        contract.save()
        Contract.objects.update_pricing()
        contract.refresh_from_db()
        self.assertEqual(contract.pricing, self.standard)

    @patch("freight.models.contracts.dhooks_lite.Webhook.execute")
    def test_rush_notification_pings_everyone_with_contract_info(self, execute):
        execute.return_value.status_ok = True
        contract = ContractFactory(
            handler=ContractHandlerFactory(user=self.pilot),
            user=self.pilot,
            title="RUSH CONTRACT",
        )
        contract.send_pilot_notification()
        self.assertIn("@everyone RUSH CONTRACT", execute.call_args.kwargs["content"])
        self.assertEqual(len(execute.call_args.kwargs["embeds"]), 1)

    def test_multiple_pilots_keep_rush_available_when_one_goes_offline(self):
        self.online()
        other = UserMainFactory(permissions__=["freight.jf_pilot"])
        PilotAvailability.objects.create(user=other, is_online=True)
        self.availability.is_online = False
        self.availability.save()
        self.assertTrue(PilotAvailability.available())

    def test_csrf_protection_rejects_untrusted_toggle(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.pilot)
        self.assertEqual(
            client.post(
                reverse("freight:pilot_availability"), {"online": "1"}
            ).status_code,
            403,
        )
        self.assertFalse(PilotAvailability.available())

    @patch("freight.forms.create_janice_appraisal")
    @patch("freight.views.JANICE_API_KEY", "test-key")
    def test_rendered_rush_contract_replaces_janice_description(self, appraisal):
        self.online()
        appraisal.return_value = {
            "effectivePrices": {"totalSellPrice": 1_000_000},
            "totalPackagedVolume": 1000,
            "code": "demo",
        }
        client = Client()
        client.force_login(self.pilot)
        response = client.post(
            reverse("freight:calculator"),
            {
                "pricing": self.rush.pk,
                "rush": "on",
                "janice_items": "Tritanium 1000",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-clipboard-text="RUSH CONTRACT"')
        self.assertNotContains(
            response, 'data-clipboard-text="https://janice.e-351.com/a/demo"'
        )
        self.assertContains(response, "30,000,000")

    def test_reverse_direction_matches_rush_only(self):
        handler = ContractHandlerFactory(user=self.pilot)
        contract = ContractFactory(
            handler=handler,
            user=self.pilot,
            start_location=self.standard.end_location,
            end_location=self.standard.start_location,
            title=" rush contract ",
        )
        Contract.objects.update_pricing()
        contract.refresh_from_db()
        self.assertEqual(contract.pricing, self.rush)
        self.rush.is_active = False
        with patch("freight.tasks.update_contracts_pricing.delay"):
            self.rush.save()
        Contract.objects.update_pricing()
        contract.refresh_from_db()
        self.assertIsNone(contract.pricing)

    def test_pilot_toggle_is_in_shared_header_on_calculator(self):
        self.online()
        client = Client()
        client.force_login(self.pilot)
        response = client.get(reverse("freight:calculator"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'aria-pressed="true"', count=1)
        self.assertContains(
            response, 'action="%s"' % reverse("freight:pilot_availability"), count=1
        )
        self.availability.is_online = False
        self.availability.save()
        response = client.get(reverse("freight:calculator"))
        self.assertContains(response, 'aria-pressed="false"', count=1)

    def test_ajax_toggle_returns_state_without_redirect(self):
        client = Client()
        client.force_login(self.pilot)
        response = client.post(
            reverse("freight:pilot_availability"),
            {"online": "1"},
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"is_online": True, "online_until": None, "rush_available": True},
        )
        self.assertNotIn("Location", response)
        response = client.post(
            reverse("freight:pilot_availability"),
            {"online": "0"},
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(
            response.json(),
            {"is_online": False, "online_until": None, "rush_available": False},
        )
        self.assertFalse(PilotAvailability.available())

    def test_non_javascript_toggle_returns_to_same_page_and_section(self):
        client = Client()
        client.force_login(self.pilot)
        next_url = reverse("freight:jf_planner") + "?max_volume=250000#tab_jf_planner"
        response = client.post(
            reverse("freight:pilot_availability"), {"online": "1", "next": next_url}
        )
        self.assertRedirects(response, next_url, fetch_redirect_response=False)
        response = client.post(
            reverse("freight:pilot_availability"),
            {"online": "0", "next": "https://untrusted.example/"},
        )
        self.assertRedirects(
            response,
            reverse("freight:contract_list_all"),
            fetch_redirect_response=False,
        )

    def test_rush_marker_is_exposed_in_contract_and_planner_rows(self):
        from django.contrib.auth.models import Permission
        from freight import constants

        self.pilot.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="freight", codename="basic_access"
            )
        )
        handler = ContractHandlerFactory(user=self.pilot)
        rush = ContractFactory(
            handler=handler,
            user=self.pilot,
            title="RUSH CONTRACT",
            volume=1000,
            collateral=1000,
        )
        standard = ContractFactory(
            handler=handler,
            user=self.pilot,
            title="Regular cargo",
            volume=1000,
            collateral=1000,
        )
        client = Client()
        client.force_login(self.pilot)
        for url in [
            reverse("freight:contract_list_data", args=[constants.CONTRACT_LIST_ALL]),
            reverse("freight:jf_planner_data"),
        ]:
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(response.status_code, 200)
                rows = {row["contract_id"]: row for row in response.json()["data"]}
                self.assertTrue(rows[rush.contract_id]["is_rush"])
                self.assertFalse(rows[standard.contract_id]["is_rush"])

    def test_pilot_going_offline_after_page_load_blocks_rush_calculation(self):
        self.online()
        client = Client()
        client.force_login(self.pilot)
        response = client.get(reverse("freight:calculator") + "?rush=1")
        self.assertContains(response, "Rush contract")
        self.availability.is_online = False
        self.availability.save()
        response = client.post(
            reverse("freight:calculator"),
            {
                "rush": "on",
                "pricing": self.rush.pk,
                "volume": 1000,
                "collateral": 1000,
            },
        )
        self.assertContains(response, "no JF pilot is online")
        self.assertContains(response, "Switch to standard routes")
        self.assertContains(response, "No price calculated yet")
        self.assertNotContains(response, 'data-clipboard-text="RUSH CONTRACT"')

    @patch("freight.forms.create_janice_appraisal")
    @patch("freight.views.JANICE_API_KEY", "test-key")
    def test_pilot_going_offline_during_appraisal_blocks_quote(self, appraisal):
        self.online()

        def finish_appraisal(*args):
            self.availability.is_online = False
            self.availability.save()
            return {
                "effectivePrices": {"totalSellPrice": 1_000_000},
                "totalPackagedVolume": 1000,
                "code": "demo",
            }

        appraisal.side_effect = finish_appraisal
        client = Client()
        client.force_login(self.pilot)
        response = client.post(
            reverse("freight:calculator"),
            {
                "rush": "on",
                "pricing": self.rush.pk,
                "janice_items": "Tritanium 1000",
            },
        )
        self.assertContains(response, "no JF pilot is online")
        self.assertContains(response, "No price calculated yet")
        self.assertNotContains(response, 'data-clipboard-text="RUSH CONTRACT"')
        self.assertNotContains(response, 'data-clipboard-text="30000000"')

    def test_offline_error_remains_visible_with_only_rush_routes_configured(self):
        self.online()
        self.standard.delete()
        client = Client()
        client.force_login(self.pilot)
        client.get(reverse("freight:calculator") + "?rush=1")
        self.availability.is_online = False
        self.availability.save()
        response = client.post(
            reverse("freight:calculator"),
            {
                "rush": "on",
                "pricing": self.rush.pk,
                "volume": 1000,
            },
        )
        self.assertContains(response, "no JF pilot is online")
        self.assertContains(response, "Switch to standard routes")
        self.assertContains(response, "No price calculated yet")

    def test_second_online_pilot_still_allows_quote(self):
        self.online()
        form = CalculatorForm(
            {"rush": "on", "pricing": self.rush.pk, "volume": 1000, "collateral": 1000}
        )
        self.assertTrue(form.is_valid(), form.errors)
        other = UserMainFactory(permissions__=["freight.jf_pilot"])
        PilotAvailability.objects.create(user=other, is_online=True)
        self.availability.is_online = False
        self.availability.save()
        self.assertEqual(form.get_calculated_data(self.rush)[2], 30_000_000)

    def test_standard_quote_is_allowed_while_pilots_are_offline(self):
        form = CalculatorForm(
            {"pricing": self.standard.pk, "volume": 1000, "collateral": 1000}
        )
        self.assertEqual(form.get_calculated_data(self.standard)[2], 10_000_000)
