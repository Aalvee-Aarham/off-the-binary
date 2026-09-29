"""Schemas for simulator responses. Statuses stay `str` on purpose: DEPOT_CLOSED implies statuses beyond
the documented ones, and a surprise status must not take down the whole read path."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

Fuel = Literal["DIESEL", "PETROL", "OCTANE"]


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Fuels(_M):
    DIESEL: float = Field(ge=0)
    PETROL: float = Field(ge=0)
    OCTANE: float = Field(ge=0)


class Instance(_M):
    id: int
    scenario_id: str | None = None
    seed: int | None = None
    sim_time: str
    tick: int = Field(ge=0)
    tick_minutes: int = Field(gt=0)
    status: str


class Region(_M):
    id: str
    name: str
    demand_factor: float = Field(gt=0)


class Depot(_M):
    id: str
    name: str
    region_id: str
    status: str
    dispatch_capacity_per_tick: float = Field(ge=0)
    capacity: Fuels
    inventory: Fuels


class Station(_M):
    id: str
    name: str
    region_id: str
    status: str
    demand_profile: str
    demand_multiplier: float = Field(ge=0)
    capacity: Fuels
    inventory: Fuels


class Route(_M):
    id: str
    source_depot_id: str
    destination_station_id: str
    transit_ticks: int = Field(ge=0)
    max_shipment: float = Field(gt=0)
    status: str


class Supply(_M):
    id: str
    depot_id: str
    fuel_type: Fuel
    quantity: float = Field(ge=0)
    planned_tick: int
    actual_tick: int | None = None
    status: str


class Event(_M):
    id: int
    type: str
    start_tick: int
    end_tick: int
    status: str
    parameters: dict = {}


class Allocation(_M):
    id: int
    idempotency_key: str
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: Fuel
    quantity: float = Field(gt=0)
    created_tick: int
    departure_tick: int | None = None
    expected_arrival_tick: int | None = None
    actual_arrival_tick: int | None = None
    status: str
    failure_reason: str | None = None


class DemandRow(_M):
    id: int
    station_id: str
    fuel_type: Fuel
    tick: int
    sim_time: str
    demand_liters: float = Field(ge=0)
    served_liters: float = Field(ge=0)
    unmet_liters: float = Field(ge=0)


class Metrics(_M):
    served_demand_liters: float = Field(ge=0)
    unmet_demand_liters: float = Field(ge=0)
    service_level: float = Field(ge=0, le=1)
    allocation_liters: float = Field(ge=0)
    allocation_failures: int = Field(ge=0)


def adapter(model, many=False):
    return TypeAdapter(list[model] if many else model)


SCHEMAS = {
    "/v1/instance": adapter(Instance), "/v1/regions": adapter(Region, True), "/v1/depots": adapter(Depot, True),
    "/v1/stations": adapter(Station, True), "/v1/routes": adapter(Route, True),
    "/v1/supply-arrivals": adapter(Supply, True), "/v1/events": adapter(Event, True),
    "/v1/allocations": adapter(Allocation, True), "/v1/demand-history": adapter(DemandRow, True),
    "/v1/metrics": adapter(Metrics), "POST /v1/allocations": adapter(Allocation),
}
