const http = require('http');
const fs = require('fs');
const path = require('path');
const url = require('url');

// Baseline state
const baselineState = {
  instance: {
    id: 1,
    scenario_id: "baseline",
    scenario_version: "1.0",
    seed: 12345,
    sim_time: "2026-01-01T00:00:00+00:00",
    tick: 0,
    tick_minutes: 15,
    status: "PAUSED"
  },
  regions: [
    { id: "region-dhaka", name: "Dhaka Division", demand_factor: 1.00 },
    { id: "region-chattogram", name: "Chattogram Division", demand_factor: 1.08 }
  ],
  depots: [
    {
      id: "depot-gazipur",
      name: "Gazipur Depot",
      region_id: "region-dhaka",
      status: "OPEN",
      dispatch_capacity_per_tick: 12000,
      capacity: { DIESEL: 90000, PETROL: 70000, OCTANE: 45000 },
      inventory: { DIESEL: 60000, PETROL: 45000, OCTANE: 26000 }
    },
    {
      id: "depot-patiya",
      name: "Patiya Depot",
      region_id: "region-chattogram",
      status: "OPEN",
      dispatch_capacity_per_tick: 11000,
      capacity: { DIESEL: 85000, PETROL: 65000, OCTANE: 40000 },
      inventory: { DIESEL: 55000, PETROL: 42000, OCTANE: 24000 }
    }
  ],
  stations: [
    {
      id: "station-mirpur",
      name: "Mirpur Fuel Station",
      region_id: "region-dhaka",
      status: "OPEN",
      demand_profile: "urban_high",
      demand_multiplier: 1.0,
      capacity: { DIESEL: 15000, PETROL: 14000, OCTANE: 9000 },
      inventory: { DIESEL: 9000, PETROL: 9000, OCTANE: 5000 }
    },
    {
      id: "station-tongi",
      name: "Tongi Industrial Station",
      region_id: "region-dhaka",
      status: "OPEN",
      demand_profile: "industrial",
      demand_multiplier: 1.0,
      capacity: { DIESEL: 18000, PETROL: 9000, OCTANE: 6000 },
      inventory: { DIESEL: 11000, PETROL: 6000, OCTANE: 3500 }
    },
    {
      id: "station-karnaphuli",
      name: "Karnaphuli Highway Station",
      region_id: "region-chattogram",
      status: "OPEN",
      demand_profile: "highway",
      demand_multiplier: 1.0,
      capacity: { DIESEL: 14000, PETROL: 15000, OCTANE: 9000 },
      inventory: { DIESEL: 8500, PETROL: 9500, OCTANE: 5200 }
    },
    {
      id: "station-coxsbazar",
      name: "Cox's Bazar Regional Station",
      region_id: "region-chattogram",
      status: "OPEN",
      demand_profile: "regional",
      demand_multiplier: 1.0,
      capacity: { DIESEL: 12000, PETROL: 12000, OCTANE: 7000 },
      inventory: { DIESEL: 7500, PETROL: 7500, OCTANE: 4200 }
    }
  ],
  routes: [
    { id: "route-gazipur-mirpur", source_depot_id: "depot-gazipur", destination_station_id: "station-mirpur", transit_ticks: 2, max_shipment: 7000, status: "AVAILABLE" },
    { id: "route-gazipur-tongi", source_depot_id: "depot-gazipur", destination_station_id: "station-tongi", transit_ticks: 2, max_shipment: 6500, status: "AVAILABLE" },
    { id: "route-patiya-karnaphuli", source_depot_id: "depot-patiya", destination_station_id: "station-karnaphuli", transit_ticks: 2, max_shipment: 7000, status: "AVAILABLE" },
    { id: "route-patiya-coxsbazar", source_depot_id: "depot-patiya", destination_station_id: "station-coxsbazar", transit_ticks: 3, max_shipment: 6000, status: "AVAILABLE" },
    { id: "route-gazipur-karnaphuli", source_depot_id: "depot-gazipur", destination_station_id: "station-karnaphuli", transit_ticks: 4, max_shipment: 5000, status: "AVAILABLE" },
    { id: "route-patiya-mirpur", source_depot_id: "depot-patiya", destination_station_id: "station-mirpur", transit_ticks: 4, max_shipment: 5000, status: "AVAILABLE" }
  ],
  supply_arrivals: [
    { id: "supply-001", depot_id: "depot-gazipur", fuel_type: "DIESEL", quantity: 18000, planned_tick: 12, actual_tick: null, status: "SCHEDULED" },
    { id: "supply-002", depot_id: "depot-gazipur", fuel_type: "PETROL", quantity: 14000, planned_tick: 16, actual_tick: null, status: "SCHEDULED" },
    { id: "supply-003", depot_id: "depot-patiya", fuel_type: "DIESEL", quantity: 16000, planned_tick: 14, actual_tick: null, status: "SCHEDULED" },
    { id: "supply-004", depot_id: "depot-patiya", fuel_type: "OCTANE", quantity: 8000, planned_tick: 20, actual_tick: null, status: "SCHEDULED" }
  ],
  allocations: [],
  demand_observations: [],
  events: [],
  faults: [],
  audit_logs: [
    {
      id: 1,
      wall_time: new Date().toISOString(),
      sim_time: "2026-01-01T00:00:00+00:00",
      tick: 0,
      action: "simulation.reset",
      entity_type: "simulation",
      entity_id: "1",
      result: "OK",
      metadata_json: { scenario: "baseline" }
    }
  ]
};

let state = JSON.parse(JSON.stringify(baselineState));
let tickTimer = null;

function stepTick() {
  state.instance.tick += 1;
  const totalMins = state.instance.tick * state.instance.tick_minutes;
  const d = new Date(Date.parse("2026-01-01T00:00:00Z") + totalMins * 60 * 1000);
  state.instance.sim_time = d.toISOString();

  // Generate simulated demand & consumption
  for (const st of state.stations) {
    const baseDemand = { DIESEL: 120, PETROL: 100, OCTANE: 50 };
    for (const fuel of ["DIESEL", "PETROL", "OCTANE"]) {
      const demand = Math.round(baseDemand[fuel] * (0.8 + Math.random() * 0.4) * (st.demand_multiplier || 1.0));
      const served = Math.min(st.inventory[fuel], demand);
      const unmet = demand - served;
      st.inventory[fuel] = Math.max(0, st.inventory[fuel] - served);
      state.demand_observations.push({
        id: state.demand_observations.length + 1,
        station_id: st.id,
        fuel_type: fuel,
        tick: state.instance.tick,
        sim_time: state.instance.sim_time,
        demand_liters: demand,
        served_liters: served,
        unmet_liters: unmet
      });
    }
  }

  // Check supply arrivals
  for (const sa of state.supply_arrivals) {
    if (sa.status === "SCHEDULED" && sa.planned_tick <= state.instance.tick) {
      sa.status = "ARRIVED";
      sa.actual_tick = state.instance.tick;
      const depot = state.depots.find(d => d.id === sa.depot_id);
      if (depot) {
        depot.inventory[sa.fuel_type] = Math.min(depot.capacity[sa.fuel_type], depot.inventory[sa.fuel_type] + sa.quantity);
      }
      state.audit_logs.unshift({
        id: state.audit_logs.length + 1,
        wall_time: new Date().toISOString(),
        sim_time: state.instance.sim_time,
        tick: state.instance.tick,
        action: "supply.arrived",
        entity_type: "supply_arrival",
        entity_id: sa.id,
        result: "OK",
        metadata_json: { quantity: sa.quantity, fuel_type: sa.fuel_type, depot_id: sa.depot_id }
      });
    }
  }

  // Update in-transit allocations
  for (const alloc of state.allocations) {
    if (alloc.status === "PENDING" && alloc.departure_tick == null) {
      alloc.departure_tick = state.instance.tick;
      alloc.status = "IN_TRANSIT";
      const route = state.routes.find(r => r.id === alloc.route_id);
      alloc.expected_arrival_tick = state.instance.tick + (route ? route.transit_ticks : 2);
    } else if (alloc.status === "IN_TRANSIT" && alloc.expected_arrival_tick <= state.instance.tick) {
      alloc.status = "ARRIVED";
      alloc.actual_arrival_tick = state.instance.tick;
      const station = state.stations.find(s => s.id === alloc.destination_station_id);
      if (station) {
        station.inventory[alloc.fuel_type] = Math.min(station.capacity[alloc.fuel_type], station.inventory[alloc.fuel_type] + alloc.quantity);
      }
    }
  }

  state.audit_logs.unshift({
    id: state.audit_logs.length + 1,
    wall_time: new Date().toISOString(),
    sim_time: state.instance.sim_time,
    tick: state.instance.tick,
    action: "simulation.tick",
    entity_type: "simulation",
    entity_id: "1",
    result: "OK",
    metadata_json: { tick: state.instance.tick }
  });

  if (state.audit_logs.length > 200) state.audit_logs.length = 200;
}

function calcMetrics() {
  let served = 0, unmet = 0;
  for (const obs of state.demand_observations) {
    served += obs.served_liters;
    unmet += obs.unmet_liters;
  }
  const total = served + unmet;
  let allocated = 0, failures = 0;
  for (const a of state.allocations) {
    if (a.status === "IN_TRANSIT" || a.status === "ARRIVED") allocated += a.quantity;
    if (a.status === "FAILED") failures += 1;
  }
  return {
    served_demand_liters: Math.round(served * 10) / 10,
    unmet_demand_liters: Math.round(unmet * 10) / 10,
    service_level: total ? Math.round((served / total) * 1000000) / 1000000 : 1.0,
    allocation_liters: Math.round(allocated * 10) / 10,
    allocation_failures: failures
  };
}

// Read ADMIN_HTML_TEMPLATE from main.py
const mainPyPath = path.join(__dirname, 'simulator_extracted/app/app/main.py');
let adminTemplate = '';
if (fs.existsSync(mainPyPath)) {
  const content = fs.readFileSync(mainPyPath, 'utf8');
  const startIdx = content.indexOf('ADMIN_HTML_TEMPLATE = r"""<!doctype html>');
  if (startIdx !== -1) {
    const endMarker = '"""';
    const htmlStart = content.indexOf('<!doctype html>', startIdx);
    const htmlEnd = content.indexOf(endMarker, htmlStart);
    adminTemplate = content.substring(htmlStart, htmlEnd);
  }
}

// Swagger UI HTML
const swaggerHtml = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>BUP Fuel Supply Simulator - Swagger UI</title>
  <link rel="stylesheet" type="text/css" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css" />
  <style>
    body { margin: 0; background: #fafafa; }
    .topbar { display: none !important; }
  </style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    window.onload = function() {
      SwaggerUIBundle({
        url: "/openapi.json",
        dom_id: '#swagger-ui',
        deepLinking: true,
        presets: [
          SwaggerUIBundle.presets.apis,
          SwaggerUIBundle.SwaggerUIStandalonePreset
        ]
      });
    };
  </script>
</body>
</html>`;

const openapiSpec = {
  openapi: "3.1.0",
  info: {
    title: "BUP Fuel Supply Simulator API",
    version: "1.0.0",
    description: "Deterministic local fuel supply chain simulator for BUP CSE Fest Hackathon. Models depots, stations, routes, and allocations across Dhaka and Chattogram."
  },
  paths: {
    "/v1/health": {
      get: { summary: "Health check", tags: ["Public"], responses: { "200": { description: "OK" } } }
    },
    "/v1/instance": {
      get: { summary: "Get simulation instance status & tick clock", tags: ["Public"], responses: { "200": { description: "Instance status" } } }
    },
    "/v1/regions": {
      get: { summary: "List regions (Dhaka, Chattogram)", tags: ["Public"], responses: { "200": { description: "List of regions" } } }
    },
    "/v1/depots": {
      get: { summary: "List depots (Gazipur, Patiya)", tags: ["Public"], responses: { "200": { description: "Depots list with inventory" } } }
    },
    "/v1/stations": {
      get: { summary: "List fuel stations with capacities & inventory", tags: ["Public"], responses: { "200": { description: "Stations list" } } }
    },
    "/v1/routes": {
      get: { summary: "List distribution routes & transit ticks", tags: ["Public"], responses: { "200": { description: "Routes list" } } }
    },
    "/v1/supply-arrivals": {
      get: { summary: "List scheduled & arrived refinery deliveries", tags: ["Public"], responses: { "200": { description: "Supply arrivals list" } } }
    },
    "/v1/allocations": {
      get: { summary: "List submitted fuel allocations & transit status", tags: ["Public"], responses: { "200": { description: "Allocations" } } },
      post: {
        summary: "Dispatch fuel replenishment (the only domain write)",
        tags: ["Public"],
        requestBody: {
          content: {
            "application/json": {
              schema: {
                type: "object",
                properties: {
                  idempotency_key: { type: "string", example: "alloc-001" },
                  source_depot_id: { type: "string", example: "depot-gazipur" },
                  destination_station_id: { type: "string", example: "station-mirpur" },
                  route_id: { type: "string", example: "route-gazipur-mirpur" },
                  fuel_type: { type: "string", enum: ["DIESEL", "PETROL", "OCTANE"], example: "DIESEL" },
                  quantity: { type: "number", example: 3000 }
                },
                required: ["idempotency_key", "source_depot_id", "destination_station_id", "route_id", "fuel_type", "quantity"]
              }
            }
          }
        },
        responses: { "201": { description: "Created" } }
      }
    },
    "/v1/metrics": {
      get: { summary: "Aggregated ground-truth performance metrics", tags: ["Public"], responses: { "200": { description: "Metrics" } } }
    },
    "/v1/stream": {
      get: { summary: "Server-Sent Events (SSE) notification stream", tags: ["Public"], responses: { "200": { description: "Event stream" } } }
    },
    "/admin": {
      get: { summary: "Web-based admin console dashboard", tags: ["Admin"], responses: { "200": { description: "Admin HTML" } } }
    },
    "/admin/run": {
      post: { summary: "Start simulation background runner", tags: ["Admin"], responses: { "200": { description: "Running" } } }
    },
    "/admin/pause": {
      post: { summary: "Pause simulation clock", tags: ["Admin"], responses: { "200": { description: "Paused" } } }
    },
    "/admin/toggle": {
      post: { summary: "Toggle running / paused", tags: ["Admin"], responses: { "200": { description: "Toggled" } } }
    },
    "/admin/step": {
      post: { summary: "Step forward exactly 1 tick (15 minutes)", tags: ["Admin"], responses: { "200": { description: "Stepped" } } }
    },
    "/admin/reset": {
      post: { summary: "Reset world back to baseline scenario", tags: ["Admin"], responses: { "200": { description: "Reset" } } }
    },
    "/admin/events": {
      post: { summary: "Inject crisis event (demand spike, outage, etc.)", tags: ["Admin"], responses: { "201": { description: "Event injected" } } }
    },
    "/admin/faults": {
      post: { summary: "Inject system fault (latency, unavailable, error_rate)", tags: ["Admin"], responses: { "201": { description: "Fault injected" } } }
    }
  }
};

const server = http.createServer((req, res) => {
  const parsed = url.parse(req.url, true);
  const pathname = parsed.pathname;

  // JSON helper
  const sendJson = (code, data) => {
    res.writeHead(code, {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*'
    });
    res.end(JSON.stringify(data, null, 2));
  };

  // CORS preflight
  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type'
    });
    res.end();
    return;
  }

  // Swagger docs
  if (pathname === '/docs') {
    res.writeHead(200, { 'Content-Type': 'text/html' });
    res.end(swaggerHtml);
    return;
  }
  if (pathname === '/openapi.json') {
    sendJson(200, openapiSpec);
    return;
  }

  // Admin page
  if (pathname === '/admin' && req.method === 'GET') {
    const instJson = JSON.stringify(state.instance, null, 2);
    const metricsJson = JSON.stringify(calcMetrics(), null, 2);
    const auditJson = JSON.stringify(state.audit_logs.slice(0, 10), null, 2);
    const statsJson = JSON.stringify({
      row_counts: {
        regions: state.regions.length,
        depots: state.depots.length,
        stations: state.stations.length,
        routes: state.routes.length,
        supply_arrivals: state.supply_arrivals.length,
        demand_observations: state.demand_observations.length,
        allocations: state.allocations.length,
        events: state.events.length,
        faults: state.faults.length,
        audit_logs: state.audit_logs.length
      },
      db_path: "/app/data/fuel_simulator.db",
      db_bytes: 40960,
      database_url: "sqlite:////app/data/fuel_simulator.db",
      scenario_file: "scenarios/baseline.yaml"
    }, null, 2);

    const rendered = adminTemplate
      .replace("__INST_JSON__", instJson)
      .replace("__METRICS_JSON__", metricsJson)
      .replace("__AUDIT_JSON__", auditJson)
      .replace("__STATS_JSON__", statsJson);

    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(rendered);
    return;
  }

  // API endpoints
  if (pathname === '/v1/health') {
    sendJson(200, {
      status: "ok",
      database: "ok",
      simulation: { status: state.instance.status, tick: state.instance.tick }
    });
    return;
  }

  if (pathname === '/v1/instance') {
    sendJson(200, state.instance);
    return;
  }

  if (pathname === '/v1/metrics') {
    sendJson(200, calcMetrics());
    return;
  }

  if (pathname === '/v1/regions') {
    sendJson(200, state.regions);
    return;
  }

  if (pathname === '/v1/depots') {
    sendJson(200, state.depots);
    return;
  }

  if (pathname === '/v1/stations') {
    sendJson(200, state.stations);
    return;
  }

  if (pathname === '/v1/routes') {
    sendJson(200, state.routes);
    return;
  }

  if (pathname === '/v1/supply-arrivals') {
    sendJson(200, state.supply_arrivals);
    return;
  }

  if (pathname === '/v1/allocations' && req.method === 'GET') {
    sendJson(200, state.allocations);
    return;
  }

  if (pathname === '/admin/stats') {
    sendJson(200, {
      row_counts: {
        regions: state.regions.length,
        depots: state.depots.length,
        stations: state.stations.length,
        routes: state.routes.length,
        supply_arrivals: state.supply_arrivals.length,
        demand_observations: state.demand_observations.length,
        allocations: state.allocations.length,
        events: state.events.length,
        faults: state.faults.length,
        audit_logs: state.audit_logs.length
      }
    });
    return;
  }

  if (pathname === '/admin/audit') {
    const limit = parseInt(parsed.query.limit || '10', 10);
    sendJson(200, state.audit_logs.slice(0, limit));
    return;
  }

  if (pathname === '/admin/toggle' && req.method === 'POST') {
    if (state.instance.status === 'RUNNING') {
      state.instance.status = 'PAUSED';
      if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
    } else {
      state.instance.status = 'RUNNING';
      if (!tickTimer) {
        tickTimer = setInterval(stepTick, 1000);
      }
    }
    sendJson(200, { status: state.instance.status });
    return;
  }

  if (pathname === '/admin/step' && req.method === 'POST') {
    stepTick();
    sendJson(200, { tick: state.instance.tick, sim_time: state.instance.sim_time });
    return;
  }

  if (pathname === '/admin/reset' && req.method === 'POST') {
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
    state = JSON.parse(JSON.stringify(baselineState));
    sendJson(200, { status: "reset" });
    return;
  }

  // Parse body for POST requests
  let bodyStr = '';
  req.on('data', chunk => bodyStr += chunk);
  req.on('end', () => {
    let body = {};
    try { if (bodyStr) body = JSON.parse(bodyStr); } catch (e) {}

    if (pathname === '/admin/events' && req.method === 'POST') {
      const evt = {
        id: state.events.length + 1,
        type: body.type || 'demand_spike',
        start_tick: body.start_tick || 0,
        end_tick: (body.start_tick || 0) + (body.duration_ticks || 4),
        status: "ACTIVE",
        parameters: body.parameters || {}
      };
      state.events.unshift(evt);

      // Apply effect immediately if demand_spike
      if (evt.type === 'demand_spike' && evt.parameters.multiplier) {
        for (const st of state.stations) {
          st.demand_multiplier = (st.demand_multiplier || 1.0) * evt.parameters.multiplier;
        }
      }

      state.audit_logs.unshift({
        id: state.audit_logs.length + 1,
        wall_time: new Date().toISOString(),
        sim_time: state.instance.sim_time,
        tick: state.instance.tick,
        action: "event.started",
        entity_type: "event",
        entity_id: String(evt.id),
        result: "OK",
        metadata_json: evt
      });

      sendJson(201, evt);
      return;
    }

    if (pathname === '/admin/faults' && req.method === 'POST') {
      const ft = {
        id: state.faults.length + 1,
        type: body.type || 'latency',
        duration_seconds: body.duration_seconds || 60,
        parameters: body.parameters || {},
        active: true
      };
      state.faults.unshift(ft);
      sendJson(201, ft);
      return;
    }

    if (pathname === '/admin/faults/clear' && req.method === 'POST') {
      state.faults = [];
      sendJson(200, { status: "cleared" });
      return;
    }

    if (pathname === '/v1/allocations' && req.method === 'POST') {
      const alloc = {
        id: state.allocations.length + 1,
        idempotency_key: body.idempotency_key || ("alloc-" + Date.now()),
        source_depot_id: body.source_depot_id,
        destination_station_id: body.destination_station_id,
        route_id: body.route_id,
        fuel_type: body.fuel_type,
        quantity: body.quantity,
        created_tick: state.instance.tick,
        departure_tick: null,
        expected_arrival_tick: null,
        actual_arrival_tick: null,
        status: "PENDING",
        failure_reason: null
      };

      // Deduct depot inventory
      const depot = state.depots.find(d => d.id === body.source_depot_id);
      if (depot && depot.inventory[body.fuel_type] >= body.quantity) {
        depot.inventory[body.fuel_type] -= body.quantity;
      }

      state.allocations.unshift(alloc);
      sendJson(201, alloc);
      return;
    }

    // Default redirect to /admin
    if (pathname === '/' || pathname === '') {
      res.writeHead(302, { 'Location': '/admin' });
      res.end();
      return;
    }

    sendJson(404, { detail: { code: "NOT_FOUND", message: "Path not found" } });
  });
});

const PORT = 8000;
server.listen(PORT, () => {
  console.log(`Fuel Simulator Server listening at http://localhost:${PORT}`);
  console.log(`- Admin Console: http://localhost:${PORT}/admin`);
  console.log(`- Swagger UI: http://localhost:${PORT}/docs`);
});
