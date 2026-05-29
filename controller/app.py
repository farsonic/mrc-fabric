#!/usr/bin/env python3
"""
mrc-fabric controller — topology-driven EV-profile manager.

Speaks the OCP MRC 1.0 mrc_ctl.h control plane to host agents:
  - GET  /api/profile               one-shot snapshot (legacy / debug)
  - GET  /api/profile/stream        Server-Sent Events: profile pushed on every change
  - POST /api/profile               set mode / active paths / offered rate
  - POST /api/path/<id>/active      toggle a path's membership in the spray set
  - POST /api/path/<id>/status      mark good/deny (auto-reroutes)
  - POST /api/metrics               agent posts host counters (eth_tx/rx bytes)
  - GET  /api/telemetry             per-link bps (real where agents report, modeled elsewhere)
  - GET  /api/topology              fabric.json + flow
  - GET  /api/paths                 enumerated paths
  - POST /api/reload                re-read fabric.json
"""
from flask import Flask, jsonify, request, render_template, Response, stream_with_context
import json, os, time, threading, queue, random

app = Flask(__name__)
app.json.sort_keys = False
FABRIC_PATH = os.environ.get("FABRIC_JSON", os.path.join(os.path.dirname(__file__), "fabric.json"))

def load_fabric():
    with open(FABRIC_PATH) as f:
        return json.load(f)
FAB = load_fabric()

STATE = {
    "mode": "srv6",
    "profile_state": "online",
    "flow": dict(FAB.get("flow", {})),
    "active_paths": [],
    "path_status": {},
    "offered_gbps": 100.0,
    "updated": time.time(),
}
MODES = {
    "udp":  "ECMP-UDP — spray across ALL good paths (MRC_CTL_EV_FMT_MODE_UDP)",
    "stev": "Structured EV — pin the flow to ONE path (MRC_CTL_EV_FMT_MODE_STEV)",
    "srv6": "SRv6 uSID — explicit segment list(s); one or many paths (MRC_CTL_EV_FMT_MODE_SRV6)",
}

# ---- SSE subscriber registry ------------------------------------------------
_subs_lock = threading.Lock()
_subscribers = []   # list of queue.Queue, one per connected agent

def _publish_profile():
    """Push the current profile JSON to every connected SSE subscriber."""
    snap = build_profile()
    msg = "data: " + json.dumps(snap) + "\n\n"
    with _subs_lock:
        dead = []
        for q in _subscribers:
            try: q.put_nowait(msg)
            except queue.Full: dead.append(q)
        for q in dead:
            try: _subscribers.remove(q)
            except ValueError: pass

# ---- agent-reported host metrics --------------------------------------------
# {host_name: {"eth_tx_bps": float, "eth_rx_bps": float, "ts": float}}
HOST_METRICS = {}

# ---- topology helpers -------------------------------------------------------
def _idx(coll, key="name"): return {x[key]: x for x in coll}
def host_of(n):  return _idx(FAB["hosts"]).get(n)
def leaf_of(n):  return _idx(FAB["leaves"]).get(n)
def neighbours(node):
    out = []
    for a, b in FAB.get("links", []):
        if a == node: out.append(b)
        elif b == node: out.append(a)
    return out
def usid_stack(*u): return f"{FAB['usid_block']}:" + ":".join(u) + "::"

def compute_paths():
    flow = STATE["flow"]
    src_h, dst_h = host_of(flow.get("src")), host_of(flow.get("dst"))
    if not src_h or not dst_h: return []
    src_leaf, dst_leaf = src_h["leaf"], dst_h["leaf"]
    dlu = leaf_of(dst_leaf)["usid"]
    paths = []
    for sp in FAB["spines"]:
        nb = neighbours(sp["name"])
        if src_leaf in nb and dst_leaf in nb:
            pid = f"via-{sp['name']}"
            paths.append({
                "id": pid, "spine": sp["name"],
                "segments_nodes": [src_h["name"], src_leaf, sp["name"], dst_leaf, dst_h["name"]],
                "usid_segments": [sp["usid"], dlu, FAB["host_ua"], dst_h["decap"]],
                "usid": usid_stack(sp["usid"], dlu, FAB["host_ua"], dst_h["decap"]),
                "entropy": 49000 + len(paths),
                "status": STATE["path_status"].get(pid, "good"),
                "dst_addr": dst_h["addr"],
            })
    ids = [p["id"] for p in paths]
    STATE["active_paths"] = [p for p in STATE["active_paths"] if p in ids]
    if not STATE["active_paths"]:
        STATE["active_paths"] = [p["id"] for p in paths if p["status"] == "good"]
    return paths

def apply_mode_default(mode):
    paths = compute_paths()
    good = [p["id"] for p in paths if p["status"] == "good"]
    if mode == "udp":    STATE["active_paths"] = good[:]
    elif mode == "stev": STATE["active_paths"] = good[:1]
    elif mode == "srv6" and not STATE["active_paths"]:
        STATE["active_paths"] = good[:]

# ---- telemetry --------------------------------------------------------------
def link_key(a, b): return "|".join(sorted([a, b]))

def telemetry():
    """Per-link bps. Where an agent has reported real eth counters, use those for
    the host-facing link; spine links remain the offered-rate spray model until
    real switch interface-counter polling is wired."""
    paths = compute_paths()
    active = [p for p in paths if p["id"] in STATE["active_paths"] and p["status"] == "good"]
    n = len(active)
    share = (STATE["offered_gbps"] * 1e9 / n) if n else 0.0   # bits/s per path
    links_bps = {link_key(a, b): 0.0 for a, b in FAB.get("links", [])}
    per_path_bps = {}
    for p in active:
        bw = share * (1.0 + random.uniform(-0.04, 0.04))
        per_path_bps[p["id"]] = bw
        ch = p["segments_nodes"]
        for i in range(len(ch) - 1):
            links_bps[link_key(ch[i], ch[i + 1])] += bw

    # Overlay REAL host-facing link counters where an agent reported them.
    real_overlay = {}
    for host_name, m in HOST_METRICS.items():
        h = host_of(host_name)
        if not h or time.time() - m.get("ts", 0) > 5: continue
        access = link_key(host_name, h["leaf"])
        real_bps = max(m.get("eth_tx_bps", 0.0), m.get("eth_rx_bps", 0.0))
        links_bps[access] = real_bps
        real_overlay[access] = real_bps

    # Convert to Gbps for the UI.
    def g(x): return round(x / 1e9, 3)
    return {
        "links":        {k: g(v) for k, v in links_bps.items()},
        "per_path":     {k: g(v) for k, v in per_path_bps.items()},
        "total_gbps":   round(sum(per_path_bps.values()) / 1e9, 3),
        "offered_gbps": STATE["offered_gbps"],
        "n_active":     n,
        "real_links":   sorted(real_overlay.keys()),
        "hosts_reporting": sorted(HOST_METRICS.keys()),
        "model": ("host-counter overlay (real on access links) + offered-rate model (spine links)"
                  if real_overlay else "offered-rate spray model (no agent metrics yet)"),
    }

# ---- profile ----------------------------------------------------------------
def build_profile():
    paths = compute_paths()
    flow = STATE["flow"]; dst_h = host_of(flow.get("dst")) or {}
    active_ids = STATE["active_paths"]
    evs = [{
        "id": p["id"], "spine": p["spine"], "entropy": p["entropy"],
        "usid": p["usid"], "segments": [p["usid"]],
        "status": p["status"], "active": p["id"] in active_ids,
    } for p in paths]
    primary = next((p for p in paths if p["id"] in active_ids and p["status"] == "good"), None)
    active_evs = [e for e in evs if e["active"] and e["status"] == "good"]
    return {
        "version": 3,
        "mode": STATE["mode"],
        "state": STATE["profile_state"],
        "multipath": len(active_evs) > 1,
        "flow": {"src": flow.get("src"), "dst": flow.get("dst"), "dst_addr": dst_h.get("addr")},
        "active_paths":   [e["id"] for e in active_evs],
        "active_path":    primary["id"] if primary else None,
        "active_entropy": primary["entropy"] if primary else 0,
        "active_usid":    primary["usid"] if primary else "",
        "active_dst":     dst_h.get("addr", ""),
        "active_evs":     [{"entropy": e["entropy"], "usid": e["usid"]} for e in active_evs],
        "usid_block":     f"{FAB['usid_block']}::/32",
        "offered_gbps":   STATE["offered_gbps"],
        "evs": evs,
        "served_at": STATE["updated"],
    }

# ---- API --------------------------------------------------------------------
@app.route("/")
def index(): return render_template("index.html")

@app.route("/api/profile", methods=["GET"])
def api_profile(): return jsonify(build_profile())

@app.route("/api/profile/stream")
def api_profile_stream():
    """Server-Sent Events: emit the current profile immediately, then on every change.
    Agents subscribe here once and keep the connection open."""
    q = queue.Queue(maxsize=64)
    with _subs_lock: _subscribers.append(q)
    @stream_with_context
    def gen():
        try:
            yield "data: " + json.dumps(build_profile()) + "\n\n"
            while True:
                try: yield q.get(timeout=20)
                except queue.Empty: yield ": keepalive\n\n"   # SSE comment, keeps the conn alive
        finally:
            with _subs_lock:
                try: _subscribers.remove(q)
                except ValueError: pass
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/profile", methods=["POST"])
def api_set_profile():
    b = request.get_json(force=True, silent=True) or {}
    if b.get("mode") in MODES:
        STATE["mode"] = b["mode"]; apply_mode_default(b["mode"])
    if b.get("profile_state") in ("init", "offline", "online"):
        STATE["profile_state"] = b["profile_state"]
    if isinstance(b.get("offered_gbps"), (int, float)) and b["offered_gbps"] > 0:
        STATE["offered_gbps"] = float(b["offered_gbps"])
    ids = [p["id"] for p in compute_paths()]
    if isinstance(b.get("active_paths"), list):
        STATE["active_paths"] = [x for x in b["active_paths"] if x in ids]
    elif b.get("active_path") in ids:
        STATE["active_paths"] = [b["active_path"]]
    if "flow" in b and host_of(b["flow"].get("src")) and host_of(b["flow"].get("dst")):
        STATE["flow"] = {"src": b["flow"]["src"], "dst": b["flow"]["dst"]}; STATE["active_paths"] = []
    STATE["updated"] = time.time()
    _publish_profile()
    return jsonify(build_profile())

@app.route("/api/path/<pid>/active", methods=["POST"])
def api_path_active(pid):
    b = request.get_json(force=True, silent=True) or {}
    ids = [p["id"] for p in compute_paths()]
    if pid not in ids: return jsonify({"error": "no such path"}), 404
    on = bool(b.get("active", True))
    s = set(STATE["active_paths"])
    if on: s.add(pid)
    else:  s.discard(pid)
    STATE["active_paths"] = [i for i in ids if i in s]
    STATE["updated"] = time.time()
    _publish_profile()
    return jsonify(build_profile())

@app.route("/api/path/<pid>/status", methods=["POST"])
def api_path_status(pid):
    b = request.get_json(force=True, silent=True) or {}
    st = b.get("status")
    if st not in ("good", "deny"): return jsonify({"error": "good|deny"}), 400
    STATE["path_status"][pid] = st
    paths = compute_paths()
    if st == "deny":
        STATE["active_paths"] = [i for i in STATE["active_paths"] if i != pid]
        if not STATE["active_paths"]:
            g = next((p["id"] for p in paths if p["status"] == "good"), None)
            if g: STATE["active_paths"] = [g]
    STATE["updated"] = time.time()
    _publish_profile()
    return jsonify(build_profile())

@app.route("/api/metrics", methods=["POST"])
def api_metrics():
    """Agent posts host counters: {host, eth_tx_bps, eth_rx_bps}."""
    b = request.get_json(force=True, silent=True) or {}
    h = b.get("host")
    if not h or not host_of(h): return jsonify({"error": "unknown host"}), 400
    HOST_METRICS[h] = {
        "eth_tx_bps": float(b.get("eth_tx_bps", 0)),
        "eth_rx_bps": float(b.get("eth_rx_bps", 0)),
        "routes":     int(b.get("routes", 0)),
        "iface":      b.get("iface", "eth1"),
        "ts":         time.time(),
    }
    return jsonify({"ok": True, "received": HOST_METRICS[h]})

@app.route("/api/metrics", methods=["GET"])
def api_metrics_get(): return jsonify(HOST_METRICS)

@app.route("/api/telemetry")
def api_telemetry(): return jsonify(telemetry())

@app.route("/api/topology")
def api_topology():
    return jsonify({"block": FAB["usid_block"], "spines": FAB["spines"], "leaves": FAB["leaves"],
                    "hosts": FAB["hosts"], "links": FAB["links"], "flow": STATE["flow"]})

@app.route("/api/paths")
def api_paths():
    return jsonify({"paths": compute_paths(), "modes": MODES,
                    "active_paths": STATE["active_paths"], "offered_gbps": STATE["offered_gbps"]})

@app.route("/api/reload", methods=["POST"])
def api_reload():
    global FAB
    FAB = load_fabric(); STATE["active_paths"] = []
    STATE["flow"] = STATE["flow"] or dict(FAB.get("flow", {}))
    _publish_profile()
    return jsonify({"reloaded": True, "spines": len(FAB["spines"]),
                    "leaves": len(FAB["leaves"]), "hosts": len(FAB["hosts"])})

@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "subscribers": len(_subscribers),
                    "metrics_hosts": list(HOST_METRICS.keys())})

if __name__ == "__main__":
    # threaded=True is required: the SSE generators block, and we still need to
    # serve other API calls concurrently.
    app.run(host="0.0.0.0", port=9810, threaded=True)
