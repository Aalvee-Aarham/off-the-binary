Target `http://localhost:8080` · backend 0.2.0 · generated 2026-09-29 13:24

| Phase | Path | VUs | Requests | RPS | Error % | avg ms | p50 | p95 | p99 | max | CPU cores | RSS MB |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | `GET /api/state` | 10 | 2393 | 478.5 | 0.00 | 20.9 | 17.1 | 24.3 | 100.9 | 649.3 | – | – |
| baseline | `GET /api/state` | 50 | 676 | 124.7 | 0.00 | 385.4 | 230.7 | 1238.9 | 1762.2 | 3654.4 | – | – |
| baseline | `GET /api/state` | 100 | 575 | 85.4 | 0.00 | 1022.0 | 661.6 | 3751.3 | 4815.8 | 5375.5 | – | – |
| baseline | `POST /api/decisions/recommend` | 1 | 52 | 10.4 | 0.00 | 96.1 | 92.2 | 128.6 | 190.3 | 190.3 | – | – |
| baseline | `POST /api/decisions/recommend` | 4 | 65 | 12.5 | 0.00 | 318.2 | 315.5 | 386.4 | 578.1 | 578.1 | – | – |
| baseline | `POST /api/decisions/recommend` | 8 | 71 | 13.6 | 0.00 | 578.5 | 563.7 | 837.9 | 852.7 | 852.7 | – | – |
| sim latency fault 500ms | `GET /api/state` | 10 | 1916 | 383.7 | 0.00 | 26.0 | 16.4 | 51.0 | 289.6 | 994.3 | – | – |
| sim latency fault 500ms | `GET /api/state` | 50 | 1340 | 260.7 | 0.00 | 188.5 | 138.0 | 536.6 | 794.5 | 1087.6 | – | – |
| sim latency fault 500ms | `GET /api/state` | 100 | 1085 | 198.2 | 0.00 | 477.8 | 338.7 | 1452.6 | 2014.6 | 3116.0 | – | – |
| sim latency fault 500ms | `POST /api/decisions/recommend` | 1 | 58 | 11.5 | 0.00 | 86.6 | 86.2 | 106.0 | 114.9 | 114.9 | – | – |
| sim latency fault 500ms | `POST /api/decisions/recommend` | 4 | 66 | 13.2 | 0.00 | 302.4 | 295.8 | 414.0 | 624.0 | 624.0 | – | – |
| sim latency fault 500ms | `POST /api/decisions/recommend` | 8 | 71 | 13.5 | 0.00 | 583.1 | 569.2 | 921.7 | 934.1 | 934.1 | – | – |
