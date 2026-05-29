#!/usr/bin/env bash
# mrc-fabric lab orchestrator.
#
#   ./deploy.sh up          # full: fabric + hosts + controller + agents + verify
#   ./deploy.sh fabric      # containerlab + push SONiC static config + verify SIDs
#   ./deploy.sh hosts       # baseline host config (addr, route, decap, sysctls)
#   ./deploy.sh controller  # build + run the mrc-controller container
#   ./deploy.sh agents      # install + start mrc-agent on each host
#   ./deploy.sh verify      # SIDs + host addrs + controller reachability + agents
#   ./deploy.sh login NODE  # shell into switch/host (installs the sudo-shim for community SONiC)
#   ./deploy.sh demo        # toggle the controller; watch host routes reflect it
#   ./deploy.sh destroy     # tear it all down
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(cd "$ROOT/.." && pwd)"
TOPO="${TOPO:-$ROOT/topology.clab.yaml}"

SWITCHES="p0-spine00 p0-spine01 p0-leaf00 p0-leaf01"
HOSTS="host00 host01"
CTRL_NAME="mrc-controller"
CTRL_IMG="mrc-controller:dev"
CTRL_NET="mrcsrv6_mgmt"   # name comes from topology.clab.yaml mgmt block
CTRL_IP="172.20.18.213"
CTRL_PORT="9810"

CMD="${1:-up}"; ARG="${2:-}"

# ---- helpers ---------------------------------------------------------------
install_sudo_shim() {  # community SONiC ships no sudo
  local N="$1"
  printf '#!/bin/sh\nexec "$@"\n' | docker exec -i "$N" tee /usr/local/bin/sudo >/dev/null 2>&1 || true
  docker exec "$N" chmod +x /usr/local/bin/sudo 2>/dev/null || true
}
host_pfx() { case "$1" in host00) echo "2001:db8:cccc:00";; host01) echo "2001:db8:cccc:01";; esac; }
peer_of()  { case "$1" in
  host00) echo "2001:db8:cccc:01::2 fc00:0000:10:21:e009:d001::";;
  host01) echo "2001:db8:cccc:00::2 fc00:0000:10:20:e009:d001::";;
esac; }

# ---- fabric ----------------------------------------------------------------
SWITCH_PORTS="Ethernet0 Ethernet4 Ethernet8 Ethernet12 Ethernet16 Ethernet20 Ethernet24 Ethernet28 Ethernet32 Ethernet36"
# Per-node End SID (== own locator prefix) and the iface to attach it to.
# FRR 10.5.4 accepts `behavior uN` in static-sids config but staticd silently
# fails to push it to zebra/kernel for the locator's own prefix. We work around
# by installing the End action with seg6local directly. Linux also strips the
# action when `dev lo`, so we attach to a real interface (any non-lo works
# since End re-routes after the next-csid shift).
end_sid_of()    { case "$1" in
  p0-spine00) echo "fc00:0:10::/48";;
  p0-spine01) echo "fc00:0:11::/48";;
  p0-leaf00)  echo "fc00:0:20::/48";;
  p0-leaf01)  echo "fc00:0:21::/48";;
esac; }
end_sid_dev_of(){ case "$1" in
  p0-spine00|p0-spine01) echo "Ethernet4";;
  p0-leaf00|p0-leaf01)   echo "Ethernet0";;
esac; }

push_switch() {
  local N="$1"
  echo "==> $N: pushing static SONiC config"
  install_sudo_shim "$N"
  docker cp "$ROOT/switch-config/$N/config_db.json" "$N:/etc/sonic/config_db.json"
  docker exec "$N" bash -c "sonic-cfggen -j /etc/sonic/config_db.json --write-to-db && supervisorctl restart all" >/dev/null 2>&1 || true
  # bring switch ports up (community SONiC defaults them admin-down)
  for IF in $SWITCH_PORTS; do docker exec "$N" config interface startup "$IF" >/dev/null 2>&1 || true; done
  local FRR_DIR=/etc/sonic/frr
  docker exec "$N" mkdir -p "$FRR_DIR" 2>/dev/null || true
  docker cp "$ROOT/switch-config/$N/frr.conf" "$N:$FRR_DIR/frr.conf"
  # seg6_enabled per-interface — `all` only applies to ifaces created AFTER the
  # sysctl, so we set it explicitly on every existing iface here.
  docker exec "$N" bash -c '
    for f in /proc/sys/net/ipv6/conf/*/seg6_enabled; do echo 1 > "$f" 2>/dev/null; done
    sysctl -w net.ipv6.conf.all.seg6_enabled=1 net.ipv6.conf.all.forwarding=1 >/dev/null 2>&1
  ' || true
  for try in 1 2 3 4; do docker exec "$N" vtysh -f "$FRR_DIR/frr.conf" >/dev/null 2>&1 && break; sleep 1; done
  # FRR uN workaround: install the End action for this node's locator directly.
  local SID; SID="$(end_sid_of "$N")"
  local DEV; DEV="$(end_sid_dev_of "$N")"
  if [ -n "$SID" ] && [ -n "$DEV" ]; then
    docker exec "$N" ip -6 route replace "$SID" \
      encap seg6local action End flavors next-csid lblen 32 nflen 16 dev "$DEV" 2>/dev/null || true
  fi
}
verify_switch_sids() {
  local N="$1"; local want have
  want=$(grep -cE '^[[:space:]]+sid[[:space:]]' "$ROOT/switch-config/$N/frr.conf" || true)
  # count any seg6local route (whether FRR-programmed or ip-route-installed by
  # the workaround above)
  have=$(docker exec "$N" ip -6 route show table all 2>/dev/null | grep -c seg6local || true)
  printf "  %-15s want=%s  have=%s  %s\n" "$N" "$want" "$have" "$([ "$have" -ge "$want" ] && echo OK || echo MISSING)"
}
do_fabric() {
  pushd "$ROOT" >/dev/null
  echo "== containerlab deploy =="
  sudo -E containerlab deploy -t "$TOPO" >/dev/null
  for s in $SWITCHES; do push_switch "$s"; done
  echo "== verify SIDs =="; for s in $SWITCHES; do verify_switch_sids "$s"; done
  popd >/dev/null
}

# ---- hosts -----------------------------------------------------------------
configure_host() {
  local H="$1"; local P; P=$(host_pfx "$H")
  echo "==> $H: baseline config"
  if ! docker exec "$H" bash -c 'command -v ip >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1'; then
    docker exec "$H" bash -c 'apt-get update -qq >/dev/null 2>&1 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iproute2 iputils-ping python3 python3-minimal curl procps >/dev/null 2>&1' || true
  fi
  docker exec "$H" bash -c "
    ip -6 addr replace ${P}::2/64 dev eth1
    ip link set eth1 up
    # Lower MTU on eth1 so encap'd packets (inner + ~64B SRv6 overhead) still
    # fit the switch underlay MTU (9100). Without this, TCP MSS is computed
    # from 9100 and after encap packets exceed the underlay MTU and get
    # dropped — visible as iperf3 'retransmit storm with 0 bps'.
    ip link set eth1 mtu 9000
    ip -6 route replace fc00:0000::/32 via ${P}::1 dev eth1
    ip -6 route replace 2001:db8:cccc::/48 via ${P}::1 dev eth1
    ip -6 route replace fc00:0000:d001::/48 dev eth1 encap seg6local action End.DT6 table 0
    sysctl -w net.ipv6.fib_multipath_hash_policy=1 >/dev/null 2>&1
    # seg6_enabled per-interface (see push_switch comment for why)
    sysctl -w net.ipv6.conf.eth1.seg6_enabled=1 net.ipv6.conf.all.seg6_enabled=1 >/dev/null 2>&1
  " 2>/dev/null || true
  read -r PADDR PSTACK <<<"$(peer_of "$H")"
  docker exec "$H" bash -c "ip -6 route replace ${PADDR}/128 encap seg6 mode encap segs ${PSTACK} dev eth1" 2>/dev/null || true
}
do_hosts() { for h in $HOSTS; do configure_host "$h"; done; }

# ---- controller container --------------------------------------------------
do_controller() {
  echo "== build $CTRL_IMG =="
  docker build -t "$CTRL_IMG" -f "$PROJECT/controller/Dockerfile" "$PROJECT" || { echo "build failed"; return 1; }
  docker rm -f "$CTRL_NAME" >/dev/null 2>&1 || true
  docker network inspect "$CTRL_NET" >/dev/null 2>&1 || \
    docker network create --subnet 172.20.18.0/24 "$CTRL_NET" >/dev/null
  echo "== start $CTRL_NAME on $CTRL_NET ($CTRL_IP:$CTRL_PORT) =="
  docker run -d --name "$CTRL_NAME" --network "$CTRL_NET" --ip "$CTRL_IP" \
    -p "${CTRL_PORT}:${CTRL_PORT}" "$CTRL_IMG" >/dev/null
  for h in $HOSTS; do docker network connect "$CTRL_NET" "$h" 2>/dev/null || true; done
  for i in 1 2 3 4 5; do curl -sf -m 1 "http://localhost:${CTRL_PORT}/healthz" >/dev/null && break; sleep 1; done
  curl -sf -m 1 "http://localhost:${CTRL_PORT}/healthz" >/dev/null && echo "  controller up: http://localhost:${CTRL_PORT}"
}

# ---- agents ----------------------------------------------------------------
push_agent() {
  local H="$1"; local P; P=$(host_pfx "$H")
  echo "==> $H: install + start mrc-agent"
  docker cp "$PROJECT/agent/mrc-agent" "$H:/usr/local/bin/mrc-agent"
  docker exec "$H" chmod +x /usr/local/bin/mrc-agent
  docker exec "$H" pkill -9 -f 'mrc-agent run' 2>/dev/null || true; sleep 1
  docker exec -d "$H" bash -c "
    nohup /usr/local/bin/mrc-agent run \
        --host $H --controller http://${CTRL_IP}:${CTRL_PORT} \
        --tenant ${P}::2 --underlay eth1 \
        >/var/log/mrc-agent.log 2>&1 &
  "
  sleep 1
  docker exec "$H" pgrep -f 'mrc-agent run' >/dev/null && echo "  $H: agent running" \
    || { echo "  $H: agent FAILED"; docker exec "$H" tail /var/log/mrc-agent.log 2>/dev/null; }
}
do_agents() { for h in $HOSTS; do push_agent "$h"; done; }

# ---- verify ----------------------------------------------------------------
do_verify() {
  echo "== switch SIDs =="; for s in $SWITCHES; do verify_switch_sids "$s"; done
  echo "== host addresses =="
  for h in $HOSTS; do
    a=$(docker exec "$h" ip -6 addr show eth1 2>/dev/null | awk '/inet6 2001/{print $2}')
    printf "  %-8s eth1 %s\n" "$h" "${a:-MISSING}"
  done
  echo "== controller =="
  curl -sf -m 2 "http://localhost:${CTRL_PORT}/healthz" | python3 -m json.tool 2>/dev/null | sed 's/^/  /'
  echo "== agents =="
  for h in $HOSTS; do
    docker exec "$h" pgrep -f 'mrc-agent run' >/dev/null 2>&1 \
      && echo "  $h: agent process OK" || echo "  $h: agent NOT RUNNING"
  done
  echo "== controller sees agents (POSTed metrics) =="
  curl -sf -m 2 "http://localhost:${CTRL_PORT}/api/metrics" | python3 -m json.tool 2>/dev/null | sed 's/^/  /'
}

# ---- demo ------------------------------------------------------------------
do_demo() {
  local CURL="curl -sf -m 2 -H Content-Type:application/json"
  local URL="http://localhost:${CTRL_PORT}/api/profile"
  show_route() { docker exec host00 ip -6 route show 2001:db8:cccc:01::2 2>/dev/null | sed 's/^/    /'; }
  show_mode()  { curl -sf -m 1 "http://localhost:${CTRL_PORT}/api/profile" | python3 -c "import sys,json;d=json.load(sys.stdin);print(f\"   mode={d['mode']}  multipath={d['multipath']}  active_paths={d['active_paths']}\")"; }

  echo "== 1. NONE — plain RoCEv2, no MRC, kernel does standard L4-hash ECMP =="
  $CURL -X POST "$URL" -d '{"mode":"none"}' >/dev/null; sleep 1
  show_mode; echo "   host00 route to host01:"; show_route
  echo;
  echo "== 2. UDP — EV in UDP src port, app-driven; no kernel encap installed =="
  $CURL -X POST "$URL" -d '{"mode":"udp","multipath":true,"active_paths":["via-p0-spine00","via-p0-spine01"]}' >/dev/null; sleep 1
  show_mode; echo "   host00 route to host01 (should be plain — no encap):"; show_route
  echo;
  echo "== 3. STEV — structured entropy in UDP src port; same underlay =="
  $CURL -X POST "$URL" -d '{"mode":"stev","multipath":true,"active_paths":["via-p0-spine00","via-p0-spine01"]}' >/dev/null; sleep 1
  show_mode; echo "   host00 route to host01 (still plain):"; show_route
  echo;
  echo "== 4. SRv6 single path via spine00 — kernel encap installed =="
  $CURL -X POST "$URL" -d '{"mode":"srv6","multipath":false,"active_paths":["via-p0-spine00"]}' >/dev/null; sleep 1
  show_mode; echo "   host00 route to host01:"; show_route
  echo;
  echo "== 5. SRv6 multipath (spray both spines) — ECMP nexthops =="
  $CURL -X POST "$URL" -d '{"mode":"srv6","multipath":true,"active_paths":["via-p0-spine00","via-p0-spine01"]}' >/dev/null; sleep 1
  show_mode; echo "   host00 route to host01:"; show_route
  echo;
  echo "READY. Tip: run \`docker exec -it host00 mrc-agent monitor\` to see live per-mode telemetry."
}

# ---- login -----------------------------------------------------------------
do_login() {
  local N="${ARG:-}"
  [ -z "$N" ] && { echo "usage: $0 login NODE   (nodes: $SWITCHES $HOSTS)"; exit 1; }
  docker inspect "$N" >/dev/null 2>&1 || { echo "no such container: $N"; exit 1; }
  install_sudo_shim "$N"
  echo "== $N == sudo-shim installed; you are root. 'exit' to leave."
  docker exec -it "$N" bash 2>/dev/null || docker exec -it "$N" sh
}

# ---- destroy ---------------------------------------------------------------
do_destroy() {
  docker rm -f "$CTRL_NAME" >/dev/null 2>&1 || true
  docker network rm "$CTRL_NET" >/dev/null 2>&1 || true
  sudo containerlab destroy -t "$TOPO" --cleanup >/dev/null 2>&1 || true
}

# ---- diag (live SR config dump for any node) -------------------------------
do_diag() {
  local N="${ARG:-}"
  [ -z "$N" ] && { echo "usage: $0 diag NODE   (nodes: $SWITCHES $HOSTS)"; exit 1; }
  docker inspect "$N" >/dev/null 2>&1 || { echo "no such container: $N"; exit 1; }
  install_sudo_shim "$N"
  case " $SWITCHES " in *" $N "*)
    echo "== $N · SWITCH SR DIAG =="
    echo "-- expected SIDs (from frr.conf) --"
    docker exec "$N" grep -E '^\s+sid\s' /etc/sonic/frr/frr.conf 2>/dev/null | sed 's/^/  /'
    echo "-- seg6local routes installed in kernel --"
    docker exec "$N" ip -6 route show table all 2>/dev/null | grep seg6local | sed 's/^/  /'
    echo "-- interface up/down --"
    docker exec "$N" sh -c 'show interface status 2>/dev/null | head -8 | sed "s/^/  /"' || \
      docker exec "$N" ip -br link show | grep -E 'Ethernet|eth1' | sed 's/^/  /'
    echo "-- seg6_enabled (per-interface) --"
    docker exec "$N" sh -c 'for f in all default Ethernet0 Ethernet4 Ethernet36 Loopback0 eth1; do
      v=$(cat /proc/sys/net/ipv6/conf/$f/seg6_enabled 2>/dev/null); fw=$(cat /proc/sys/net/ipv6/conf/$f/forwarding 2>/dev/null)
      [ -n "$v$fw" ] && printf "  %-12s seg6=%s fwd=%s\n" "$f" "${v:--}" "${fw:--}"
    done'
    echo "-- FRR running srv6 --"
    docker exec "$N" vtysh -c 'show running-config' 2>/dev/null | sed -n '/^segment-routing/,/^!/p' | sed 's/^/  /'
    return ;;
  esac
  case " $HOSTS " in *" $N "*)
    echo "== $N · HOST DIAG =="
    echo "-- eth1 address + mtu --"
    docker exec "$N" ip -6 addr show eth1 2>/dev/null | grep -E 'inet6|mtu' | sed 's/^/  /'
    echo "-- mrc0 (identity marker) --"
    docker exec "$N" ip -d link show mrc0 2>/dev/null | sed 's/^/  /' || echo "  (mrc0 not present)"
    echo "-- encap routes --"
    docker exec "$N" ip -6 route show 2>/dev/null | grep -E 'encap seg6|seg6local' | sed 's/^/  /'
    echo "-- seg6_enabled --"
    docker exec "$N" sh -c 'for f in all default eth1; do
      v=$(cat /proc/sys/net/ipv6/conf/$f/seg6_enabled 2>/dev/null)
      [ -n "$v" ] && printf "  %-12s seg6=%s\n" "$f" "$v"
    done'
    echo "-- mrc-agent process --"
    docker exec "$N" pgrep -af 'mrc-agent run' 2>/dev/null | sed 's/^/  /' || echo "  (not running)"
    echo "-- recent agent log --"
    docker exec "$N" tail -10 /var/log/mrc-agent.log 2>/dev/null | sed 's/^/  /' || echo "  (no log)"
    return ;;
  esac
  echo "unknown node category: $N"
}

case "$CMD" in
  up)         do_fabric; do_hosts; do_controller; do_agents; do_verify
              echo "READY. UI: http://localhost:${CTRL_PORT}   Demo: ./deploy.sh demo" ;;
  fabric)     do_fabric ;;
  hosts)      do_hosts ;;
  controller) do_controller ;;
  agents)     do_agents ;;
  verify)     do_verify ;;
  demo)       do_demo ;;
  login)      do_login ;;
  diag)       do_diag ;;
  destroy)    do_destroy ;;
  *) echo "usage: $0 {up|fabric|hosts|controller|agents|verify|demo|login|diag|destroy} [arg]"; exit 1 ;;
esac
