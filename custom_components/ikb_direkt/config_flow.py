"""Config flow for IKB Direkt Smart Meter integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
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

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step where user provides credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]

            session = async_get_clientsession(self.hass)
            client = IKBDirektClient(self._username, self._password, session=session)

            try:
                await client.login()
                meters = await client.get_meters()
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
                    # Single meter found -> automatically set up
                    meter = meters[0]
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
                            CONF_HISTORICAL_DAYS: user_input.get(
                                CONF_HISTORICAL_DAYS, DEFAULT_HISTORICAL_DAYS
                            ),
                        },
                    )
                else:
                    # Multiple meters found -> proceed to meter selection step
                    self._meters = meters
                    return await self.async_step_meter()

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

        if user_input is not None:
            selected_cp = user_input[CONF_METER_ID]
            meter = next(
                (m for m in self._meters if m.counting_point == selected_cp), None
            )
            if meter:
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
                        CONF_HISTORICAL_DAYS: DEFAULT_HISTORICAL_DAYS,
                    },
                )

        meter_options = {m.counting_point: m.display_name for m in self._meters}
        schema = vol.Schema(
            {
                vol.Required(CONF_METER_ID): vol.In(meter_options),
            }
        )

        return self.async_show_form(step_id="meter", data_schema=schema, errors=errors)

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
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage options."""
        if user_input is not None:
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
                vol.Optional(CONF_BASE_PRICE_YEAR, default=float(current_base)): vol.Coerce(
                    float
                ),
                vol.Optional(CONF_ENERGY_PRICE, default=float(current_energy)): vol.Coerce(
                    float
                ),
                vol.Optional(
                    CONF_OFFPEAK_PRICE, default=float(current_offpeak)
                ): vol.Coerce(float),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
