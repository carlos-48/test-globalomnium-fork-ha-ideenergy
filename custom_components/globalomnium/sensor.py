# Copyright (C) 2021-2022 Luis López <luis@cuarentaydos.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301,
# USA.


# TODO
# Maybe we need to mark some function as callback but I'm not sure whose.
# from homeassistant.core import callback


# Check sensor.SensorEntityDescription
# https://github.com/home-assistant/core/blob/dev/homeassistant/components/sensor/__init__.py


import itertools
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import recorder
from homeassistant.components.recorder import statistics
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    # UnitOfEnergy,
    # UnitOfPower,
    # UnitOfWater, #no implementado en HA
    UnitOfVolume
)
from homeassistant.core import HomeAssistant, callback, dt_util
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import DiscoveryInfoType
from homeassistant.util import dt as dtutil
from homeassistant_historical_sensor import HistoricalSensor, HistoricalState

from .const import DOMAIN
from .datacoordinator import (
    DATA_ATTR_HISTORICAL_CONSUMPTION,
    # DATA_ATTR_HISTORICAL_GENERATION,
    # DATA_ATTR_HISTORICAL_POWER_DEMAND,
    DATA_ATTR_MEASURE_ACCUMULATED,
    DATA_ATTR_MEASURE_INSTANT,
    DataSetType,
)
from .entity import GOEntity
from .fixes import async_fix_statistics

PLATFORM = "sensor"

MAINLAND_SPAIN_ZONEINFO = dtutil.zoneinfo.ZoneInfo("Europe/Madrid")
_LOGGER = logging.getLogger(__name__)


# The GOSensor class provides:
#     __init__
#     __repr__
#     name
#     unique_id
#     device_info
#     entity_registry_enabled_default
# The CoordinatorEntity class provides:
#     should_poll
#     async_update
#     async_added_to_hass
#     available


class HistoricalSensorMixin(HistoricalSensor):
    @callback
    def _handle_coordinator_update(self) -> None:
        self.hass.add_job(self.async_write_ha_historical_states())

    def async_update_historical(self) -> None:
        pass


class StatisticsMixin(HistoricalSensor):
    @property
    def statistic_id(self):
        return self.entity_id

    def get_statistic_metadata(self) -> StatisticMetaData:
        meta = super().get_statistic_metadata() | {"has_sum": True}

        return meta

    async def async_added_to_hass(self):
        await super().async_added_to_hass()

        #
        # In 2.0 branch we f**ked statistiscs.
        # Don't set state_class attributes for historical sensors!
        #
        # FIXME: Remove in future 3.0 series.
        #
        await async_fix_statistics(self.hass, self.get_statistic_metadata())

    async def async_calculate_statistic_data(
        self, hist_states: list[HistoricalState], *, latest: dict | None
    ) -> list[StatisticData]:
        #
        # Filter out invalid states
        #

        n_original_hist_states = len(hist_states)
        hist_states = [x for x in hist_states if x.state not in (0, None)]
        if len(hist_states) != n_original_hist_states:
            _LOGGER.warning(
                f"{self.statistic_id}: "
                + "found some weird values in historical statistics"
            )

        #
        # Group historical states by hour block
        #

        def hour_block_for_hist_state(hist_state: HistoricalState) -> datetime:
            # XX:00:00 states belongs to previous hour block
            if hist_state.dt.minute == 0 and hist_state.dt.second == 0:
                dt = hist_state.dt - timedelta(hours=1)
                return dt.replace(minute=0, second=0, microsecond=0)

            else:
                return hist_state.dt.replace(minute=0, second=0, microsecond=0)

        #
        # Ignore supplied 'lastest' and fetch again from recorder
        # FIXME: integrate into homeassistant_historical_sensor and remove
        #

        def get_last_statistics():
            ret = statistics.get_last_statistics(
                self.hass,
                1,
                self.statistic_id,
                convert_units=True,
                types={"sum"},
            )

            # ret can be none or {}
            if not ret:
                return None

            try:
                return ret[self.statistic_id][0]

            except KeyError:
                # No stats found
                return None

            except IndexError:
                # What?
                _LOGGER.error(
                    f"{self.statatistic_id}: "
                    + "[bug] found last statistics key but doesn't have any value! "
                    + f"({ret!r})"
                )
                raise

        latest = await recorder.get_instance(self.hass).async_add_executor_job(
            get_last_statistics
        )

        #
        # Get last sum sum from latest
        #
        def extract_last_sum(latest) -> float:
            return float(latest["sum"]) if latest else 0

        try:
            total_accumulated = extract_last_sum(latest)
        except (KeyError, ValueError):
            _LOGGER.error(
                f"{self.statistic_id}: [bug] statistics broken (lastest={latest!r})"
            )
            return []

        start_point_local_dt = dt_util.as_local(
            dt_util.utc_from_timestamp(latest.get("start", 0) if latest else 0)
        )

        _LOGGER.debug(
            f"{self.statistic_id}: "
            + f"calculating statistics using {total_accumulated} as base accumulated "
            + f"(registed at {start_point_local_dt})"
        )

        #
        # Calculate statistic data
        #

        ret = []

        for dt, collection_it in itertools.groupby(
            hist_states, key=hour_block_for_hist_state
        ):
            collection = list(collection_it)

            # hour_mean = statistics.mean([x.state for x in collection])
            hour_accumulated = sum([x.state for x in collection])
            total_accumulated = total_accumulated + hour_accumulated

            ret.append(
                StatisticData(
                    start=dt,
                    state=hour_accumulated,
                    # mean=hour_mean,
                    sum=total_accumulated,
                )
            )

        return ret


class AccumulatedConsumption(RestoreEntity, GOEntity, SensorEntity):
    GO_PLATFORM = PLATFORM
    GO_ENTITY_NAME = "Accumulated Consumption"
    GO_DATA_SETS = [DataSetType.MEASURE]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._attr_device_class = SensorDeviceClass.WATER
        self._attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS

        # TOTAL vs TOTAL_INCREASING:
        #
        # It's recommended to use state class total without last_reset whenever
        # possible, state class total_increasing or total with last_reset should only be
        # used when state class total without last_reset does not work for the sensor.
        # https://developers.home-assistant.io/docs/core/entity/sensor/#how-to-choose-state_class-and-last_reset

        # The sensor's value never resets, e.g. a lifetime total energy consumption or
        # production: state_class total, last_reset not set or set to None

        self._attr_state_class = SensorStateClass.TOTAL

    @property
    def state(self):
        if self.coordinator.data is None:
            return None

        return self.coordinator.data[DATA_ATTR_MEASURE_ACCUMULATED]

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        saved_data = await async_get_last_state_safe(self, float)
        self.coordinator.update_internal_data(
            {DATA_ATTR_MEASURE_ACCUMULATED: saved_data}
        )


# class InstantPowerDemand(RestoreEntity, IDeEntity, SensorEntity):
#     I_DE_PLATFORM = PLATFORM
#     I_DE_ENTITY_NAME = "Instant Power Demand"
#     I_DE_DATA_SETS = [DataSetType.MEASURE]
# 
#     def __init__(self, *args, **kwargs):
#        super().__init__(*args, **kwargs)
#         self._attr_device_class = SensorDeviceClass.POWER
#         self._attr_state_class = SensorStateClass.MEASUREMENT
#         self._attr_native_unit_of_measurement = UnitOfPower.WATT
# 
#     @property
#     def state(self):
#         if self.coordinator.data is None:
#             return None
# 
#         return self.coordinator.data[DATA_ATTR_MEASURE_INSTANT]
# 
#     @callback
#     def _handle_coordinator_update(self) -> None:
#         self.async_write_ha_state()
# 
#     async def async_added_to_hass(self) -> None:
#         await super().async_added_to_hass()
# 
#         saved_data = await async_get_last_state_safe(self, float)
#         self.coordinator.update_internal_data({DATA_ATTR_MEASURE_INSTANT: saved_data})
# 
# 
class HistoricalConsumption(
    StatisticsMixin, HistoricalSensorMixin, GOEntity, SensorEntity
):
    GO_PLATFORM = PLATFORM
    GO_ENTITY_NAME = "Historical Consumption"
    GO_DATA_SETS = [DataSetType.HISTORICAL_CONSUMPTION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._attr_device_class = SensorDeviceClass.WATER
        self._attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
        self._attr_entity_registry_enabled_default = False
        self._attr_state = None

        # The sensor's state is reset with every state update, for example a sensor
        # updating every minute with the energy consumption during the past minute:
        # state class total, last_reset updated every state change.
        #
        # (*) last_reset is set in states by historical_states_from_historical_api_data
        # (*) set only in internal statistics model
        #
        # DON'T set for HistoricalSensors, you will mess your statistics.
        # Keep as reference.
        #
        # self._attr_state_class = SensorStateClass.TOTAL

    @property
    def historical_states(self):
        ret = historical_states_from_historical_api_data(
            self.coordinator.data[DATA_ATTR_HISTORICAL_CONSUMPTION]["historical"]
        )

        return ret


# class HistoricalGeneration(
#     StatisticsMixin, HistoricalSensorMixin, IDeEntity, SensorEntity
# ):
#     I_DE_PLATFORM = PLATFORM
#     I_DE_ENTITY_NAME = "Historical Generation"
#     I_DE_DATA_SETS = [DataSetType.HISTORICAL_GENERATION]
# 
#     def __init__(self, *args, **kwargs):
#         super().__init__(*args, **kwargs)
#         self._attr_device_class = SensorDeviceClass.ENERGY
#         self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
#         self._attr_entity_registry_enabled_default = False
#         self._attr_state = None
# 
#         # The sensor's state is reset with every state update, for example a sensor
#         # updating every minute with the energy consumption during the past minute:
#         # state class total, last_reset updated every state change.
#         #
#         # (*) last_reset is set in states by historical_states_from_historical_api_data
#         # (*) set only in internal statistics model
#         #
#         # DON'T set for HistoricalSensors, you will mess your statistics.
#         #
#         # Keep as reference.
#         #
#         # self._attr_state_class = SensorStateClass.TOTAL
# 
#     @property
#     def historical_states(self):
#         ret = historical_states_from_historical_api_data(
#             self.coordinator.data[DATA_ATTR_HISTORICAL_GENERATION]["historical"]
#         )
# 
#         return ret
# 
# 
# class HistoricalPowerDemand(HistoricalSensorMixin, IDeEntity, SensorEntity):
#     I_DE_PLATFORM = PLATFORM
#     I_DE_ENTITY_NAME = "Historical Power Demand"
#     I_DE_DATA_SETS = [DataSetType.HISTORICAL_POWER_DEMAND]
# 
#     def __init__(self, *args, **kwargs):
#         super().__init__(*args, **kwargs)
#         self._attr_device_class = SensorDeviceClass.POWER
#         self._attr_native_unit_of_measurement = UnitOfPower.WATT
#         self._attr_entity_registry_enabled_default = False
#         self._attr_state = None
# 
#     @property
#     def historical_states(self):
#         def _convert_item(item):
#             # [
#             #     {
#             #         "dt": datetime.datetime(2021, 4, 24, 13, 0),
#             #         "value": 3012.0
#             #     },
#             #     ...
#             # ]
#             return HistoricalState(
#                 state=item["value"] / 1000,
#                 dt=item["dt"].replace(tzinfo=MAINLAND_SPAIN_ZONEINFO),
#             )
# 
#         data = self.coordinator.data[DATA_ATTR_HISTORICAL_POWER_DEMAND]
#         ret = [_convert_item(item) for item in data]
# 
#         return ret
# 
# 
async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_devices: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,  # noqa DiscoveryInfoType | None
):
    coordinator, device_info = hass.data[DOMAIN][config_entry.entry_id]

    sensors = [
        AccumulatedConsumption(
            config_entry=config_entry, device_info=device_info, coordinator=coordinator
        ),
#         InstantPowerDemand(
#             config_entry=config_entry, device_info=device_info, coordinator=coordinator
#         ),
        HistoricalConsumption(
            config_entry=config_entry, device_info=device_info, coordinator=coordinator
        ),
#         HistoricalGeneration(
#             config_entry=config_entry, device_info=device_info, coordinator=coordinator
#         ),
#         HistoricalPowerDemand(
#             config_entry=config_entry, device_info=device_info, coordinator=coordinator
#         ),
    ]
    async_add_devices(sensors)


def historical_states_from_historical_api_data(
    data: list[dict] | None = None,
) -> list[HistoricalState]:
    def _convert_item(item):
        # FIXME: What about canary islands?
        dt = item["end"].replace(tzinfo=MAINLAND_SPAIN_ZONEINFO)
        last_reset = item["start"].replace(tzinfo=MAINLAND_SPAIN_ZONEINFO)

        return HistoricalState(
            state=item["value"] / 1000, #¿debo dividir entre mil? ya veré si para GO necesito este cambio de unidad, creo que si
            dt=dt,
            attributes={"last_reset": last_reset},
        )

    return [_convert_item(item) for item in data or []]


async def async_get_last_state_safe(
    entity: RestoreEntity, convert_fn: Callable[[Any], Any]
) -> Any:
    # Try to load previous state using RestoreEntity
    #
    # self.async_get_last_state().last_update is tricky and can't be trusted in our
    # scenario. last_updated can be the last time HA exited because state is saved
    # at exit with last_updated=exit_time, not last_updated=sensor_last_update
    #
    # It's easier to just load the value and schedule an update with
    # schedule_update_ha_state() (which is meant for push sensors but...)

    state = await entity.async_get_last_state()
    if state is None:
        _LOGGER.debug(f"{entity.entity_id}: restore state failed (no state)")
        return None

    if state.state in [STATE_UNKNOWN, STATE_UNAVAILABLE]:
        _LOGGER.debug(f"{entity.entity_id}: restore state failed ({state.state})")
        return None

    try:
        return convert_fn(state.state)

    except (TypeError, ValueError):
        sttype = type(state.state)
        _LOGGER.debug(
            f"{entity.entity_id}: restore state failed "
            + f"(incompatible. type='{sttype}', value='{state.state!r}')"
        )
        return None
