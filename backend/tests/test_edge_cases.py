import asyncio
import numpy as np
import pytest
import httpx
from app import config
from app.detect import Detector
from app.forecast import Forecaster, arrivals, risks
from app.models import Metrics, Event
from app.sim_client import SimClient, _error_code
from app.solvers import _lp, build_problem, check, finalize
from app.state import StateStore
from app.world import Twin, baseline_world
from app.router import state_text, route_rules


def test_detect_falsy_zero_stockout_hours():
    """Verify that hours_to_stockout = 0.0 with high p_stockout correctly raises CRITICAL alert."""
    det = Detector()
    snap = Twin(baseline_world()).snapshot()
    risk_dict = {
        ("station-mirpur", "DIESEL"): {
            "station_id": "station-mirpur", "fuel": "DIESEL", "inventory": 0.0, "capacity": 15000.0,
            "demand_4h": 500.0, "incoming": 0.0, "hours_to_stockout": 0.0, "p_stockout": 0.95,
            "sigma": 0.1, "correction": 1.0, "signals": ["immediate stockout"]
        }
    }
    fc = Forecaster()
    paths = fc.paths(snap, config.HORIZON_TICKS)
    raised, _, _ = det.run(snap, None, risk_dict, paths, fc)
    alert = next((a for a in raised if a["entity"] == "station-mirpur/DIESEL"), None)
    assert alert is not None
    assert alert["severity"] == "critical"
    assert "stockout in 0.0h" in alert["message"]


def test_lp_empty_constraints():
    """Verify that _lp handles empty constraint matrices without numpy/scipy dimension errors."""
    snap = Twin(baseline_world()).snapshot()
    fc = Forecaster()
    paths = fc.paths(snap, config.HORIZON_TICKS)
    arr = arrivals(snap, config.HORIZON_TICKS)
    r = risks(snap, fc, paths, arr, config.HORIZON_TICKS)
    P = build_problem(snap, fc, paths, arr, r)
    plan, _ = _lp(P, {})
    assert plan == []


def test_finalize_zero_and_dust_quantities():
    """Verify finalize never outputs part <= 0 shipments."""
    snap = Twin(baseline_world()).snapshot()
    raw = [("route-gazipur-mirpur", "DIESEL", 50.0), ("route-gazipur-mirpur", "DIESEL", 0.0)]
    final = finalize(raw, snap)
    assert final == []

    raw_valid = [("route-gazipur-mirpur", "DIESEL", 1200.0)]
    final_valid = finalize(raw_valid, snap)
    assert len(final_valid) == 1
    assert final_valid[0]["quantity"] == 1200.0


def test_forecast_zero_multiplier_and_unmapped_regions():
    """Verify forecast methods handle 0 multipliers and unmapped station regions gracefully."""
    snap = Twin(baseline_world()).snapshot()
    snap["stations"]["station-test"] = {
        "id": "station-test", "name": "Test Station", "region_id": "unknown-region",
        "status": "OPEN", "demand_profile": "regional", "demand_multiplier": 1.0,
        "capacity": {"DIESEL": 10000, "PETROL": 10000, "OCTANE": 5000},
        "inventory": {"DIESEL": 5000, "PETROL": 5000, "OCTANE": 2500}
    }
    snap["events"].append({
        "id": 999, "type": "demand_spike", "start_tick": 0, "end_tick": 5, "status": "ACTIVE",
        "parameters": {"multiplier": 0.0}
    })
    fc = Forecaster()
    paths = fc.paths(snap, config.HORIZON_TICKS)
    assert ("station-test", "DIESEL") in paths
    assert not np.isnan(paths[("station-test", "DIESEL")]).any()


def test_state_demand_rows_none_snap():
    """Verify StateStore.demand_rows works when self.snap is None."""
    sim = SimClient("http://sim", transport=httpx.MockTransport(lambda req: httpx.Response(200, json=[])))
    store = StateStore(sim)
    assert store.snap is None
    rows = asyncio.run(store.demand_rows(0))
    assert rows == []


def test_sim_client_direct_code_error_format():
    """Verify _error_code correctly handles direct {'code': '...', 'message': '...'} responses."""
    resp = httpx.Response(400, json={"code": "INVALID_PARAM", "message": "bad input"})
    code, msg = _error_code(resp)
    assert code == "INVALID_PARAM"
    assert msg == "bad input"


def test_router_unmapped_station_state_text():
    """Verify state_text does not crash when station has no entry in risks."""
    snap = Twin(baseline_world()).snapshot()
    snap["stations"]["station-unmapped"] = {
        "id": "station-unmapped", "status": "OPEN", "demand_multiplier": 1.0,
        "capacity": {"DIESEL": 10000, "PETROL": 10000, "OCTANE": 5000},
        "inventory": {"DIESEL": 5000, "PETROL": 5000, "OCTANE": 2500}
    }
    text = state_text(snap, set(), {}, [])
    assert "unmapped" in text


def test_check_float_precision_boundary():
    """Verify check() doesn't fail on floating-point precision jitter."""
    snap = Twin(baseline_world()).snapshot()
    ship = [{
        "source_depot_id": "depot-gazipur", "destination_station_id": "station-mirpur",
        "route_id": "route-gazipur-mirpur", "fuel_type": "DIESEL", "quantity": 6000.0
    }]
    # Station inventory 9000 + 6000 = 15000 (equal to capacity 15000)
    snap["stations"]["station-mirpur"]["inventory"]["DIESEL"] = 9000.0 + 1e-13
    snap["stations"]["station-mirpur"]["capacity"]["DIESEL"] = 15000.0
    valid, rej = check(ship, snap)
    assert len(valid) == 1
    assert not rej


def test_metrics_model_validation_tolerance():
    """Verify Metrics schema accepts 1.000001 service level without failing validation."""
    m = Metrics(
        served_demand_liters=10000.0, unmet_demand_liters=0.0, service_level=1.0005,
        allocation_liters=5000.0, allocation_failures=0
    )
    assert m.service_level == 1.0005
