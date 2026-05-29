# mrc-fabric

> An OCP MRC 1.0 fabric in a box — a containerized controller, a host-side
> virtual NIC agent, and a SONiC SRv6/uSID lab to run them against.

Two cooperating components implementing the
[OCP Multipath Reliable Connection 1.0](https://www.opencompute.org/documents/ocp-mrc-1-0-pdf)
control plane on a community SONiC + containerlab fabric:

```
                           ┌────────────────────────────┐
                           │      mrc-controller        │   (Docker, Flask + SSE)
                           │  EV-profile manager + UI   │
                           └─────────┬──────────┬───────┘
                       SSE: profile  │          │  POST: host counters
                                     ▼          ▲
                  ┌──────────────────────────────────────────┐
                  │              mrc-agent                   │   (one per host)
                  │   owns `mrc0` virtual NIC + seg6 routes  │
                  └──────────────────┬───────────────────────┘
                                     │  ip -6 route ... encap seg6 ...
                                     ▼
   ┌─────────────────────────────────────────────────────────────────────────┐
   │     SONiC fabric (containerlab) — static uSID SIDs, no dynamic CP      │
   │       spine00 ─── spine01                                              │
   │          \   X   /                                                     │
   │       leaf00 ─── leaf01                                                │
   │         │           │                                                  │
   │       host00      host01     (the agent runs here)                     │
   └─────────────────────────────────────────────────────────────────────────┘
```

**The split that matters.** Apps stay MRC-unaware — they open a plain socket
to a peer tenant address. The agent is the MRC endpoint: it holds a persistent
control-plane subscription to the controller and reprograms the host's seg6
encap state in real time. The controller never reaches into the host; it
**pushes** profile changes via Server-Sent Events and the agent reacts. The
switches are statically programmed once (uSID SIDs don't change) — only the
host-side encap choice moves.

## Layout
```
mrc-fabric/
├── controller/           Flask + SSE + telemetry overlay, Dockerized
│   ├── app.py
│   ├── gen_fabric.py        # clab topology -> fabric.json
│   ├── gen_switch_config.py # fabric -> SONiC per-switch config (wraps the upstream emulator)
│   ├── templates/index.html
│   ├── Dockerfile
│   └── requirements.txt
├── agent/                The host virtual NIC daemon (`mrc-agent`)
│   ├── mrc-agent             # Python; SSE client + mrc0 lifecycle + metrics
│   ├── mrc-agent.service     # systemd unit
│   └── README.md
├── lab/                  containerlab lab + orchestrator
│   ├── topology.clab.yaml
│   ├── deploy.sh             # up | fabric | hosts | controller | agents | demo | login | verify | destroy
│   ├── switch-config/        # per-switch SONiC static config (config_db + frr.conf)
│   └── ansible/
└── docs/
    ├── architecture.md
    └── spec-mapping.md       # how this maps to OCP MRC 1.0
```

## Quick start
```
git clone … mrc-fabric && cd mrc-fabric/lab
./deploy.sh up                          # fabric + hosts + controller + agents
# open http://localhost:9810
./deploy.sh demo                        # toggle the controller; watch host routes follow
```

## What "running" looks like
Once `./deploy.sh up` is done, on host00:
```
mrc-agent status              # the rich at-a-glance view (reads local state file)
mrc-agent status --watch      # live refresh
mrc-agent paths               # just the active paths
```
Shows host identity, controller connection state, the active EV set with
friendly path IDs, the kernel route the agent has installed, and live underlay
counters — all in one place.

Toggle a path in the GUI (or `curl /api/profile`) — the agent log on the host
records the push, and `ip -6 route show 2001:db8:cccc:01::2` changes within a
tick. Any traffic in flight (ping6, iperf3, curl, anything) rebalances.

## Status
- Controller + agent **work end-to-end in this repo** — the SSE push loop is
  validated; a profile change reaches the agent and reprograms the kernel route
  within a tick.
- Static SONiC switch config is sourced from the upstream Apache-2.0
  `segment-routing/srv6-mrc-emulator` generator; the configs for this topology
  are committed at `lab/switch-config/`.
- The host counters reported back by the agent are real (read from
  `/sys/class/net/<iface>/statistics`); spine-link bps in the UI is still a
  modeled overlay until switch-counter polling is added (a clean follow-on).

## License
Apache-2.0. See `LICENSE`. The SONiC config generator wraps the
Apache-2.0 `segment-routing/srv6-mrc-emulator`.
