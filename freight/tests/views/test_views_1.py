import datetime as dt
from http import HTTPStatus
from unittest.mock import patch

from django.test import RequestFactory, tag
from django.urls import reverse
from django.utils.timezone import now

from app_utils.testdata_factories import (
    EveAllianceInfoFactory,
    EveCharacterFactory,
    EveCorporationInfoFactory,
    UserFactory,
    UserMainFactory,
)
from app_utils.testing import NoSocketsTestCase, json_response_to_python

from freight import constants, views
from freight.app_settings import FREIGHT_OPERATION_MODE_MY_ALLIANCE
from freight.models import Contract, ContractHandler
from freight.tests.testdata.factories_2 import (
    ContractFactory,
    ContractHandlerFactory,
    LocationStationFactory,
    PricingFactory,
    UserMainDefaultFactory,
)

MODULE_PATH = "freight.views"


def json_response_to_python_dict(response) -> dict:
    return {x["id"]: x for x in json_response_to_python(response)["data"]}


class TestCalculator(NoSocketsTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = UserMainDefaultFactory()
        cls.factory = RequestFactory()

    def test_index(self):
        # given
        ContractHandlerFactory(user=self.user)
        request = self.factory.get(reverse("freight:index"))
        request.user = self.user

        # when
        response = views.index(request)

        # then
        self.assertEqual(response.status_code, HTTPStatus.FOUND)
        self.assertEqual(response.url, reverse("freight:calculator"))

    def test_calculator_access_with_permission(self):
        # given
        ContractHandlerFactory(user=self.user)
        request = self.factory.get(reverse("freight:calculator"))
        request.user = self.user

        # when
        response = views.calculator(request)

        # then
        self.assertEqual(response.status_code, HTTPStatus.OK)

    def test_calculator_no_access_without_permission(self):
        # given
        ContractHandlerFactory(user=self.user)
        request = self.factory.get(reverse("freight:calculator"))
        request.user = UserMainFactory()
        # when
        response = views.calculator(request)

        # then
        self.assertNotEqual(response.status_code, HTTPStatus.OK)

    def test_can_render_calculator_without_handler(self):
        # given
        request = self.factory.get(reverse("freight:calculator"))
        request.user = self.user
        # when
        response = views.calculator(request)
        # then
        self.assertEqual(response.status_code, HTTPStatus.OK)


class TestRouteDashboard(NoSocketsTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.factory = RequestFactory()
        cls.user = UserMainFactory(
            permissions__=["freight.basic_access", "freight.use_calculator"]
        )
        cls.handler = ContractHandlerFactory(user=cls.user)

    def test_route_dashboard_access_with_calculator_permission(self):
        request = self.factory.get(reverse("freight:route_dashboard"))
        request.user = self.user

        response = views.route_dashboard(request)

        self.assertEqual(response.status_code, HTTPStatus.OK)

    def test_route_dashboard_no_access_without_calculator_permission(self):
        user = UserMainFactory(permissions__=["freight.basic_access"])
        request = self.factory.get(reverse("freight:route_dashboard"))
        request.user = user

        response = views.route_dashboard(request)

        self.assertNotEqual(response.status_code, HTTPStatus.OK)

    def test_route_dashboard_totals_only_include_configured_routes(self):
        pricing = PricingFactory(
            start_location=LocationStationFactory(name="Jita"),
            end_location=LocationStationFactory(name="Amamake"),
        )
        issued_at = now() - dt.timedelta(days=1)
        ContractFactory(
            date_completed=issued_at + dt.timedelta(hours=6),
            date_issued=issued_at,
            finished=True,
            handler=self.handler,
            pricing=pricing,
            volume=100,
        )
        ContractFactory(
            date_completed=issued_at + dt.timedelta(hours=2),
            date_issued=issued_at,
            finished=True,
            handler=self.handler,
            pricing=pricing,
            volume=200,
        )
        ContractFactory(
            date_completed=issued_at + dt.timedelta(hours=8),
            date_issued=issued_at,
            handler=self.handler,
            pricing=pricing,
            status=Contract.Status.FINISHED_ISSUER,
            volume=300,
        )
        ContractFactory(
            date_completed=issued_at + dt.timedelta(hours=12),
            date_issued=issued_at,
            handler=self.handler,
            pricing=pricing,
            status=Contract.Status.FINISHED_CONTRACTOR,
            volume=400,
        )
        ContractFactory(
            handler=self.handler,
            pricing=pricing,
            volume=50,
        )
        ContractFactory(
            failed=True,
            handler=self.handler,
            pricing=pricing,
            volume=500,
        )
        ContractFactory(
            canceled=True,
            handler=self.handler,
            pricing=pricing,
            volume=700,
        )
        ContractFactory(
            handler=self.handler,
            pricing=pricing,
            status=Contract.Status.REJECTED,
            volume=800,
        )
        ContractFactory(
            handler=self.handler,
            pricing=pricing,
            status=Contract.Status.DELETED,
            volume=900,
        )
        ContractFactory(
            handler=self.handler,
            pricing=pricing,
            status=Contract.Status.REVERSED,
            volume=1000,
        )
        ContractFactory(
            handler=self.handler,
            volume=999,
        )

        rows, summary = views._route_dashboard_totals()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["route"], "Jita <-> Amamake")
        self.assertEqual(rows[0]["contracts"], 5)
        self.assertEqual(rows[0]["volume"], 1050)
        self.assertEqual(rows[0]["delivered_contracts"], 4)
        self.assertEqual(rows[0]["to_deliver_contracts"], 1)
        self.assertEqual(rows[0]["to_deliver_volume"], 50)
        self.assertEqual(rows[0]["average_delivery_time"], "7h")
        self.assertEqual(summary["routes"], 1)
        self.assertEqual(summary["contracts"], 5)
        self.assertEqual(summary["volume"], 1050)
        self.assertEqual(summary["to_deliver_contracts"], 1)
        self.assertEqual(summary["to_deliver_volume"], 50)
        self.assertEqual(summary["average_delivery_time"], "7h")


class TestContractListData_Access(NoSocketsTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.factory = RequestFactory()
        cls.user_1 = UserMainFactory(
            permissions__=[
                "freight.basic_access",
                "freight.view_contracts",
                "freight.use_calculator",
            ]
        )
        cls.handler = ContractHandlerFactory(user=cls.user_1)
        cls.user_2 = UserMainFactory(
            main_character__character=EveCharacterFactory(
                corporation=cls.user_1.profile.main_character.corporation
            ),
            permissions__=["freight.basic_access"],
        )

    def test_should_open_all_contracts_page(self):
        # given
        request = self.factory.get(reverse("freight:contract_list_all"))
        request.user = self.user_1
        # when
        response = views.contract_list_all(request)
        # then
        self.assertEqual(response.status_code, HTTPStatus.OK)

    def test_all_no_access_without_permission(self):
        request = self.factory.get(reverse("freight:contract_list_all"))
        request.user = self.user_2
        response = views.contract_list_all(request)
        self.assertNotEqual(response.status_code, HTTPStatus.OK)

    def test_jf_planner_access_with_permission(self):
        request = self.factory.get(reverse("freight:jf_planner"))
        request.user = self.user_1
        response = views.jf_planner(request)
        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertContains(response, 'id="jf_contract_check_warning"')
        self.assertContains(
            response,
            "This plan contains at least one potentially incorrect contract.",
        )

    def test_jf_planner_no_access_without_permission(self):
        request = self.factory.get(reverse("freight:jf_planner"))
        request.user = self.user_2
        response = views.jf_planner(request)
        self.assertNotEqual(response.status_code, HTTPStatus.OK)

    # TODO
    """ issue with setting permission
    def test_active_access_with_permission(self):
        request = self.factory.get(reverse('freight:contract_list_active'))
        request.user = self.user_1

        response = views.contract_list_active(request)
        self.assertEqual(response.status_code, HTTPStatus.OK)
    """

    def test_user_no_access_without_permission(self):
        request = self.factory.get(reverse("freight:contract_list_user"))
        request.user = self.user_2
        response = views.contract_list_user(request)
        self.assertNotEqual(response.status_code, HTTPStatus.OK)

    def test_user_access_with_permission(self):
        request = self.factory.get(reverse("freight:contract_list_user"))
        request.user = self.user_1

        response = views.contract_list_user(request)
        self.assertEqual(response.status_code, HTTPStatus.OK)

    def test_user_access_with_permission_and_no_main(self):
        # given
        user = UserFactory(
            permissions__=["freight.basic_access", "freight.use_calculator"]
        )
        request = self.factory.get(reverse("freight:contract_list_user"))
        request.user = user
        # when
        response = views.contract_list_user(request)
        # then
        self.assertEqual(response.status_code, HTTPStatus.OK)

    def test_data_user_no_access_without_permission_1(self):
        request = self.factory.get(
            reverse("freight:contract_list_data", args={constants.CONTRACT_LIST_USER})
        )
        request.user = self.user_2
        response = views.contract_list_data(request, constants.CONTRACT_LIST_USER)
        data = json_response_to_python(response)["data"]
        self.assertListEqual(data, [])

    def test_data_user_no_access_without_permission_2(self):
        request = self.factory.get(
            reverse("freight:contract_list_data", args={constants.CONTRACT_LIST_ACTIVE})
        )
        request.user = self.user_2
        response = views.contract_list_data(request, constants.CONTRACT_LIST_ACTIVE)
        data = json_response_to_python(response)["data"]
        self.assertListEqual(data, [])

    def test_data_user_no_access_without_permission_3(self):
        request = self.factory.get(
            reverse("freight:contract_list_data", args={constants.CONTRACT_LIST_ALL})
        )
        request.user = self.user_2
        response = views.contract_list_data(request, constants.CONTRACT_LIST_ALL)
        data = json_response_to_python(response)["data"]
        self.assertListEqual(data, [])


class TestContractListData_Categories(NoSocketsTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.factory = RequestFactory()
        cls.user_1 = UserMainFactory(
            permissions__=[
                "freight.basic_access",
                "freight.view_contracts",
                "freight.use_calculator",
            ]
        )
        cls.handler = ContractHandlerFactory(user=cls.user_1)
        cls.contract_accepted = ContractFactory(handler=cls.handler, accepted=True)
        cls.contract_canceled = ContractFactory(handler=cls.handler, canceled=True)
        cls.contract_failed = ContractFactory(handler=cls.handler, failed=True)
        cls.contract_finished = ContractFactory(handler=cls.handler, finished=True)
        cls.contract_outstanding = ContractFactory(handler=cls.handler)
        cls.user_2 = UserMainFactory(
            permissions__=["freight.basic_access", "freight.use_calculator"]
        )
        cls.contract_accepted_user = ContractFactory(
            handler=cls.handler, user=cls.user_2, accepted=True
        )
        cls.contract_canceled_user = ContractFactory(
            handler=cls.handler, user=cls.user_2, canceled=True
        )
        cls.contract_failed_user = ContractFactory(
            handler=cls.handler, user=cls.user_2, failed=True
        )
        cls.contract_finished_user = ContractFactory(
            handler=cls.handler, user=cls.user_2, finished=True
        )
        cls.contract_outstanding_user = ContractFactory(
            handler=cls.handler, user=cls.user_2
        )

    def test_should_return_all_contracts(self):
        # given
        request = self.factory.get(
            reverse("freight:contract_list_data", args={constants.CONTRACT_LIST_ALL})
        )
        request.user = self.user_1

        # when
        response = views.contract_list_data(request, constants.CONTRACT_LIST_ALL)

        # then
        got = {obj["contract_id"] for obj in json_response_to_python(response)["data"]}
        want = {
            self.contract_outstanding.contract_id,
            self.contract_accepted.contract_id,
            self.contract_finished.contract_id,
            self.contract_failed.contract_id,
            self.contract_canceled.contract_id,
            self.contract_outstanding_user.contract_id,
            self.contract_accepted_user.contract_id,
            self.contract_finished_user.contract_id,
            self.contract_failed_user.contract_id,
            self.contract_canceled_user.contract_id,
        }
        self.assertSetEqual(got, want)

    def test_should_return_active_contracts(self):
        # given
        request = self.factory.get(
            reverse("freight:contract_list_data", args={constants.CONTRACT_LIST_ACTIVE})
        )
        request.user = self.user_1

        # when
        response = views.contract_list_data(request, constants.CONTRACT_LIST_ACTIVE)

        # then
        # then
        got = {obj["contract_id"] for obj in json_response_to_python(response)["data"]}
        want = {
            self.contract_outstanding.contract_id,
            self.contract_accepted.contract_id,
            self.contract_outstanding_user.contract_id,
            self.contract_accepted_user.contract_id,
        }
        self.assertSetEqual(got, want)

    def test_should_return_contracts_issued_by_user(self):
        # given
        request = self.factory.get(
            reverse("freight:contract_list_data", args={constants.CONTRACT_LIST_USER})
        )
        request.user = self.user_2

        # when
        response = views.contract_list_data(request, constants.CONTRACT_LIST_USER)

        # then
        self.assertEqual(response.status_code, HTTPStatus.OK)
        got = {obj["contract_id"] for obj in json_response_to_python(response)["data"]}
        want = {
            self.contract_outstanding_user.contract_id,
            self.contract_accepted_user.contract_id,
            self.contract_finished_user.contract_id,
            self.contract_failed_user.contract_id,
        }
        self.assertSetEqual(got, want)

    def test_should_not_return_user_contracts_when_user_is_missing_permission(self):
        # given
        user = UserMainFactory(permissions__=["freight.basic_access"])
        ContractFactory(handler=self.handler, user=user)
        request = self.factory.get(
            reverse("freight:contract_list_data", args={constants.CONTRACT_LIST_USER})
        )
        request.user = user

        # when
        response = views.contract_list_data(request, constants.CONTRACT_LIST_USER)

        # then
        self.assertEqual(response.status_code, HTTPStatus.OK)
        got = {obj["contract_id"] for obj in json_response_to_python(response)["data"]}
        self.assertFalse(got)

    def test_data_invalid_category(self):
        request = self.factory.get(
            reverse("freight:contract_list_data", args={"this_is_not_valid"})
        )
        request.user = self.user_1

        with self.assertRaises(ValueError):
            views.contract_list_data(request, "this_is_not_valid")


class TestContractListData_ResponseDetails(NoSocketsTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.factory = RequestFactory()
        cls.user = UserMainFactory(
            permissions__=["freight.basic_access", "freight.view_contracts"]
        )
        cls.handler = ContractHandlerFactory(user=cls.user)

    def test_should_return_contract(self):
        # given
        contract = ContractFactory(handler=self.handler, accepted=True)
        request = self.factory.get(
            reverse("freight:contract_list_data", args={constants.CONTRACT_LIST_ALL})
        )
        request.user = self.user

        # when
        response = views.contract_list_data(request, constants.CONTRACT_LIST_ALL)

        # then
        self.assertEqual(response.status_code, HTTPStatus.OK)
        data = json_response_to_python(response)["data"]
        obj = data[0]
        self.assertEqual(obj["contract_id"], contract.contract_id)
        self.assertEqual(obj["status"], "in_progress")


class TestJfPlannerData(NoSocketsTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.factory = RequestFactory()
        cls.user = UserMainFactory(
            permissions__=["freight.basic_access", "freight.view_contracts"]
        )
        cls.handler = ContractHandlerFactory(user=cls.user)

    def test_should_arrange_active_contracts_into_jf_loads(self):
        start_location = LocationStationFactory()
        end_location = LocationStationFactory()
        ContractFactory(
            collateral=500,
            end_location=end_location,
            handler=self.handler,
            reward=100,
            start_location=start_location,
            volume=200,
        )
        ContractFactory(
            collateral=500,
            end_location=end_location,
            handler=self.handler,
            reward=100,
            start_location=start_location,
            volume=100,
        )
        ContractFactory(
            collateral=500,
            end_location=end_location,
            handler=self.handler,
            reward=100,
            start_location=start_location,
            volume=200,
        )
        ContractFactory(
            collateral=500,
            finished=True,
            handler=self.handler,
            reward=100,
            volume=100,
        )

        request = self.factory.get(
            reverse("freight:jf_planner_data"),
            {"max_volume": "300", "max_collateral": "1000"},
        )
        request.user = self.user

        response = views.jf_planner_data(request)

        self.assertEqual(response.status_code, HTTPStatus.OK)
        response_data = json_response_to_python(response)
        data = response_data["data"]
        self.assertEqual(len(data), 3)
        self.assertEqual({row["load_index"] for row in data}, {1, 2})
        self.assertTrue(all(not row["over_limit"] for row in data))
        self.assertFalse(response_data["has_contract_check_errors"])
        summary = response_data["summary"]
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["jf_loads"], 2)
        self.assertEqual(summary[0]["reward"], 300)

    def test_should_warn_about_failed_contract_check(self):
        pricing = PricingFactory(update_contracts=False)
        contract = ContractFactory(
            collateral=500,
            handler=self.handler,
            issues='["Reward is too low"]',
            pricing=pricing,
            reward=100,
            volume=100,
        )
        request = self.factory.get(reverse("freight:jf_planner_data"))
        request.user = self.user

        response = views.jf_planner_data(request)

        self.assertEqual(response.status_code, HTTPStatus.OK)
        response_data = json_response_to_python(response)
        self.assertTrue(response_data["has_contract_check_errors"])
        row = response_data["data"][0]
        self.assertEqual(row["contract_id"], contract.contract_id)
        self.assertTrue(row["has_pricing_errors"])
        self.assertIn("fa-exclamation-triangle", row["pricing_check"])
        self.assertIn("Reward is too low", row["pricing_check"])

    def test_should_default_jf_planner_to_outstanding_contracts(self):
        outstanding_contract = ContractFactory(
            collateral=500,
            handler=self.handler,
            reward=100,
            volume=100,
        )
        ContractFactory(
            accepted=True,
            collateral=500,
            handler=self.handler,
            reward=100,
            volume=100,
        )

        request = self.factory.get(reverse("freight:jf_planner_data"))
        request.user = self.user

        response = views.jf_planner_data(request)

        self.assertEqual(response.status_code, HTTPStatus.OK)
        data = json_response_to_python(response)["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["contract_id"], outstanding_contract.contract_id)
        self.assertEqual(data[0]["status"], Contract.Status.OUTSTANDING)

    def test_should_allow_in_progress_jf_planner_status(self):
        ContractFactory(
            collateral=500,
            handler=self.handler,
            reward=100,
            volume=100,
        )
        in_progress_contract = ContractFactory(
            accepted=True,
            collateral=500,
            handler=self.handler,
            reward=100,
            volume=100,
        )

        request = self.factory.get(
            reverse("freight:jf_planner_data"),
            {"statuses": [Contract.Status.IN_PROGRESS]},
        )
        request.user = self.user

        response = views.jf_planner_data(request)

        self.assertEqual(response.status_code, HTTPStatus.OK)
        data = json_response_to_python(response)["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["contract_id"], in_progress_contract.contract_id)
        self.assertEqual(data[0]["status"], Contract.Status.IN_PROGRESS)

    def test_should_not_mix_routes_in_same_jf_load(self):
        route_1_start = LocationStationFactory(name="3-FKCZ - Station")
        route_1_end = LocationStationFactory(name="SVM-3K - Station")
        route_2_start = LocationStationFactory(name="1DH-SX - Station")
        route_2_end = LocationStationFactory(name="Jita - Station")
        ContractFactory(
            collateral=500,
            end_location=route_1_end,
            handler=self.handler,
            reward=100,
            start_location=route_1_start,
            volume=200,
        )
        ContractFactory(
            collateral=500,
            end_location=route_2_end,
            handler=self.handler,
            reward=100,
            start_location=route_2_start,
            volume=100,
        )

        request = self.factory.get(
            reverse("freight:jf_planner_data"),
            {"max_volume": "300", "max_collateral": "1000"},
        )
        request.user = self.user

        response = views.jf_planner_data(request)

        self.assertEqual(response.status_code, HTTPStatus.OK)
        data = json_response_to_python(response)["data"]
        self.assertEqual(len(data), 2)
        self.assertEqual({row["load_index"] for row in data}, {1, 2})
        self.assertNotEqual(data[0]["route"], data[1]["route"])
        summary = json_response_to_python(response)["summary"]
        self.assertEqual(len(summary), 2)
        self.assertEqual({row["jf_loads"] for row in summary}, {1})
        self.assertEqual(sum(row["reward"] for row in summary), 200)

    def test_should_mix_selected_routes_and_store_the_rest(self):
        route_1_start = LocationStationFactory(name="A - Station")
        route_1_end = LocationStationFactory(name="D - Station")
        route_2_start = LocationStationFactory(name="A - Station")
        route_2_end = LocationStationFactory(name="B - Station")
        route_3_start = LocationStationFactory(name="C - Station")
        route_3_end = LocationStationFactory(name="E - Station")
        convoy_contract_1 = ContractFactory(
            collateral=500,
            end_location=route_1_end,
            handler=self.handler,
            reward=100,
            start_location=route_1_start,
            volume=200,
        )
        convoy_contract_2 = ContractFactory(
            collateral=500,
            end_location=route_2_end,
            handler=self.handler,
            reward=100,
            start_location=route_2_start,
            volume=100,
        )
        storage_contract = ContractFactory(
            collateral=500,
            end_location=route_3_end,
            handler=self.handler,
            reward=100,
            start_location=route_3_start,
            volume=100,
        )

        request = self.factory.get(
            reverse("freight:jf_planner_data"),
            {
                "max_volume": "300",
                "max_collateral": "1000",
                "convoy_routes": [
                    views._jf_route_key_value(convoy_contract_1),
                    views._jf_route_key_value(convoy_contract_2),
                ],
            },
        )
        request.user = self.user

        response = views.jf_planner_data(request)

        self.assertEqual(response.status_code, HTTPStatus.OK)
        data = json_response_to_python(response)["data"]
        convoy_rows = [row for row in data if not row["storage"]]
        storage_rows = [row for row in data if row["storage"]]
        self.assertEqual(
            {row["contract_id"] for row in convoy_rows},
            {convoy_contract_1.contract_id, convoy_contract_2.contract_id},
        )
        self.assertEqual({row["load_index"] for row in convoy_rows}, {1})
        self.assertEqual(len(storage_rows), 1)
        self.assertEqual(storage_rows[0]["jf_load"], "Storage")
        self.assertEqual(storage_rows[0]["contract_id"], storage_contract.contract_id)
        summary = json_response_to_python(response)["summary"]
        self.assertEqual(summary, [{"route": "Convoy", "jf_loads": 1, "reward": 200}])

    def test_should_show_contracts_that_exceed_jf_limits(self):
        contract = ContractFactory(
            collateral=500,
            handler=self.handler,
            reward=100,
            volume=400,
        )

        request = self.factory.get(
            reverse("freight:jf_planner_data"),
            {"max_volume": "300", "max_collateral": "1000"},
        )
        request.user = self.user

        response = views.jf_planner_data(request)

        self.assertEqual(response.status_code, HTTPStatus.OK)
        data = json_response_to_python(response)["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["contract_id"], contract.contract_id)
        self.assertTrue(data[0]["over_limit"])


@patch(MODULE_PATH + ".messages", autospec=True)
@patch(MODULE_PATH + ".tasks.run_contracts_sync", autospec=True)
class TestSetupContractHandler(NoSocketsTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.factory = RequestFactory()

    def test_can_setup_for_alliance(self, mock_run_contracts_sync, mock_message):
        # given
        user = UserMainFactory(
            permissions__=[
                "freight.basic_access",
                "freight.setup_contract_handler",
            ]
        )
        token = user.token_set.first()
        token.character_id = user.profile.main_character.character_id
        request = self.factory.post(
            reverse("freight:setup_contract_handler"), data={"_token": token.pk}
        )
        request.user = user
        request.token = token

        # when
        with patch(
            MODULE_PATH + ".FREIGHT_OPERATION_MODE", FREIGHT_OPERATION_MODE_MY_ALLIANCE
        ):
            orig_view = views.setup_contract_handler.__wrapped__.__wrapped__.__wrapped__

        # then
        response = orig_view(request, token)
        self.assertEqual(mock_run_contracts_sync.delay.call_count, 1)
        self.assertEqual(response.status_code, HTTPStatus.FOUND)
        self.assertEqual(response.url, reverse("freight:index"))

    @tag("tox_disabled")  # TODO: Make test work in tox
    def test_should_return_error_when_user_is_not_in_an_alliance(
        self, mock_run_contracts_sync, mock_message
    ):
        # given
        user = UserMainFactory(
            permissions__=[
                "freight.basic_access",
                "freight.setup_contract_handler",
            ],
            main_character__character=EveCharacterFactory(
                corporation__create_alliance=False
            ),
        )
        token = user.token_set.first()
        token.character_id = user.profile.main_character.character_id
        request = self.factory.post(
            reverse("freight:setup_contract_handler"), data={"_token": token.pk}
        )
        request.user = user
        request.token = token

        # when
        with patch(
            MODULE_PATH + ".FREIGHT_OPERATION_MODE", FREIGHT_OPERATION_MODE_MY_ALLIANCE
        ):
            orig_view = views.setup_contract_handler.__wrapped__.__wrapped__.__wrapped__

        # then
        response = orig_view(request, token)
        self.assertEqual(mock_message.error.call_count, 1)
        self.assertEqual(mock_run_contracts_sync.delay.call_count, 0)
        self.assertEqual(response.status_code, HTTPStatus.FOUND)
        self.assertEqual(response.url, reverse("freight:index"))

    def test_should_show_error_when_existing_handler_has_different_operation_mode(
        self, mock_run_contracts_sync, mock_message
    ):
        # given
        user = UserMainFactory(
            permissions__=[
                "freight.basic_access",
                "freight.setup_contract_handler",
            ]
        )
        ContractHandlerFactory(
            user=user, operation_mode=ContractHandler.Mode.MY_CORPORATION
        )
        token = user.token_set.first()
        token.character_id = user.profile.main_character.character_id
        request = self.factory.post(
            reverse("freight:setup_contract_handler"), data={"_token": token.pk}
        )
        request.user = user
        request.token = token

        # when
        with patch(
            MODULE_PATH + ".FREIGHT_OPERATION_MODE", FREIGHT_OPERATION_MODE_MY_ALLIANCE
        ):
            orig_view = views.setup_contract_handler.__wrapped__.__wrapped__.__wrapped__

        # then
        response = orig_view(request, token)
        self.assertEqual(mock_message.error.call_count, 1)
        self.assertEqual(response.status_code, HTTPStatus.FOUND)
        self.assertEqual(response.url, reverse("freight:index"))
        self.assertEqual(mock_run_contracts_sync.delay.call_count, 0)


class TestStatistics(NoSocketsTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.factory = RequestFactory()
        cls.user = UserMainFactory(
            permissions__=["freight.basic_access", "freight.view_statistics"]
        )
        cls.handler = ContractHandlerFactory(user=cls.user)

    def test_should_open_statistics_page(self):
        # given
        request = self.factory.get(reverse("freight:statistics"))
        request.user = self.user

        # when
        response = views.statistics(request)

        # then
        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertContains(response, 'id="statistics_export_csv"')
        self.assertContains(response, "Export CSV")

    @patch(MODULE_PATH + ".FREIGHT_STATISTICS_MAX_DAYS", 42)
    def test_should_use_default_statistics_days(self):
        # given
        request = self.factory.get(reverse("freight:statistics"))
        request.user = self.user

        # when
        response = views.statistics(request)

        # then
        self.assertContains(response, 'value="42"')

    def test_statistics_routes_data_uses_selected_days(self):
        # given
        pricing = PricingFactory(
            start_location=LocationStationFactory(name="Jita"),
            end_location=LocationStationFactory(name="Amamake"),
        )
        ContractFactory(
            collateral=2_000_000_000,
            date_completed=now() - dt.timedelta(days=3),
            date_issued=now() - dt.timedelta(days=3, hours=6),
            finished=True,
            handler=self.handler,
            pricing=pricing,
            reward=200_000_000,
            volume=200_000,
        )
        ContractFactory(
            collateral=1_000_000_000,
            date_completed=now() - dt.timedelta(days=10),
            date_issued=now() - dt.timedelta(days=10, hours=6),
            finished=True,
            handler=self.handler,
            pricing=pricing,
            reward=100_000_000,
            volume=100_000,
        )

        request = self.factory.get(reverse("freight:statistics_routes_data"), {"days": 7})
        request.user = self.user

        # when
        response = views.statistics_routes_data(request)

        # then
        data: list = json_response_to_python(response)["data"]
        self.assertEqual(len(data), 1)
        obj = data.pop()
        self.assertEqual(obj["collaterals"], 2_000_000_000)
        self.assertEqual(obj["contracts"], 1)
        self.assertEqual(obj["rewards"], 200_000_000)
        self.assertEqual(obj["volume"], 200_000)

    def test_statistics_routes_data(self):
        # given
        pricing = PricingFactory(
            start_location=LocationStationFactory(name="Jita"),
            end_location=LocationStationFactory(name="Amamake"),
        )
        pilot = EveCharacterFactory()
        ContractFactory(
            acceptor=pilot,
            collateral=2_000_000_000,
            finished=True,
            handler=self.handler,
            pricing=pricing,
            reward=200_000_000,
            volume=200_000,
        )
        ContractFactory(
            acceptor=pilot,
            collateral=1_000_000_000,
            finished=True,
            handler=self.handler,
            pricing=pricing,
            reward=1_00_000_000,
            volume=100_000,
        )
        ContractFactory(
            collateral=1_000_000_000,
            handler=self.handler,
            pricing=pricing,
            reward=1_00_000_000,
            volume=100_000,
        )  # should be ignored
        ContractFactory(
            collateral=1_000_000_000,
            handler=self.handler,
            pricing=pricing,
            reward=1_00_000_000,
            volume=100_000,
            failed=True,
        )  # should be ignored
        ContractFactory(
            collateral=1_000_000_000,
            handler=self.handler,
            pricing=pricing,
            reward=1_00_000_000,
            volume=100_000,
            canceled=True,
        )  # should be ignored

        request = self.factory.get(reverse("freight:statistics_routes_data"))
        request.user = self.user

        # when
        response = views.statistics_routes_data(request)

        # then
        self.assertEqual(response.status_code, HTTPStatus.OK)

        data: list = json_response_to_python(response)["data"]
        self.assertEqual(len(data), 1)
        obj = data.pop()
        self.assertEqual(obj["collaterals"], 3_000_000_000)
        self.assertEqual(obj["contracts"], 2)
        self.assertEqual(obj["customers"], 2)
        self.assertEqual(obj["name"], "Jita <-> Amamake")
        self.assertEqual(obj["pilots"], 1)
        self.assertEqual(obj["rewards"], 300_000_000)
        self.assertEqual(obj["volume"], 300_000)

    def test_statistics_pilots_data(self):
        # given
        pricing = PricingFactory(
            start_location=LocationStationFactory(name="Jita"),
            end_location=LocationStationFactory(name="Amamake"),
        )
        pilot = EveCharacterFactory(
            character_name="Bruce Wayne", corporation__corporation_name="Wayne Foods"
        )
        ContractFactory(
            acceptor=pilot,
            collateral=2_000_000_000,
            finished=True,
            handler=self.handler,
            pricing=pricing,
            reward=200_000_000,
            volume=200_000,
        )
        ContractFactory(
            acceptor=pilot,
            collateral=1_000_000_000,
            finished=True,
            handler=self.handler,
            pricing=pricing,
            reward=1_00_000_000,
            volume=100_000,
        )
        ContractFactory(
            collateral=1_000_000_000,
            handler=self.handler,
            pricing=pricing,
            reward=1_00_000_000,
            volume=100_000,
        )  # should be ignored
        ContractFactory(
            collateral=1_000_000_000,
            handler=self.handler,
            pricing=pricing,
            reward=1_00_000_000,
            volume=100_000,
            failed=True,
        )  # should be ignored
        ContractFactory(
            collateral=1_000_000_000,
            handler=self.handler,
            pricing=pricing,
            reward=1_00_000_000,
            volume=100_000,
            canceled=True,
        )  # should be ignored

        request = self.factory.get(reverse("freight:statistics_pilots_data"))
        request.user = self.user

        # when
        response = views.statistics_pilots_data(request)

        # then
        self.assertEqual(response.status_code, HTTPStatus.OK)

        data: list = json_response_to_python(response)["data"]
        self.assertEqual(len(data), 1)
        obj = data.pop()
        self.assertEqual(obj["collaterals"], 3_000_000_000)
        self.assertEqual(obj["contracts"], 2)
        self.assertEqual(obj["name"], "Bruce Wayne")
        self.assertEqual(obj["corporation"], "Wayne Foods")
        self.assertEqual(obj["rewards"], 300_000_000)
        self.assertEqual(obj["volume"], 300_000)

    def test_statistics_pilot_corporations_data(self):
        # given
        pricing = PricingFactory(
            start_location=LocationStationFactory(name="Jita"),
            end_location=LocationStationFactory(name="Amamake"),
        )
        corporation_name = "Wayne Foods"
        alliance_name = "Wayne Inc."
        pilot = EveCharacterFactory(
            corporation=EveCorporationInfoFactory(
                corporation_name=corporation_name,
                alliance=EveAllianceInfoFactory(alliance_name=alliance_name),
            ),
        )
        ContractFactory(
            acceptor=pilot,
            collateral=2_000_000_000,
            finished=True,
            handler=self.handler,
            pricing=pricing,
            reward=200_000_000,
            volume=200_000,
        )
        ContractFactory(
            acceptor=pilot,
            collateral=1_000_000_000,
            finished=True,
            handler=self.handler,
            pricing=pricing,
            reward=1_00_000_000,
            volume=100_000,
        )
        ContractFactory(
            collateral=1_000_000_000,
            handler=self.handler,
            pricing=pricing,
            reward=1_00_000_000,
            volume=100_000,
        )  # should be ignored
        ContractFactory(
            collateral=1_000_000_000,
            handler=self.handler,
            pricing=pricing,
            reward=1_00_000_000,
            volume=100_000,
            failed=True,
        )  # should be ignored
        ContractFactory(
            collateral=1_000_000_000,
            handler=self.handler,
            pricing=pricing,
            reward=1_00_000_000,
            volume=100_000,
            canceled=True,
        )  # should be ignored

        request = self.factory.get(
            reverse("freight:statistics_pilot_corporations_data")
        )
        request.user = self.user

        # when
        response = views.statistics_pilot_corporations_data(request)

        # then
        self.assertEqual(response.status_code, HTTPStatus.OK)

        data: list = json_response_to_python(response)["data"]
        self.assertEqual(len(data), 1)
        obj = data.pop()
        self.assertEqual(obj["collaterals"], 3_000_000_000)
        self.assertEqual(obj["contracts"], 2)
        self.assertEqual(obj["name"], corporation_name)
        self.assertEqual(obj["alliance"], alliance_name)
        self.assertEqual(obj["rewards"], 300_000_000)
        self.assertEqual(obj["volume"], 300_000)

    def test_statistics_customer_data(self):
        # given
        pricing = PricingFactory(
            start_location=LocationStationFactory(name="Jita"),
            end_location=LocationStationFactory(name="Amamake"),
        )
        corporation_name = "Finance ABC"
        customer_name = "Clark Kent"
        customer = EveCharacterFactory(
            character_name=customer_name,
            corporation=EveCorporationInfoFactory(corporation_name=corporation_name),
        )
        ContractFactory(
            issuer=customer,
            collateral=2_000_000_000,
            finished=True,
            handler=self.handler,
            pricing=pricing,
            reward=200_000_000,
            volume=200_000,
        )
        ContractFactory(
            issuer=customer,
            collateral=1_000_000_000,
            finished=True,
            handler=self.handler,
            pricing=pricing,
            reward=1_00_000_000,
            volume=100_000,
        )
        ContractFactory(
            issuer=customer,
            collateral=1_000_000_000,
            handler=self.handler,
            pricing=pricing,
            reward=1_00_000_000,
            volume=100_000,
        )  # should be ignored
        ContractFactory(
            issuer=customer,
            collateral=1_000_000_000,
            handler=self.handler,
            pricing=pricing,
            reward=1_00_000_000,
            volume=100_000,
            failed=True,
        )  # should be ignored
        ContractFactory(
            issuer=customer,
            collateral=1_000_000_000,
            handler=self.handler,
            pricing=pricing,
            reward=1_00_000_000,
            volume=100_000,
            canceled=True,
        )  # should be ignored

        request = self.factory.get(reverse("freight:statistics_customer_data"))
        request.user = self.user

        # when
        response = views.statistics_customer_data(request)

        # then
        self.assertEqual(response.status_code, HTTPStatus.OK)

        data: list = json_response_to_python(response)["data"]
        self.assertEqual(len(data), 1)
        obj = data.pop()
        self.assertEqual(obj["collaterals"], 3_000_000_000)
        self.assertEqual(obj["contracts"], 2)
        self.assertEqual(obj["name"], customer_name)
        self.assertEqual(obj["corporation"], corporation_name)
        self.assertEqual(obj["rewards"], 300_000_000)
        self.assertEqual(obj["volume"], 300_000)
