from http import HTTPStatus
from unittest.mock import patch

import pook

from django.urls import reverse

from app_utils.testdata_factories import UserMainFactory
from app_utils.testing import NoSocketsTestCase

from freight.models import Location
from freight.tests.testdata.factories_2 import (
    PositionFactory,
    PricingFactory,
    make_esi_url,
)
from freight.views import ADD_LOCATION_TOKEN_TAG


class TestAddLocation(NoSocketsTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = UserMainFactory(
            main_character__scopes=[
                "esi-universe.read_structures.v1",
            ],
            permissions__=["freight.basic_access", "freight.add_location"],
        )

    @pook.on
    def test_can_add_location(self):
        # given
        location_id = 1_000_000_000_001
        pook.get(
            make_esi_url(f"universe/structures/{location_id}"),
            reply=HTTPStatus.OK,
            response_json={
                "owner_id": 1001,
                "name": "Alpha",
                "position": PositionFactory(),
                "solar_system_id": 2001,
                "type_id": 3001,
            },
        )
        self.client.force_login(self.user)
        token = self.user.token_set.first()
        session = self.client.session
        session[ADD_LOCATION_TOKEN_TAG] = token.pk
        session.save()

        # when
        response = self.client.post(
            reverse("freight:add_location_2"), data={"location_id": location_id}
        )

        # then
        self.assertTrue(Location.objects.filter(id=location_id).exists())

        self.assertEqual(response.status_code, HTTPStatus.FOUND)
        self.assertEqual(response.url, reverse("freight:add_location_2"))

    @pook.on
    def test_should_return_error_when_esi_request_failed(self):
        # given
        location_id = 1_000_000_000_001
        pook.get(
            make_esi_url(f"universe/structures/{location_id}"),
            reply=HTTPStatus.FORBIDDEN,
            response_json={"error": "some error"},
        )
        self.client.force_login(self.user)
        token = self.user.token_set.first()
        session = self.client.session
        session[ADD_LOCATION_TOKEN_TAG] = token.pk
        session.save()

        # when
        response = self.client.post(
            reverse("freight:add_location_2"), data={"location_id": location_id}
        )

        # then
        self.assertEqual(response.status_code, HTTPStatus.OK)

        messages = list(response.context["messages"])
        self.assertEqual(len(messages), 1)
        self.assertIn("error", messages[0].tags)


class TestCalculator_Page(NoSocketsTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = UserMainFactory(
            permissions__=[
                "freight.basic_access",
                "freight.use_calculator",
            ]
        )

    def test_can_open_page_with_pricing(self):
        # given
        self.client.force_login(self.user)
        PricingFactory()

        # when
        response = self.client.get(reverse("freight:calculator"))

        # then
        self.assertEqual(response.status_code, HTTPStatus.OK)

    def test_can_open_page_witout_pricing(self):
        # given
        self.client.force_login(self.user)

        # when
        response = self.client.get(reverse("freight:calculator"))

        # then
        self.assertEqual(response.status_code, HTTPStatus.OK)

    @patch("freight.views.JANICE_API_KEY", None)
    def test_should_hide_janice_fields_without_api_key(self):
        self.client.force_login(self.user)
        PricingFactory()

        response = self.client.get(reverse("freight:calculator"))

        self.assertNotIn("janice_items", response.context["form"].fields)
        self.assertNotIn("janice_extra_collateral", response.context["form"].fields)

    @patch("freight.views.JANICE_API_KEY", "secret")
    def test_should_show_janice_fields_with_api_key(self):
        self.client.force_login(self.user)
        PricingFactory()

        response = self.client.get(reverse("freight:calculator"))

        self.assertIn("janice_items", response.context["form"].fields)
        self.assertIn("janice_extra_collateral", response.context["form"].fields)


class TestCalculator_Form(NoSocketsTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = UserMainFactory(
            permissions__=[
                "freight.basic_access",
                "freight.use_calculator",
            ]
        )

    def test_can_calculate_simple_price(self):
        # given
        self.client.force_login(self.user)
        pricing = PricingFactory(price_base=40_000_000)
        form_data = {
            "pricing": pricing.pk,
        }

        # when
        response = self.client.post(
            reverse("freight:calculator"), data=form_data, follow=True
        )

        # then
        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertIsNone(response.context.get("errors"))
        self.assertEqual(response.context["price"], 40_000_000)

    def test_can_calculate_complex_price(self):
        # given
        self.client.force_login(self.user)
        pricing = PricingFactory(
            collateral_max=5_000_000_000,
            price_base=50_000_000,
            price_per_collateral_percent=2,
            price_per_volume=150,
            volume_max=320_000,
        )
        form_data = {
            "pricing": pricing.pk,
            "volume": 50_000,
            "collateral": 200_0000_000,
        }

        # when
        response = self.client.post(
            reverse("freight:calculator"), data=form_data, follow=True
        )

        # then
        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertIsNone(response.context.get("errors"))
        self.assertEqual(response.context["price"], 98_000_000)

    def test_should_show_error_when_volume_is_required_and_missing(self):
        # given
        self.client.force_login(self.user)
        pricing = PricingFactory(
            collateral_max=5_000_000_000,
            price_base=50_000_000,
            price_per_collateral_percent=2,
            price_per_volume=150,
            volume_max=320_000,
        )
        form_data = {
            "pricing": pricing.pk,
            "collateral": 200_0000_000,
        }

        # when
        response = self.client.post(
            reverse("freight:calculator"), data=form_data, follow=True
        )

        # then
        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertIsNotNone(response.context.get("errors"))
        self.assertIsNone(response.context.get("price"))

    def test_should_show_error_when_collaterial_is_required_and_missing(self):
        # given
        self.client.force_login(self.user)
        pricing = PricingFactory(
            collateral_max=5_000_000_000,
            price_base=50_000_000,
            price_per_collateral_percent=2,
            price_per_volume=150,
            volume_max=320_000,
        )
        form_data = {
            "pricing": pricing.pk,
            "volume": 50_000,
        }

        # when
        response = self.client.post(
            reverse("freight:calculator"), data=form_data, follow=True
        )

        # then
        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertIsNotNone(response.context.get("errors"))
        self.assertIsNone(response.context.get("price"))

    def test_should_show_error_when_collaterial_is_too_high(self):
        # given
        self.client.force_login(self.user)
        pricing = PricingFactory(
            collateral_max=5_000_000_000,
            price_base=50_000_000,
            price_per_collateral_percent=2,
            price_per_volume=150,
            volume_max=320_000,
        )
        form_data = {
            "pricing": pricing.pk,
            "volume": 50_000,
            "collateral": 5_000_000_001,
        }

        # when
        response = self.client.post(
            reverse("freight:calculator"), data=form_data, follow=True
        )

        # then
        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertIsNotNone(response.context.get("errors"))
        self.assertIsNone(response.context.get("price"))

    def test_should_show_error_when_volume_is_to_high(self):
        # given
        self.client.force_login(self.user)
        pricing = PricingFactory(
            collateral_max=5_000_000_000,
            price_base=50_000_000,
            price_per_collateral_percent=2,
            price_per_volume=150,
            volume_max=320_000,
        )
        form_data = {
            "pricing": pricing.pk,
            "volume": 320_001,
            "collateral": 200_0000_000,
        }

        # when
        response = self.client.post(
            reverse("freight:calculator"), data=form_data, follow=True
        )

        # then
        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertIsNotNone(response.context.get("errors"))
        self.assertIsNone(response.context.get("price"))

    @patch("freight.forms.create_janice_appraisal")
    @patch("freight.views.JANICE_API_KEY", "secret")
    def test_can_calculate_from_janice_appraisal(self, mock_create_janice_appraisal):
        self.client.force_login(self.user)
        pricing = PricingFactory(
            price_base=40_000_000,
            price_per_collateral_percent=1,
            price_per_volume=100,
        )
        mock_create_janice_appraisal.return_value = {
            "code": "XpAZ4B",
            "failures": "",
            "totalVolume": 85,
            "effectivePrices": {"totalSplitPrice": 17_397_750},
        }
        form_data = {
            "pricing": pricing.pk,
            "janice_items": (
                "Light Neutron Blaster II\t5\n"
                "Torpedo Launcher II\t2\n"
                "1400mm Howitzer Artillery II\t"
            ),
            "janice_extra_collateral": 1_000_000,
        }

        response = self.client.post(
            reverse("freight:calculator"), data=form_data, follow=True
        )

        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertIsNone(response.context.get("errors"))
        self.assertEqual(response.context["volume"], 85)
        self.assertEqual(response.context["collateral"], 18_397_750)
        self.assertEqual(response.context["price"], 41_000_000)
        self.assertEqual(
            response.context["janice_appraisal_url"],
            "https://janice.e-351.com/a/XpAZ4B",
        )
        mock_create_janice_appraisal.assert_called_once_with(
            "secret", form_data["janice_items"]
        )
