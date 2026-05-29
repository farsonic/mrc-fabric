# mrc-agent — the host virtual NIC

A small, persistent daemon that makes a Linux host an MRC endpoint. It:

1. Owns a logical interface (`mrc0`, dummy) on which the host's IPv6 tenant
   address lives. This is the "MRC surface" of the host, decoupled from any
   physical underlay.
2. Holds an SSE subscription to the mrc-fabric controller and reprograms the
   kernel seg6 encap route(s) on every profile change — mode shifts, path
   denies, multipath spray, all visible within a tick.
3. Posts host underlay byte counters back to the controller for real telemetry
   on access links (the controller's modeled telemetry stays for spine links
   until switch counter polling is wired).

Any application — `iperf3 -6`, `ping6`, `curl`, rocebench — that sends to a
peer tenant address picks up the agent's current state. Apps stay
MRC-unaware; the agent is the MRC endpoint.

## Quick start
```
sudo ./mrc-agent run \
    --host host00 \
    --controller http://172.20.18.213:9810 \
    --tenant 2001:db8:cccc:00::2 \
    --underlay eth1
```
Or via systemd: copy `mrc-agent.service`, edit the env line, then
`systemctl enable --now mrc-agent`.

## Inspect
```
mrc-agent show --controller http://172.20.18.213:9810
# -> current EV mode, active EV set, installed seg6 route, metrics

mrc-agent test --controller http://172.20.18.213:9810
# one-shot SSE test
```

## Dry run (no root, no actual ip commands)
```
mrc-agent run ... --dry-run
```
