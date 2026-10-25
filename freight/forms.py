"""Forms for Freight."""

import json
import math
import urllib.error
import urllib.parse
import urllib.request

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator

from allianceauth.services.hooks import get_extension_logger

from .models import PilotAvailability, Pricing

logger = get_extension_logger(__name__)

JANICE_APPRAISAL_URL = "https://janice.e-351.com/api/rest/v2/appraisal"
JANICE_APPRAISAL_VIEW_URL = "https://janice.e-351.com/a/{code}"
JANICE_APPRAISAL_TIMEOUT = 15


class JaniceAppraisalError(Exception):
    """Raised when a Janice appraisal can not be created."""


def create_janice_appraisal(api_key: str, items: str) -> dict:
    """Create a Janice appraisal for the pasted item list."""
    api_key = api_key.strip()
    query = urllib.parse.urlencode(
        {
            "market": 2,
            "designation": "appraisal",
            "pricing": "sell",
            "pricingVariant": "immediate",
            "persist": "true",
            "compactize": "true",
            "pricePercentage": 1,
        }
    )
    request = urllib.request.Request(
        f"{JANICE_APPRAISAL_URL}?{query}",
        data=items.encode("utf-8"),
        headers={
            "accept": "application/json",
            "X-ApiKey": api_key,
            "Content-Type": "text/plain",
            "User-Agent": "aa-freight",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=JANICE_APPRAISAL_TIMEOUT
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        response_body = ex.read().decode("utf-8", errors="replace")
        logger.warning(
            "Janice appraisal request failed with HTTP %s: %s",
            ex.code,
            response_body,
        )
        if ex.code == 403:
            raise JaniceAppraisalError(
                "Janice appraisal request was forbidden. "
                "Please check JANICE_API_KEY."
            ) from ex
        raise JaniceAppraisalError(
            f"Janice appraisal request failed with HTTP {ex.code}"
        ) from ex
    except (urllib.error.URLError, TimeoutError) as ex:
        logger.warning("Janice appraisal request failed: %s", ex)
        raise JaniceAppraisalError("Janice appraisal request failed") from ex
    except json.JSONDecodeError as ex:
        logger.warning("Janice appraisal response was not valid JSON: %s", ex)
        raise JaniceAppraisalError("Janice appraisal request failed") from ex


def janice_appraisal_url(appraisal: dict) -> str:
    """Return the public Janice appraisal URL."""
    return JANICE_APPRAISAL_VIEW_URL.format(code=appraisal["code"])


class CalculatorForm(forms.Form):
    """A form for the price calculator."""

    rush = forms.BooleanField(
        required=False,
        label="Rush contract",
        help_text="An online JF pilot is available. Use rush routes and pricing.",
    )
    pricing = forms.ModelChoiceField(
        queryset=Pricing.objects.select_related("start_location", "end_location")
        .filter(is_active=True)
        .order_by("start_location__name", "end_location__name"),
        label="Route",
        help_text="Pick a route for your courier contract",
        empty_label=None,
    )
    volume = forms.IntegerField(
        help_text="Est. volume of your cargo in m3",
        required=False,
        validators=[MinValueValidator(0)],
    )
    collateral = forms.IntegerField(
        help_text=(
            "Collateral in ISK, must be roughly equal to the est. "
            "value of your cargo"
        ),
        required=False,
        validators=[MinValueValidator(0)],
        widget=forms.NumberInput(attrs={"data-isk-approximation": "true"}),
    )
    janice_items = forms.CharField(
        label="Items to appraise",
        help_text=(
            "Paste an EVE item list to use Janice total volume and split price."
        ),
        required=False,
        widget=forms.Textarea(attrs={"rows": 8}),
    )
    janice_extra_collateral = forms.IntegerField(
        label="Extra collateral",
        help_text="Additional ISK to add on top of the Janice split price.",
        required=False,
        validators=[MinValueValidator(0)],
        widget=forms.NumberInput(attrs={"data-isk-approximation": "true"}),
    )
    janice_extra_volume = forms.IntegerField(
        label="Extra volume",
        help_text="Additional m3 to add on top of the Janice packaged volume.",
        required=False,
        validators=[MinValueValidator(0)],
    )

    def __init__(self, *args, janice_api_key=None, rush=False, **kwargs):
        super().__init__(*args, **kwargs)
        online = PilotAvailability.available()
        self.rush_available = online
        if not online:
            self.fields.pop("rush")
        selected_rush = bool(self.data.get("rush")) if self.is_bound else rush
        self.fields["pricing"].queryset = self.fields["pricing"].queryset.filter(
            is_rush=selected_rush and online
        )
        self.janice_api_key = janice_api_key
        if not self.janice_api_key:
            self.fields.pop("janice_items")
            self.fields.pop("janice_extra_collateral")
            self.fields.pop("janice_extra_volume")

    def clean(self):
        if self.data.get("rush") and not self.rush_available:
            raise ValidationError(
                "Rush contracts are unavailable: no JF pilot is online. Please use a standard route."
            )
        pricing = self.cleaned_data.get("pricing")
        if pricing:
            issue_prefix = "Issues:"
            janice_items = self.cleaned_data.get("janice_items")
            if janice_items:
                self._apply_janice_appraisal(janice_items)

            if pricing.requires_volume() and self.cleaned_data.get("volume") is None:
                raise ValidationError(f"{issue_prefix} volume is required")

            if (
                pricing.requires_collateral()
                and self.cleaned_data.get("collateral") is None
            ):
                raise ValidationError(f"{issue_prefix} collateral is required")

            volume = self.cleaned_data.get("volume")
            collateral = self.cleaned_data.get("collateral")
            issues = pricing.get_contract_price_check_issues(volume, collateral)

            if issues:
                raise ValidationError(f"{issue_prefix} {', '.join(issues)}")

    def _apply_janice_appraisal(self, janice_items: str) -> None:
        try:
            appraisal = create_janice_appraisal(self.janice_api_key, janice_items)
            sell_price = appraisal["effectivePrices"]["totalSellPrice"]
            volume = appraisal["totalPackagedVolume"]
        except (KeyError, TypeError, JaniceAppraisalError) as ex:
            raise ValidationError(f"Issues: {ex}") from ex

        failures = appraisal.get("failures")
        if failures:
            raise ValidationError(f"Issues: Janice could not appraise: {failures}")

        extra_collateral = self.cleaned_data.get("janice_extra_collateral") or 0
        extra_volume = self.cleaned_data.get("janice_extra_volume") or 0
        self.cleaned_data["janice_appraisal"] = appraisal
        self.cleaned_data["volume"] = math.ceil(float(volume) + extra_volume)
        self.cleaned_data["collateral"] = math.ceil(
            float(sell_price) + extra_collateral
        )

    def get_calculated_data(self, pricing: object) -> tuple:
        """Return calculated pricing data."""
        if self.is_valid():
            if self.cleaned_data.get("volume"):
                volume = int(self.cleaned_data.get("volume"))
            else:
                volume = 0
            if self.cleaned_data.get("collateral"):
                collateral = int(self.cleaned_data.get("collateral"))
            else:
                collateral = 0
            price = (
                math.ceil(pricing.get_calculated_price(volume, collateral) / 1_000_000)
                * 1_000_000
            )

        else:
            volume = None
            collateral = None
            price = None

        return volume, collateral, price


class AddLocationForm(forms.Form):
    """A form for adding a new location."""

    location_id = forms.IntegerField(
        label="Location ID",
        help_text="Eve Online ID for a station or an Upwell structure",
    )
