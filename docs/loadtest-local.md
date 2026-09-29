Target `http://localhost:8080` · backend 0.2.0 · generated 2026-09-29 11:57

| Phase | Path | VUs | Requests | RPS | Error % | avg ms | p50 | p95 | p99 | max | CPU cores | RSS MB |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | `GET /api/state` | 10 | 4508 | 225.5 | 0.00 | 44.3 | 34.7 | 81.6 | 306.9 | 715.8 | – | – |
| baseline | `GET /api/state` | 50 | 2253 | 111.6 | 0.00 | 445.5 | 307.1 | 1268.6 | 2095.3 | 3346.3 | – | – |
| baseline | `GET /api/state` | 100 | 2191 | 105.2 | 0.00 | 932.1 | 675.6 | 2564.4 | 3915.9 | 6574.5 | – | – |
| baseline | `POST /api/decisions/recommend` | 1 | 125 | 6.2 | 0.00 | 160.4 | 156.0 | 207.4 | 252.3 | 266.5 | – | – |
| baseline | `POST /api/decisions/recommend` | 4 | 195 | 9.6 | 0.00 | 413.3 | 404.1 | 564.6 | 679.5 | 685.2 | – | – |
| baseline | `POST /api/decisions/recommend` | 8 | 233 | 11.4 | 0.00 | 701.2 | 694.2 | 863.8 | 1178.2 | 1223.4 | – | – |
| sim latency fault 500ms | `GET /api/state` | 10 | 5138 | 256.9 | 0.00 | 38.9 | 33.0 | 54.7 | 172.4 | 724.0 | – | – |
| sim latency fault 500ms | `GET /api/state` | 50 | 2476 | 122.2 | 0.00 | 405.9 | 289.4 | 1136.1 | 1779.8 | 2818.6 | – | – |
| sim latency fault 500ms | `GET /api/state` | 100 | 2314 | 112.7 | 0.00 | 873.2 | 625.3 | 2500.2 | 3620.7 | 6381.4 | – | – |
| sim latency fault 500ms | `POST /api/decisions/recommend` | 1 | 176 | 8.8 | 0.00 | 113.6 | 111.9 | 145.7 | 179.1 | 190.4 | – | – |
| sim latency fault 500ms | `POST /api/decisions/recommend` | 4 | 251 | 12.4 | 0.00 | 322.8 | 318.2 | 445.9 | 606.3 | 612.6 | – | – |
| sim latency fault 500ms | `POST /api/decisions/recommend` | 8 | 255 | 12.5 | 0.00 | 641.0 | 633.3 | 814.1 | 917.5 | 1254.9 | – | – |
