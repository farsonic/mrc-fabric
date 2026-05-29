# mrc-fabric controller

Topology-driven OCP MRC 1.0 EV-profile manager. Speaks the `mrc_ctl.h` control
plane to host agents via Server-Sent Events; serves a live web UI; ingests host
counters from agents for real telemetry overlay; generates static SONiC switch
configs from the same fabric model.

## Run (Docker)
```
docker build -t mrc-controller -f controller/Dockerfile .
docker run --rm -p 9810:9810 mrc-controller
# open http://localhost:9810
```

## API
| Method | Path                       | What |
|--------|----------------------------|------|
| GET    | `/api/profile`             | EV-profile snapshot |
| GET    | `/api/profile/stream`      | SSE: push on every change (agents subscribe here) |
| POST   | `/api/profile`             | set mode, active paths, offered rate |
| POST   | `/api/path/<id>/active`    | toggle spray membership |
| POST   | `/api/path/<id>/status`    | mark good \| deny |
| POST   | `/api/metrics`             | agent posts host counters |
| GET    | `/api/telemetry`           | per-link bps (real where reported) |
| GET    | `/api/topology`            | fabric.json + flow |
| GET    | `/healthz`                 | health + agent subscriber count |

## SONiC switch config
```
python3 controller/gen_switch_config.py --topo lab/topology.clab.yaml --out lab/switch-config/
```
Produces per-switch `config_db.json` + `frr.conf` for static uSID programming.
