"""Config flow for IKB Direkt Smart Meter integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
import voluptuous as vol

from .api import (
    IKBAuthenticationError,
    IKBConnectionError,
    IKBDirektClient,
    IKBDirektError,
    MeterPoint,
)
from .const import (
    CONF_BASE_PRICE_YEAR,
    CONF_ENERGY_PRICE,
    CONF_HISTORICAL_DAYS,
    CONF_METER_ID,
    CONF_OFFPEAK_PRICE,
    CONF_PASSWORD,
    CONF_TARIFF_MODEL,
    CONF_USERNAME,
    DEFAULT_HISTORICAL_DAYS,
    DOMAIN,
    TARIFF_MODEL_DUAL_SUMMER_NOON,
    TARIFF_MODEL_FIXED,
    TARIFF_MODEL_NONE,
)

_LOGGER = logging.getLogger(__name__)

TARIFF_MODELS = [
    TARIFF_MODEL_NONE,
    TARIFF_MODEL_FIXED,
    TARIFF_MODEL_DUAL_SUMMER_NOON,
]


class IKBDirektConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for IKB Direkt Smart Meter."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._username: str | None = None
        self._password: str | None = None
        self._meters: list[MeterPoint] = []
        self._historical_days = DEFAULT_HISTORICAL_DAYS
        self._reauth_entry = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step where user provides credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]

            self._historical_days = user_input.get(CONF_HISTORICAL_DAYS, DEFAULT_HISTORICAL_DAYS)
            client = IKBDirektClient(self._username, self._password)

            try:
                await client.login()
                meters = await client.get_meters()
                meters = list({(m.counting_point, m.reading_type): m for m in meters}.values())
            except IKBAuthenticationError:
                errors["base"] = "invalid_auth"
            except IKBConnectionError:
                errors["base"] = "cannot_connect"
            except IKBDirektError as err:
                _LOGGER.exception("Unexpected error during IKB setup: %s", err)
                errors["base"] = "unknown"
            else:
                if not meters:
                    errors["base"] = "no_meters"
                elif len(meters) == 1:
                    return await self._create_meter_entry(meters[0])
                else:
                    self._meters = meters
                    return await self.async_step_meter()
            finally:
                await client.close()

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(
                    CONF_HISTORICAL_DAYS, default=DEFAULT_HISTORICAL_DAYS
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle selecting a specific meter if multiple are present."""
        errors: dict[str, str] = {}

        meter_map = {f"{m.counting_point}|{m.reading_type}": m for m in self._meters}
        if user_input is not None:
            meter = meter_map.get(user_input[CONF_METER_ID])
            if meter:
                return await self._create_meter_entry(meter)
            errors["base"] = "no_meters"

        meter_options = {key: m.display_name for key, m in meter_map.items()}
        schema = vol.Schema(
            {
                vol.Required(CONF_METER_ID): vol.In(meter_options),
            }
        )

        return self.async_show_form(step_id="meter", data_schema=schema, errors=errors)

    async def _create_meter_entry(self, meter: MeterPoint) -> FlowResult:
        await self.async_set_unique_id(
            f"{self._username}_{meter.counting_point}_{meter.reading_type}"
        )
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"IKB {meter.meter_number} ({meter.reading_type})",
            data={
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_METER_ID: meter.counting_point,
                "meter_raw_json": meter.raw_json,
                "meter_number": meter.meter_number,
                "reading_type": meter.reading_type,
                "display_name": meter.display_name,
                CONF_HISTORICAL_DAYS: self._historical_days,
            },
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Replace credentials after the portal rejects the stored password."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors = {}
        entry = self._reauth_entry
        if entry is None:
            return self.async_abort(reason="unknown")
        if user_input is not None:
            client = IKBDirektClient(entry.data[CONF_USERNAME], user_input[CONF_PASSWORD])
            try:
                meters = await client.get_meters()
                if not any(m.counting_point == entry.data[CONF_METER_ID]
                           and m.reading_type == entry.data.get("reading_type", "Verbrauch")
                           for m in meters):
                    errors["base"] = "no_meters"
                else:
                    self.hass.config_entries.async_update_entry(
                        entry, data={**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
                    )
                    await self.hass.config_entries.async_reload(entry.entry_id)
                    return self.async_abort(reason="reauth_successful")
            except IKBAuthenticationError:
                errors["base"] = "invalid_auth"
            except IKBConnectionError:
                errors["base"] = "cannot_connect"
            except IKBDirektError:
                errors["base"] = "unknown"
            finally:
                await client.close()
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}), errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return IKBDirektOptionsFlow(config_entry)


class IKBDirektOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for IKB Direkt."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    @property
    def config_entry(self) -> config_entries.ConfigEntry:
        """Return the config entry."""
        return getattr(self, "_config_entry", None) or super().config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage options."""
        errors = {}
        if user_input is not None:
            if user_input.get("offpeak_start_hour", 10) >= user_input.get("offpeak_end_hour", 16):
                errors["base"] = "invalid_offpeak_window"
            else:
                return self.async_create_entry(title="", data=user_input)

        current_days = self.config_entry.options.get(
            CONF_HISTORICAL_DAYS,
            self.config_entry.data.get(CONF_HISTORICAL_DAYS, DEFAULT_HISTORICAL_DAYS),
        )
        current_model = self.config_entry.options.get(
            CONF_TARIFF_MODEL,
            self.config_entry.data.get(CONF_TARIFF_MODEL, TARIFF_MODEL_NONE),
        )
        current_base = self.config_entry.options.get(
            CONF_BASE_PRICE_YEAR,
            self.config_entry.data.get(CONF_BASE_PRICE_YEAR, 0.0),
        )
        current_energy = self.config_entry.options.get(
            CONF_ENERGY_PRICE,
            self.config_entry.data.get(CONF_ENERGY_PRICE, 0.0),
        )
        current_offpeak = self.config_entry.options.get(
            CONF_OFFPEAK_PRICE,
            self.config_entry.data.get(CONF_OFFPEAK_PRICE, 0.0),
        )

        schema = vol.Schema(
            {
                vol.Optional(CONF_HISTORICAL_DAYS, default=current_days): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=365)
                ),
                vol.Optional(CONF_TARIFF_MODEL, default=current_model): vol.In(
                    TARIFF_MODELS
                ),
                vol.Optional(CONF_BASE_PRICE_YEAR, default=float(current_base)): vol.All(
                    vol.Coerce(float), vol.Range(min=0)
                ),
                vol.Optional(CONF_ENERGY_PRICE, default=float(current_energy)): vol.Coerce(
                    float
                ),
                vol.Optional(
                    CONF_OFFPEAK_PRICE, default=float(current_offpeak)
                ): vol.Coerce(float),
                vol.Optional("offpeak_start_hour", default=self.config_entry.options.get("offpeak_start_hour", 10)):
                    vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
                vol.Optional("offpeak_end_hour", default=self.config_entry.options.get("offpeak_end_hour", 16)):
                    vol.All(vol.Coerce(int), vol.Range(min=1, max=24)),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
