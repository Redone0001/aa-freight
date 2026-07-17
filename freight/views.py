"""Views for freight."""

import datetime
import math
from typing import Optional

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Count, Q, Sum
from django.forms import HiddenInput
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.html import format_html
from django.utils.text import format_lazy
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as __
from esi.decorators import token_required
from esi.exceptions import HTTPError
from esi.models import Token

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter, EveCorporationInfo
from allianceauth.services.hooks import get_extension_logger

from . import constants, tasks
from .app_settings import (
    FREIGHT_APP_NAME,
    FREIGHT_OPERATION_MODE,
    FREIGHT_STATISTICS_MAX_DAYS,
    JANICE_API_KEY,
)
from .forms import CalculatorForm, janice_appraisal_url
from .helpers import update_or_create_eveentity_from_character
from .models import Contract, ContractHandler, EveEntity, Location, Pricing

logger = get_extension_logger(__name__)

ADD_LOCATION_TOKEN_TAG = "freight_add_location_token"
JF_DEFAULT_MAX_VOLUME = 340_000
JF_DEFAULT_MAX_COLLATERAL = 40_000_000_000
JF_EXACT_PACKING_LIMIT = 18
JF_PLANNER_STATUSES = [Contract.Status.OUTSTANDING, Contract.Status.IN_PROGRESS]
JF_DEFAULT_STATUSES = [Contract.Status.OUTSTANDING]


def _statistics_days(request) -> int:
    """Return selected statistics window in days."""
    try:
        days = int(request.GET.get("days", FREIGHT_STATISTICS_MAX_DAYS))
    except (TypeError, ValueError):
        days = FREIGHT_STATISTICS_MAX_DAYS
    return days if days >= 1 else FREIGHT_STATISTICS_MAX_DAYS


def add_common_context(request, context: dict) -> dict:
    """adds the common context used by all view"""
    pending_user_count = (
        Contract.objects.all().issued_by_user(request.user).pending_count()
    )
    operation_mode = ContractHandler.Mode(FREIGHT_OPERATION_MODE)
    my_mode = operation_mode.name_display()
    setup_str = __("Setup")
    button_label = format_lazy(
        "{setup} {operation_mode}", setup=setup_str, operation_mode=my_mode
    )
    new_context = {
        **{
            "app_title": FREIGHT_APP_NAME,
            "pending_all_count": Contract.objects.all().pending_count(),
            "pending_user_count": pending_user_count,
            "setup_contract_handler_label": button_label,
        },
        **context,
    }
    return new_context


@login_required
@permission_required("freight.basic_access")
def index(request):
    """Index view."""
    return redirect("freight:calculator")


@login_required
@permission_required("freight.use_calculator")
def contract_list_user(request):
    """View rendering contract list with contracts of a user only."""
    try:
        user_name = request.user.profile.main_character.character_name
    except AttributeError:
        user_name = request.user.username
    context = {
        "page_title": __("My Contracts"),
        "category": constants.CONTRACT_LIST_USER,
        "user_name": user_name,
    }
    return render(
        request, "freight/contracts_user.html", add_common_context(request, context)
    )


@login_required
@permission_required("freight.use_calculator")
def route_dashboard(request):
    """View rendering route-level contract dashboard for calculator users."""
    rows, summary = _route_dashboard_totals()
    context = {
        "page_title": __("Route Dashboard"),
        "route_dashboard_rows": rows,
        "route_dashboard_summary": summary,
    }
    return render(
        request, "freight/route_dashboard.html", add_common_context(request, context)
    )


@login_required
@permission_required("freight.view_contracts")
def contract_list_all(request):
    """View rendering contract list with all contracts."""
    context = {
        "page_title": __("All Contracts"),
        "category": constants.CONTRACT_LIST_ALL,
    }
    return render(
        request, "freight/contracts_all.html", add_common_context(request, context)
    )


@login_required
@permission_required("freight.basic_access")
def contract_list_data(request, category: str) -> JsonResponse:
    """Return list of outstanding contracts for contract_list AJAX call."""
    contracts_data = []
    contracts_qs = Contract.objects.select_related(
        "acceptor",
        "acceptor_corporation",
        "end_location",
        "issuer",
        "start_location",
        "pricing",
        "pricing__start_location",
        "pricing__end_location",
    ).contract_list_filter(category=category, user=request.user)
    for contract in contracts_qs:
        route_name, pricing_check = _fetch_pricing_infos(contract)
        notes = _calc_notes(contract)

        start_location_html = format_html(
            '<span class="dotted-underline" title="{}">{}</span> {}',
            contract.start_location,
            contract.start_location.solar_system_name,
            notes,
        )
        end_location_html = format_html(
            '<span class="dotted-underline" title="{}">{}</span>',
            contract.end_location,
            contract.end_location.solar_system_name,
        )
        contracts_data.append(
            {
                "contract_id": contract.contract_id,
                "status": str(contract.status),
                "start_location": {
                    "display": start_location_html,
                    "sort": contract.start_location.name,
                },
                "end_location": {
                    "display": end_location_html,
                    "sort": contract.end_location.name,
                },
                "reward": contract.reward,
                "collateral": contract.collateral,
                "volume": contract.volume,
                "date_issued": contract.date_issued.isoformat(),
                "date_expired": contract.date_expired.isoformat(),
                "issuer": contract.issuer.character_name,
                "date_accepted": (
                    contract.date_accepted.isoformat()
                    if contract.date_accepted
                    else None
                ),
                "acceptor": contract.acceptor_name,
                "has_pricing": contract.has_pricing,
                "has_pricing_errors": contract.has_pricing_errors,
                "pricing_check": pricing_check,
                "route_name": route_name,
                "is_in_progress": contract.is_in_progress,
                "is_failed": contract.is_failed,
                "is_completed": contract.is_completed,
            }
        )
    return JsonResponse({"data": contracts_data})


@login_required
@permission_required("freight.view_contracts")
def jf_planner(request):
    """View rendering the jump freighter planner."""
    limits = _jf_planner_limits(request)
    selected_route_keys = _jf_selected_route_keys(request)
    selected_statuses = _jf_selected_statuses(request)
    context = {
        "page_title": __("JF Planner"),
        "jf_max_volume": limits["max_volume_input"],
        "jf_max_collateral": limits["max_collateral_input"],
        "jf_convoy_routes": _jf_planner_route_choices(
            request.user, selected_route_keys, selected_statuses
        ),
        "jf_convoy_routes_csv": ",".join(selected_route_keys),
        "jf_statuses": _jf_status_choices(selected_statuses),
        "jf_statuses_csv": ",".join(selected_statuses),
    }
    return render(
        request, "freight/jf_planner.html", add_common_context(request, context)
    )


@login_required
@permission_required("freight.view_contracts")
def jf_planner_data(request):
    """Return active contracts arranged into jump freighter loads."""
    limits = _jf_planner_limits(request)
    max_volume = limits["max_volume"]
    max_collateral = limits["max_collateral"]
    selected_route_keys = _jf_selected_route_keys(request)
    selected_statuses = _jf_selected_statuses(request)
    contracts = list(
        Contract.objects.select_related(
            "end_location",
            "issuer",
            "pricing",
            "pricing__start_location",
            "pricing__end_location",
            "start_location",
        )
        .contract_list_filter(category=constants.CONTRACT_LIST_ACTIVE, user=request.user)
        .filter(status__in=selected_statuses)
        .order_by("date_expired", "contract_id")
    )
    loads = _arrange_jf_loads(
        contracts, max_volume, max_collateral, selected_route_keys
    )
    return JsonResponse(
        {
            "data": _jf_planner_rows(loads, max_volume, max_collateral),
            "summary": _jf_planner_summary(loads),
        }
    )


def _calc_notes(contract):
    if contract.title or settings.DEBUG:
        if settings.DEBUG:
            title_first = f"{contract.title} " if contract.title else ""
            title = f"{title_first}{contract.contract_id}"
        else:
            title = contract.title

        notes = format_html('<i class="far fa-envelope" title="{}"></i>', title)
    else:
        notes = ""
    return notes


def _fetch_pricing_infos(contract):
    if contract.has_pricing:
        route_name = contract.pricing.name
        if not contract.has_pricing_errors:
            tooltip_text = route_name
            icon_html = format_html(
                '<span class="{}"><i class="fas fa-check" title="{}"></i></span>',
                "text-success",
                tooltip_text,
            )
        else:
            issues_text = "\n".join(contract.get_issue_list())
            tooltip_text = f"{route_name}\n{issues_text}"
            icon_html = format_html(
                (
                    '<span class="{}">'
                    '<i class="fas fa-exclamation-triangle" title="{}"></i>'
                    "</span>"
                ),
                "text-danger",
                tooltip_text,
            )

        pricing_check = icon_html
    else:
        route_name = ""
        pricing_check = "-"
    return route_name, pricing_check


def _route_dashboard_totals() -> tuple:
    """Return global route aggregates for configured contract routes."""
    route_totals = {}
    contracts = Contract.objects.filter(pricing__isnull=False).select_related(
        "pricing",
        "pricing__end_location",
        "pricing__start_location",
    )

    for contract in contracts:
        pricing = contract.pricing
        route = route_totals.setdefault(
            pricing.pk,
            {
                "route": pricing.name,
                "contracts": 0,
                "volume": 0,
                "delivered_contracts": 0,
                "to_deliver_contracts": 0,
                "to_deliver_volume": 0,
                "delivery_seconds": 0,
            },
        )
        route["contracts"] += 1
        route["volume"] += contract.volume or 0

        if contract.status in {Contract.Status.OUTSTANDING, Contract.Status.IN_PROGRESS}:
            route["to_deliver_contracts"] += 1
            route["to_deliver_volume"] += contract.volume or 0
            continue

        if (
            contract.status == Contract.Status.FINISHED
            and contract.date_issued
            and contract.date_completed
        ):
            delivery_seconds = (contract.date_completed - contract.date_issued).total_seconds()
            if delivery_seconds >= 0:
                route["delivered_contracts"] += 1
                route["delivery_seconds"] += delivery_seconds

    total_contracts = 0
    total_volume = 0
    total_delivered_contracts = 0
    total_to_deliver_contracts = 0
    total_to_deliver_volume = 0
    total_delivery_seconds = 0
    rows = []
    for route in route_totals.values():
        total_contracts += route["contracts"]
        total_volume += route["volume"]
        total_delivered_contracts += route["delivered_contracts"]
        total_to_deliver_contracts += route["to_deliver_contracts"]
        total_to_deliver_volume += route["to_deliver_volume"]
        total_delivery_seconds += route["delivery_seconds"]
        average_seconds = (
            route["delivery_seconds"] / route["delivered_contracts"]
            if route["delivered_contracts"]
            else None
        )
        rows.append(
            {
                "route": route["route"],
                "contracts": route["contracts"],
                "volume": route["volume"],
                "delivered_contracts": route["delivered_contracts"],
                "to_deliver_contracts": route["to_deliver_contracts"],
                "to_deliver_volume": route["to_deliver_volume"],
                "average_delivery_time": _format_duration(average_seconds),
            }
        )

    rows.sort(key=lambda row: (-row["contracts"], -row["volume"], row["route"]))
    summary_average_seconds = (
        total_delivery_seconds / total_delivered_contracts
        if total_delivered_contracts
        else None
    )
    summary = {
        "routes": len(rows),
        "contracts": total_contracts,
        "volume": total_volume,
        "delivered_contracts": total_delivered_contracts,
        "to_deliver_contracts": total_to_deliver_contracts,
        "to_deliver_volume": total_to_deliver_volume,
        "average_delivery_time": _format_duration(summary_average_seconds),
    }
    return rows, summary


def _format_duration(seconds) -> str:
    """Format a duration in seconds for dashboard display."""
    if seconds is None:
        return "-"

    minutes = max(0, int(round(seconds / 60)))
    if minutes < 1:
        return "<1m"

    days, remainder = divmod(minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)

    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def _jf_planner_limits(request) -> dict:
    max_volume_raw = request.GET.get("max_volume")
    max_collateral_raw = request.GET.get("max_collateral")
    max_volume, max_volume_valid = _parse_jf_limit(
        max_volume_raw, JF_DEFAULT_MAX_VOLUME
    )
    max_collateral, max_collateral_valid = _parse_jf_limit(
        max_collateral_raw, JF_DEFAULT_MAX_COLLATERAL, allow_suffix=True
    )
    return {
        "max_volume": max_volume,
        "max_collateral": max_collateral,
        "max_volume_input": (
            max_volume_raw
            if max_volume_raw and max_volume_valid
            else f"{JF_DEFAULT_MAX_VOLUME:,.0f}".replace(",", "")
        ),
        "max_collateral_input": (
            max_collateral_raw if max_collateral_raw and max_collateral_valid else "40b"
        ),
    }


def _parse_jf_limit(value, default: float, allow_suffix: bool = False) -> tuple:
    if value is None or str(value).strip() == "":
        return float(default), False

    text = str(value).strip().lower().replace(",", "").replace("_", "")
    text = text.replace(" ", "")
    multiplier = 1
    if allow_suffix and text[-1:] in {"k", "m", "b", "t"}:
        multiplier = {"k": 10**3, "m": 10**6, "b": 10**9, "t": 10**12}[text[-1]]
        text = text[:-1]

    try:
        limit = float(text) * multiplier
    except ValueError:
        return float(default), False

    if limit <= 0:
        return float(default), False
    return limit, True


def _jf_planner_route_choices(
    user, selected_route_keys: list, selected_statuses: list
) -> list:
    routes = {}
    contracts = (
        Contract.objects.select_related("end_location", "start_location")
        .contract_list_filter(category=constants.CONTRACT_LIST_ACTIVE, user=user)
        .filter(status__in=selected_statuses)
        .order_by("start_location__name", "end_location__name")
    )
    for contract in contracts:
        route_key = _jf_route_key_value(contract)
        routes.setdefault(
            route_key,
            {
                "key": route_key,
                "name": _jf_route_display(contract),
                "selected": route_key in selected_route_keys,
            },
        )
    return list(routes.values())


def _jf_status_choices(selected_statuses: list) -> list:
    return [
        {
            "value": status.value,
            "name": status.label,
            "selected": status.value in selected_statuses,
        }
        for status in JF_PLANNER_STATUSES
    ]


def _jf_selected_statuses(request) -> list:
    raw_values = []
    raw_values.extend(request.GET.getlist("statuses"))
    raw_values.extend(request.GET.getlist("statuses[]"))
    valid_statuses = {status.value for status in JF_PLANNER_STATUSES}

    statuses = []
    for value in raw_values:
        for status in str(value).split(","):
            status = status.strip()
            if status in valid_statuses and status not in statuses:
                statuses.append(status)

    return statuses or [status.value for status in JF_DEFAULT_STATUSES]


def _jf_selected_route_keys(request) -> list:
    raw_values = []
    raw_values.extend(request.GET.getlist("convoy_routes"))
    raw_values.extend(request.GET.getlist("convoy_routes[]"))

    route_keys = []
    for value in raw_values:
        for route_key in str(value).split(","):
            route_key = route_key.strip()
            if route_key and route_key not in route_keys:
                route_keys.append(route_key)
    return route_keys


def _arrange_jf_loads(
    contracts: list,
    max_volume: float,
    max_collateral: float,
    convoy_route_keys: list = None,
) -> list:
    convoy_route_keys = set(convoy_route_keys or [])
    if convoy_route_keys:
        convoy_contracts = [
            contract
            for contract in contracts
            if _jf_route_key_value(contract) in convoy_route_keys
        ]
        storage_contracts = [
            contract
            for contract in contracts
            if _jf_route_key_value(contract) not in convoy_route_keys
        ]
        loads = _arrange_jf_contract_group(
            convoy_contracts,
            max_volume,
            max_collateral,
            summary_route="Convoy",
        )
        if storage_contracts:
            loads.append(
                {
                    "contracts": storage_contracts,
                    "over_limit": False,
                    "storage": True,
                    "summary_route": "Storage",
                }
            )
        return loads

    grouped_contracts = {}
    for contract in contracts:
        grouped_contracts.setdefault(_jf_route_key(contract), []).append(contract)

    loads = []
    for route_contracts in grouped_contracts.values():
        loads.extend(
            _arrange_jf_contract_group(
                route_contracts,
                max_volume,
                max_collateral,
            )
        )

    return loads


def _jf_route_key(contract) -> tuple:
    return contract.start_location_id, contract.end_location_id


def _jf_route_key_value(contract) -> str:
    return f"{contract.start_location_id}:{contract.end_location_id}"


def _jf_route_display(contract) -> str:
    return (
        f"{contract.start_location.solar_system_name} >> "
        f"{contract.end_location.solar_system_name}"
    )


def _arrange_jf_contract_group(
    contracts: list,
    max_volume: float,
    max_collateral: float,
    summary_route: str = None,
) -> list:
    packable = [
        contract
        for contract in contracts
        if contract.volume <= max_volume and contract.collateral <= max_collateral
    ]
    oversized = [contract for contract in contracts if contract not in packable]
    ordered_contracts = sorted(
        packable,
        key=lambda contract: (
            max(contract.volume / max_volume, contract.collateral / max_collateral),
            contract.volume,
            contract.collateral,
        ),
        reverse=True,
    )
    if len(ordered_contracts) <= JF_EXACT_PACKING_LIMIT:
        loads = _arrange_jf_loads_exact(
            ordered_contracts, max_volume, max_collateral
        )
    else:
        loads = _arrange_jf_loads_greedy(
            ordered_contracts, max_volume, max_collateral
        )

    for load in loads:
        if summary_route:
            load["summary_route"] = summary_route

    for contract in oversized:
        load = {"contracts": [contract], "over_limit": True}
        if summary_route:
            load["summary_route"] = summary_route
        loads.append(load)

    return loads


def _arrange_jf_loads_exact(
    contracts: list, max_volume: float, max_collateral: float
) -> list:
    if not contracts:
        return []

    min_loads = max(
        math.ceil(sum(contract.volume for contract in contracts) / max_volume),
        math.ceil(
            sum(contract.collateral for contract in contracts) / max_collateral
        ),
    )
    greedy_loads = _arrange_jf_loads_greedy(contracts, max_volume, max_collateral)

    for load_count in range(min_loads, len(greedy_loads) + 1):
        assignment = _find_jf_assignment(
            contracts, max_volume, max_collateral, load_count
        )
        if assignment:
            return assignment
    return greedy_loads


def _find_jf_assignment(
    contracts: list, max_volume: float, max_collateral: float, load_count: int
) -> Optional[list]:
    loads = [
        {
            "contracts": [],
            "volume": 0,
            "collateral": 0,
            "over_limit": False,
        }
        for _ in range(load_count)
    ]

    def backtrack(index: int) -> bool:
        if index >= len(contracts):
            return True

        contract = contracts[index]
        tried_loads = set()
        for load in loads:
            key = (load["volume"], load["collateral"])
            if key in tried_loads:
                continue
            tried_loads.add(key)
            if (
                load["volume"] + contract.volume <= max_volume
                and load["collateral"] + contract.collateral <= max_collateral
            ):
                load["contracts"].append(contract)
                load["volume"] += contract.volume
                load["collateral"] += contract.collateral
                if backtrack(index + 1):
                    return True
                load["contracts"].pop()
                load["volume"] -= contract.volume
                load["collateral"] -= contract.collateral

            if load["volume"] == 0 and load["collateral"] == 0:
                break
        return False

    if not backtrack(0):
        return None
    return [load for load in loads if load["contracts"]]


def _arrange_jf_loads_greedy(
    contracts: list, max_volume: float, max_collateral: float
) -> list:
    loads = []
    for contract in contracts:
        best_load = None
        best_score = None
        for load in loads:
            new_volume = load["volume"] + contract.volume
            new_collateral = load["collateral"] + contract.collateral
            if new_volume <= max_volume and new_collateral <= max_collateral:
                score = max(new_volume / max_volume, new_collateral / max_collateral)
                if best_score is None or score > best_score:
                    best_load = load
                    best_score = score

        if best_load is None:
            best_load = {
                "contracts": [],
                "volume": 0,
                "collateral": 0,
                "over_limit": False,
            }
            loads.append(best_load)

        best_load["contracts"].append(contract)
        best_load["volume"] += contract.volume
        best_load["collateral"] += contract.collateral
    return loads


def _jf_planner_rows(loads: list, max_volume: float, max_collateral: float) -> list:
    rows = []
    for load_index, load in enumerate(loads, start=1):
        load_volume = sum(contract.volume for contract in load["contracts"])
        load_collateral = sum(contract.collateral for contract in load["contracts"])
        load_over_limit = load["over_limit"]
        load_storage = load.get("storage", False)
        for contract in load["contracts"]:
            route_name, pricing_check = _fetch_pricing_infos(contract)
            rows.append(
                {
                    "load_index": 999999 if load_storage else load_index,
                    "jf_load": "Storage" if load_storage else f"JF {load_index}",
                    "jf_volume": load_volume,
                    "jf_collateral": load_collateral,
                    "jf_volume_percent": load_volume / max_volume * 100,
                    "jf_collateral_percent": load_collateral / max_collateral * 100,
                    "jf_contracts": len(load["contracts"]),
                    "over_limit": load_over_limit,
                    "storage": load_storage,
                    "contract_id": contract.contract_id,
                    "route": _jf_route_display(contract),
                    "route_name": route_name,
                    "status": str(contract.status),
                    "issuer": contract.issuer.character_name,
                    "reward": contract.reward,
                    "collateral": contract.collateral,
                    "volume": contract.volume,
                    "pricing_check": pricing_check,
                }
            )
    return rows


def _jf_planner_summary(loads: list) -> list:
    summary = {}
    for load in loads:
        if not load["contracts"] or load.get("storage"):
            continue

        first_contract = load["contracts"][0]
        summary_route = load.get("summary_route")
        route_key = summary_route or _jf_route_key(first_contract)
        if route_key not in summary:
            summary[route_key] = {
                "route": summary_route or _jf_route_display(first_contract),
                "jf_loads": 0,
                "reward": 0,
            }

        summary[route_key]["jf_loads"] += 1
        summary[route_key]["reward"] += sum(
            contract.reward for contract in load["contracts"]
        )

    return list(summary.values())


@login_required
@permission_required("freight.use_calculator")
def calculator(request, pricing_pk=None):
    """Calculator view."""
    if request.method != "POST":
        pricing = Pricing.objects.get_or_default(pricing_pk)
        form = CalculatorForm(
            initial={"pricing": pricing}, janice_api_key=JANICE_API_KEY
        )
        price = None
        volume = None
        collateral = None
        appraisal_url = None
    else:
        form = CalculatorForm(request.POST, janice_api_key=JANICE_API_KEY)
        request.POST._mutable = True  # pylint: disable=protected-access
        pricing_pk = form.data.get("pricing")
        pricing = Pricing.objects.get_or_default(pricing_pk)
        volume, collateral, price = form.get_calculated_data(pricing)
        appraisal = (
            form.cleaned_data.get("janice_appraisal") if form.is_valid() else None
        )
        appraisal_url = janice_appraisal_url(appraisal) if appraisal else None

    if pricing:
        price_per_volume_eff = pricing.price_per_volume_eff()
        if not pricing.requires_volume():
            form.fields["volume"].widget = HiddenInput()
        if not pricing.requires_collateral():
            form.fields["collateral"].widget = HiddenInput()
    else:
        price_per_volume_eff = None

    if price:
        if pricing.days_to_expire:
            expires_on = datetime.datetime.now(
                datetime.timezone.utc
            ) + datetime.timedelta(days=pricing.days_to_expire)
        else:
            expires_on = None
    else:
        collateral = None
        volume = None
        expires_on = None

    handler = ContractHandler.objects.select_related("organization").first()
    if handler:
        organization_name = handler.organization.name
        availability = handler.get_availability_text_for_contracts()
    else:
        organization_name = None
        availability = None
    context = {
        "page_title": __("Reward Calculator"),
        "form": form,
        "pricing": pricing,
        "has_pricing": Pricing.objects.exists(),
        "price": price,
        "organization_name": organization_name,
        "collateral": collateral if collateral is not None else 0,
        "volume": volume if volume is not None else None,
        "expires_on": expires_on,
        "availability": availability,
        "janice_appraisal_url": appraisal_url,
        "pricing_price_per_volume_eff": price_per_volume_eff,
    }
    return render(
        request, "freight/calculator.html", add_common_context(request, context)
    )


@login_required
@permission_required("freight.setup_contract_handler")
@token_required(scopes=ContractHandler.get_esi_scopes())
def setup_contract_handler(request, token: Token):
    """View for setting up a new contract handler."""
    token_char = get_object_or_404(EveCharacter, character_id=token.character_id)
    try:
        owned_char = CharacterOwnership.objects.get(
            user=request.user, character=token_char
        )
    except CharacterOwnership.DoesNotExist:
        messages.error(
            request,
            __(
                "You can only use your main or alt characters to setup "
                "the contract handler. "
                "However, character %s is neither. ",
            )
            % token_char.character_name,
        )
        return redirect("freight:index")

    operation_mode = ContractHandler.Mode(FREIGHT_OPERATION_MODE)
    handler = ContractHandler.objects.first()
    if handler and handler.operation_mode != operation_mode:
        messages.error(
            request,
            __(
                "There already is a contract handler installed for a "
                "different operation mode. You need to first delete the "
                "existing contract handler in the admin section "
                "before you can set up this app for a different operation mode."
            ),
        )
        return redirect("freight:index")

    category = operation_mode.category()
    if category == EveEntity.CATEGORY_ALLIANCE and not token_char.alliance_id:
        messages.error(
            request,
            __(
                "Can not setup contract handler, "
                "because %s is not a member of any alliance"
            )
            % token_char,
        )
        return redirect("freight:index")

    organization, _ = update_or_create_eveentity_from_character(token_char, category)

    handler, _ = ContractHandler.objects.update_or_create(
        organization=organization,
        defaults={
            "character": owned_char,
            "operation_mode": operation_mode,
        },
    )
    tasks.run_contracts_sync.delay(force_sync=True)
    messages.success(
        request,
        __(
            "Contract Handler setup started for "
            "%(organization)s organization "
            "with %(character)s as sync character. "
            "Operation mode: %(operation_mode)s. "
            "Started syncing of courier contracts. "
        )
        % {
            "organization": organization.name,
            "character": handler.character.character.character_name,
            "operation_mode": handler.operation_mode_display,
        },
    )
    return redirect("freight:index")


@login_required
@token_required(scopes=Location.get_esi_scopes())
@permission_required("freight.add_location")
def add_location(request, token):
    """1st view when adding a new location."""
    request.session[ADD_LOCATION_TOKEN_TAG] = token.pk
    return redirect("freight:add_location_2")


@login_required
@permission_required("freight.add_location")
def add_location_2(request):
    """2nd view when adding a new location."""
    from .forms import AddLocationForm

    if ADD_LOCATION_TOKEN_TAG not in request.session:
        raise RuntimeError("Missing token in session")

    token = get_object_or_404(Token, pk=request.session[ADD_LOCATION_TOKEN_TAG])
    if request.method != "POST":
        form = AddLocationForm()
    else:
        form = AddLocationForm(request.POST)
        if form.is_valid():
            location_id = form.cleaned_data["location_id"]
            try:
                location, created = Location.objects.update_or_create_esi(
                    token=token, location_id=location_id, add_unknown=False
                )
            except HTTPError as ex:
                messages.error(
                    request,
                    __(
                        "Failed to add location with token from %(character)s "
                        "for location ID %(id)s: %(error)s"
                    )
                    % {
                        "character": token.character_name,
                        "id": location_id,
                        "error": type(ex).__name__,
                    },
                )
            else:
                action_txt = __("Added:") if created else __("Updated:")
                messages.success(request, f"{action_txt} {location.name}")
                return redirect("freight:add_location_2")
    context = {
        "page_title": __("Add / Update Location"),
        "form": form,
        "token_char_name": token.character_name,
    }
    return render(
        request, "freight/add_location.html", add_common_context(request, context)
    )


@login_required
@permission_required("freight.view_statistics")
def statistics(request):
    """View for rendering the statistics page."""
    statistics_days = _statistics_days(request)
    context = {
        "page_title": "Statistics",
        "max_days": FREIGHT_STATISTICS_MAX_DAYS,
        "statistics_days": statistics_days,
    }
    return render(
        request, "freight/statistics.html", add_common_context(request, context)
    )


@login_required
@permission_required("freight.view_statistics")
def statistics_routes_data(request):
    """Returns total for statistics as JSON."""
    cutoff_date = now() - datetime.timedelta(days=_statistics_days(request))
    finished_contracts = Q(contracts__status=Contract.Status.FINISHED) & Q(
        contracts__date_completed__gte=cutoff_date
    )
    route_totals = (
        Pricing.objects.annotate(
            contracts_count=Count("contracts", filter=finished_contracts)
        )
        .select_related("start_location", "end_location")
        .annotate(rewards=Sum("contracts__reward", filter=finished_contracts))
        .annotate(collaterals=Sum("contracts__collateral", filter=finished_contracts))
        .annotate(volume=Sum("contracts__volume", filter=finished_contracts))
        .annotate(
            pilots=Count(
                "contracts__acceptor", distinct=True, filter=finished_contracts
            )
        )
        .annotate(
            customers=Count(
                "contracts__issuer", distinct=True, filter=finished_contracts
            )
        )
    )
    totals = [
        {
            "name": route.name,
            "contracts": route.contracts_count,
            "rewards": route.rewards,
            "collaterals": route.collaterals,
            "volume": route.volume,
            "pilots": route.pilots,
            "customers": route.customers,
        }
        for route in route_totals
        if route.contracts_count > 0
    ]
    return JsonResponse({"data": totals})


@login_required
@permission_required("freight.view_statistics")
def statistics_pilots_data(request):
    """returns totals for statistics as JSON"""
    cutoff_date = now() - datetime.timedelta(days=_statistics_days(request))
    finished_contracts = Q(contracts_acceptor__status=Contract.Status.FINISHED) & Q(
        contracts_acceptor__date_completed__gte=cutoff_date
    )
    pilot_totals = (
        EveCharacter.objects.exclude(contracts_acceptor__isnull=True)
        .annotate(
            contracts_count=Count("contracts_acceptor", filter=finished_contracts)
        )
        .annotate(rewards=Sum("contracts_acceptor__reward", filter=finished_contracts))
        .annotate(
            collaterals=Sum("contracts_acceptor__collateral", filter=finished_contracts)
        )
        .annotate(volume=Sum("contracts_acceptor__volume", filter=finished_contracts))
    )
    totals = [
        {
            "name": pilot.character_name,
            "corporation": pilot.corporation_name,
            "contracts": pilot.contracts_count,
            "rewards": pilot.rewards,
            "collaterals": pilot.collaterals,
            "volume": pilot.volume,
        }
        for pilot in pilot_totals
        if pilot.contracts_count > 0
    ]
    return JsonResponse({"data": totals})


@login_required
@permission_required("freight.view_statistics")
def statistics_pilot_corporations_data(request):
    """returns totals for statistics as JSON"""
    cutoff_date = now() - datetime.timedelta(days=_statistics_days(request))
    finished_contracts = Q(
        contracts_acceptor_corporation__status=Contract.Status.FINISHED
    ) & Q(contracts_acceptor_corporation__date_completed__gte=cutoff_date)

    corporation_totals = (
        EveCorporationInfo.objects.exclude(contracts_acceptor_corporation__isnull=True)
        .select_related("alliance")
        .annotate(
            contracts_count=Count(
                "contracts_acceptor_corporation", filter=finished_contracts
            )
        )
        .annotate(
            rewards=Sum(
                "contracts_acceptor_corporation__reward", filter=finished_contracts
            )
        )
        .annotate(
            collaterals=Sum(
                "contracts_acceptor_corporation__collateral", filter=finished_contracts
            )
        )
        .annotate(
            volume=Sum(
                "contracts_acceptor_corporation__volume", filter=finished_contracts
            )
        )
    )
    totals = []
    for corporation in corporation_totals:
        if corporation.contracts_count > 0:
            alliance = (
                corporation.alliance.alliance_name if corporation.alliance else ""
            )
            totals.append(
                {
                    "name": corporation.corporation_name,
                    "alliance": alliance,
                    "contracts": corporation.contracts_count,
                    "rewards": corporation.rewards,
                    "collaterals": corporation.collaterals,
                    "volume": corporation.volume,
                }
            )
    return JsonResponse({"data": totals})


@login_required
@permission_required("freight.view_statistics")
def statistics_customer_data(request):
    """returns totals for statistics as JSON"""
    cutoff_date = now() - datetime.timedelta(days=_statistics_days(request))
    finished_contracts = Q(contracts_issuer__status=Contract.Status.FINISHED) & Q(
        contracts_issuer__date_completed__gte=cutoff_date
    )
    customer_totals = (
        EveCharacter.objects.exclude(contracts_issuer__isnull=True)
        .annotate(contracts_count=Count("contracts_issuer", filter=finished_contracts))
        .annotate(rewards=Sum("contracts_issuer__reward", filter=finished_contracts))
        .annotate(
            collaterals=Sum("contracts_issuer__collateral", filter=finished_contracts)
        )
        .annotate(volume=Sum("contracts_issuer__volume", filter=finished_contracts))
    )
    totals = []
    for customer in customer_totals:
        if customer.contracts_count > 0:
            totals.append(
                {
                    "name": customer.character_name,
                    "corporation": customer.corporation_name,
                    "contracts": customer.contracts_count,
                    "rewards": customer.rewards,
                    "collaterals": customer.collaterals,
                    "volume": customer.volume,
                }
            )
    return JsonResponse({"data": totals})
