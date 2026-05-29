# mrc-agent — the host virtual NIC

A persistent daemon that makes a Linux host an MRC endpoint. It:

1. Owns a logical interface (`mrc0`, a dummy device) on which the host's IPv6
   tenant address lives — the "MRC surface" of the host, decoupled from the
   underlay.
2. Holds an SSE subscription to the mrc-fabric controller and reprograms the
   kernel seg6 encap route(s) on every profile change.
3. Samples underlay byte counters (`/sys/class/net/<iface>/statistics`) and
   POSTs them back to the controller for real telemetry on access links.
4. Writes its full state to `/run/mrc-agent.json` (falls back to `/tmp/`) so
   `mrc-agent status` — or any other tool on the host — sees everything in one
   place, instantly, with no network call.

Any application — `iperf3 -6`, `ping6`, `curl`, rocebench — that sends to a
peer tenant address picks up the agent's current state. Apps stay
MRC-unaware; the agent is the MRC endpoint.

## Commands
```
mrc-agent run     --host H --controller URL --tenant ADDR --underlay IF
mrc-agent status  [--watch] [--json]        # the rich live view
mrc-agent paths                              # just the active paths
mrc-agent test    --controller URL           # one-shot SSE connection test
```

## `mrc-agent status` — at-a-glance view of the NIC
```
╭─ mrc0 · MRC virtual NIC ──────────────────────── state UP ─╮
│  host        host00
│  identity    2001:db8:cccc:00::2
│  underlay    eth1
│  uptime      2m 17s
╰────────────────────────────────────────────────────────────

  CONTROLLER  http://172.20.18.213:9810
    [✓] CONNECTED · 14 events received · last push 0.6s ago

  EV PROFILE  (mode SRV6 · multipath)
    flow      host00 → host01 (2001:db8:cccc:01::2)
    [✓] via-p0-spine00     EV 49000    fc00:0000:10:21:e009:d001::
    [✓] via-p0-spine01     EV 49001    fc00:0000:11:21:e009:d001::

  KERNEL ROUTE  →  2001:db8:cccc:01::2
    ECMP · 2 nexthop(s) · programmed 0.6s ago · OK
    live kernel route:
      2001:db8:cccc:01::2  metric 1024
        nexthop encap seg6 ... segs fc00:0:10:21:e009:d001:: dev eth1 weight 1
        nexthop encap seg6 ... segs fc00:0:11:21:e009:d001:: dev eth1 weight 1

  COUNTERS  (underlay eth1; mrc0 is metadata-only)
    tx       12.34 GB    last sec   3.21 Gbps
    rx       11.19 GB    last sec   2.95 Gbps
    sampled   now
```

Add `--watch` for a live refresh (defaults to 1s), `--json` for raw machine
output.

## Why mrc0 is a `dummy` (and what's "on" eth1)
On Linux, kernel seg6 encap counters tick on the egress device, not on the
device the route is associated with. `mrc0` is the *identity surface* — it
carries the tenant IPv6 address so the source address is stable and decoupled
from any physical NIC; the **underlay (`eth1`) carries the encapped traffic**,
and its counters are what `mrc-agent status` reports.

If you want to inspect raw kernel state directly:
```
ip link show mrc0
ip addr show mrc0
ip -6 route show 2001:db8:cccc:01::2     # the active encap route(s)
cat /sys/class/net/eth1/statistics/tx_bytes
```

## Install (systemd, on a real host)
```
sudo install -m 0755 mrc-agent /usr/local/bin/mrc-agent
sudo install -m 0644 mrc-agent.service /etc/systemd/system/mrc-agent.service
sudo sed -i 's|HOST=host00|HOST=<this-host>|' /etc/systemd/system/mrc-agent.service
sudo systemctl daemon-reload && sudo systemctl enable --now mrc-agent
mrc-agent status
```

## Dry-run (no root, no kernel changes)
```
mrc-agent run --host host00 --controller http://... --tenant ... --underlay lo --dry-run
```
