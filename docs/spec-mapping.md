# OCP MRC 1.0 — spec mapping

This is how the components in this repo map to the OCP MRC 1.0 spec.

## EV format modes (`mrc_ctl.h`)

| Spec mode                         | UI button | Behaviour |
|-----------------------------------|-----------|-----------|
| `MRC_CTL_EV_FMT_MODE_UDP`         | `UDP`     | Default spray: ECMP across **all** good paths. Entropy = UDP src-port. |
| `MRC_CTL_EV_FMT_MODE_STEV`        | `STEV`    | Structured EV pinned to **one** path. UI defaults to the first good. |
| `MRC_CTL_EV_FMT_MODE_SRV6`        | `SRV6`    | Explicit uSID segment list(s). Single or multipath, user picks the set. |

The controller serves the active mode in `profile.mode` and the corresponding
EV set as `profile.active_evs[]` (each `{entropy, usid}`). The agent picks the
mode-appropriate dataplane action — for SRV6 it programs `ip -6 route ... encap
seg6` on the underlay.

## Control plane (`mrc_ctl_*` calls)

| Spec concept                          | Implemented as |
|---------------------------------------|----------------|
| Endpoint → controller subscription    | `GET /api/profile/stream` (SSE) — long-lived; agent reconnects with backoff |
| Get current EV profile                | `GET /api/profile` — one-shot snapshot |
| Set / update EV profile               | `POST /api/profile`, `POST /api/path/<id>/{active,status}` |
| Endpoint health / counters reporting  | `POST /api/metrics` — agent pushes `{host, eth_tx_bps, eth_rx_bps, routes}` |
| Profile state machine                 | `profile.state ∈ {init, offline, online}` |

The profile's `version` is bumped (`served_at` timestamp) on every change,
making it natural for an agent to dedupe re-pushes if needed.

## Endpoint behaviour (the agent)

- **Tenant identity**: each MRC endpoint has a tenant IPv6 address (the
  host's "MRC surface"). The agent puts that address on `mrc0` and removes
  it from the underlay, so the identity is independent of the physical link.
- **Per-flow steering**: the agent reprograms the encap route on every
  profile push. For SRV6 multipath, it builds a kernel ECMP route with one
  `nexthop encap seg6` per active uSID stack.
- **Resilience**: SSE drop → exponential backoff reconnect (1s, 2s, 4s … cap
  30s). Each reconnect re-delivers the latest snapshot before incremental
  pushes resume.

## Fabric

- **Underlay**: SRv6 uSID, `fc00:0000::/32` block, node-len 16, func-len 16.
- **uSID assignments**:
  - spines: `fc00:0000:10::`, `fc00:0000:11::`
  - leaves: `fc00:0000:20::` (leaf00), `fc00:0000:21::` (leaf01)
  - host-facing leaf End.uA: `e009`
  - host End.DT6 decap: `d001`
- **End-to-end uSID stack** (host00 → host01 via spineX):
  `fc00:0000:<X>:21:e009:d001::` — first uSID is the spine choice (the EV).
- **Tenant overlay**: `2001:db8:cccc:00::/64` (host00 side),
  `2001:db8:cccc:01::/64` (host01).

## Out of scope (clearly)

- **No reliability protocol on the agent**. SETH/NETH (SACK/NACK), congestion
  control, retransmission — those live in MRC-aware applications (e.g.
  rocebench's MRC protocol implementation, separate repo). The agent only
  steers paths and reports counters.
- **No switch CP**. Switches are statically programmed; only host-side encap
  is dynamic.
