"""ENTSO-e current electricity and gas price information service."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

import pandas as pd

from homeassistant.components.sensor import DOMAIN, RestoreSensor, SensorDeviceClass, SensorEntityDescription, SensorExtraStoredData, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
)
from homeassistant.core import HassJob, HomeAssistant
from homeassistant.helpers import event
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import utcnow
from .const import ATTRIBUTION, CONF_COORDINATOR, CONF_ENTITY_NAME, DOMAIN, ICON, DEFAULT_CURRENCY, CONF_CURRENCY
from .coordinator import EntsoeCoordinator

_LOGGER = logging.getLogger(__name__)

@dataclass
class EntsoeEntityDescription(SensorEntityDescription):
    """Describes ENTSO-e sensor entity."""

    value_fn: Callable[[dict], StateType] = None


def sensor_descriptions(currency: str) -> tuple[EntsoeEntityDescription, ...]:
    """Construct EntsoeEntityDescription."""
    return (
        EntsoeEntityDescription(
            key="current_price",
            name="Current electricity market price",
            native_unit_of_measurement=f"{currency}/{UnitOfEnergy.KILO_WATT_HOUR}",
            state_class=SensorStateClass.MEASUREMENT,
            value_fn=lambda data: data["current_price"]
        ),
        EntsoeEntityDescription(
            key="next_hour_price",
            name="Next hour electricity market price",
            native_unit_of_measurement=f"{currency}/{UnitOfEnergy.KILO_WATT_HOUR}",
            state_class=SensorStateClass.MEASUREMENT,
            value_fn=lambda data: data["next_hour_price"],
        ),
        EntsoeEntityDescription(
            key="min_price",
            name="Lowest energy price today",
            native_unit_of_measurement=f"{currency}/{UnitOfEnergy.KILO_WATT_HOUR}",
            state_class=SensorStateClass.MEASUREMENT,
            value_fn=lambda data: data["min_price"],
        ),
        EntsoeEntityDescription(
            key="max_price",
            name="Highest energy price today",
            native_unit_of_measurement=f"{currency}/{UnitOfEnergy.KILO_WATT_HOUR}",
            state_class=SensorStateClass.MEASUREMENT,
            value_fn=lambda data: data["max_price"],
        ),
        EntsoeEntityDescription(
            key="avg_price",
            name="Average electricity price today",
            native_unit_of_measurement=f"{currency}/{UnitOfEnergy.KILO_WATT_HOUR}",
            state_class=SensorStateClass.MEASUREMENT,
            value_fn=lambda data: data["avg_price"],
        ),
        EntsoeEntityDescription(
            key="percentage_of_max",
            name="Current percentage of highest electricity price today",
            native_unit_of_measurement=f"{PERCENTAGE}",
            icon="mdi:percent",
            state_class=SensorStateClass.MEASUREMENT,
            value_fn=lambda data: round(
                data["current_price"] / data["max_price"] * 100, 1
            ),
        ),
        EntsoeEntityDescription(
            key="highest_price_time_today",
            name="Time of highest price today",
            device_class=SensorDeviceClass.TIMESTAMP,
            value_fn=lambda data: data["time_max"],
        ),
        EntsoeEntityDescription(
            key="lowest_price_time_today",
            name="Time of lowest price today",
            device_class=SensorDeviceClass.TIMESTAMP,
            value_fn=lambda data: data["time_min"],
        ),
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ENTSO-e price sensor entries."""
    entsoe_coordinator = hass.data[DOMAIN][config_entry.entry_id][CONF_COORDINATOR]

    entities = []
    entity = {}
    for description in sensor_descriptions(currency = config_entry.options.get(CONF_CURRENCY, DEFAULT_CURRENCY)):
        entity = description
        entities.append(
            EntsoeSensor(
                entsoe_coordinator,
                entity,
                config_entry.options[CONF_ENTITY_NAME]
                ))

    # Add an entity for each sensor type
    async_add_entities(entities, True)

class EntsoeSensor(CoordinatorEntity, RestoreSensor):
    """Representation of a ENTSO-e sensor."""

    _attr_attribution = ATTRIBUTION
    _attr_icon = ICON

    def __init__(self, coordinator: EntsoeCoordinator, description: EntsoeEntityDescription, name: str = "") -> None:
        """Initialize the sensor."""
        self.description = description
        self.last_update_success = True

        if name not in (None, ""):
            #The Id used for addressing the entity in the ui, recorder history etc.
            self.entity_id = f"{DOMAIN}.{name}_{description.name}"
            #unique id in .storage file for ui configuration.
            self._attr_unique_id = f"entsoe.{name}_{description.key}"
            self._attr_name = f"{description.name} ({name})"
        else:
            self.entity_id = f"{DOMAIN}.{description.name}"
            self._attr_unique_id = f"entsoe.{description.key}"
            self._attr_name = f"{description.name}"

        self.entity_description: EntsoeEntityDescription = description

        self._update_job = HassJob(self.async_schedule_update_ha_state)
        self._unsub_update = None

        super().__init__(coordinator)

    async def async_update(self) -> None:
        """Get the latest data and updates the states."""
        #_LOGGER.debug(f"update function for '{self.entity_id} called.'")
        value: Any = None
        if self.coordinator.data is not None:
            try:
                processed = self.coordinator.processed_data()
                #_LOGGER.debug(f"current coordinator.data value: {self.coordinator.data}")
                value = self.entity_description.value_fn(processed)
                #Check if value if a panda timestamp and if so convert to an HA compatible format
                if isinstance(value, pd._libs.tslibs.timestamps.Timestamp):
                    value = value.to_pydatetime()

                self._attr_native_value = value

                if self.description.key == "avg_price" and self._attr_native_value is not None:
                    self._attr_extra_state_attributes = {
                                "prices_today": processed["prices_today"],
                                "prices_tomorrow": processed["prices_tomorrow"],
                                "prices": processed["prices"]
                            }
                    
                self.last_update_success = True
                _LOGGER.debug(f"updated '{self.entity_id}' to value: {value}")
                
            except Exception as exc:
                # No data available
                self.last_update_success = False
                _LOGGER.warning(f"Unable to update entity '{self.entity_id}' due to data processing error: {value} and error: {exc} , data: {self.coordinator.data}")

        # Cancel the currently scheduled event if there is any
        if self._unsub_update:
            self._unsub_update()
            self._unsub_update = None

        # Schedule the next update at exactly the next whole hour sharp
        self._unsub_update = event.async_track_point_in_utc_time(
            self.hass,
            self._update_job,
            utcnow().replace(minute=0, second=0) + timedelta(hours=1),
        )

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.last_update_success
