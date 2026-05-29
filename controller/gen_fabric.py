#!/usr/bin/env python3
"""gen_fabric.py — derive fabric.json from a containerlab topology file.

Classifies nodes by name (spine/leaf/host), assigns uSIDs by the emulator's
convention (spine i -> block:1<i>, leaf i -> block:2<i>), maps hosts to their
leaf via the links, and records each leaf's host-facing uA SID + each host's
End.DT6 decap SID. Re-run whenever you add spines, leaves, or hosts.

    python3 gen_fabric.py --topo ../mrc-srv6-fabric/topology.clab.yaml \
        --block fc00:0000 --out fabric.json
"""
import argparse, json, re, sys
try:
    import yaml
except ImportError:
    sys.exit("pip install pyyaml")

def classify(name):
    if "spine" in name: return "spine"
    if "leaf"  in name: return "leaf"
    if "host"  in name or name.startswith("h"): return "host"
    return "other"

def idx(name):
    m = re.search(r'(\d+)\s*$', name)
    return int(m.group(1)) if m else 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topo", required=True)
    ap.add_argument("--block", default="fc00:0000")
    ap.add_argument("--node-len", type=int, default=16)
    ap.add_argument("--host-ua", default="e009")     # leaf's host-facing uA SID
    ap.add_argument("--host-decap", default="d001")  # host End.DT6 SID
    ap.add_argument("--tenant", default="2001:db8:cccc")
    ap.add_argument("--out", default="fabric.json")
    a = ap.parse_args()

    topo = yaml.safe_load(open(a.topo))
    nodes = topo["topology"]["nodes"]
    links = topo["topology"].get("links", [])

    spines, leaves, hosts = [], [], []
    for n in nodes:
        k = classify(n)
        if k == "spine": spines.append(n)
        elif k == "leaf": leaves.append(n)
        elif k == "host": hosts.append(n)
    spines.sort(key=idx); leaves.sort(key=idx); hosts.sort(key=idx)

    # uSID assignment (hex node id): spine i -> 1<i>, leaf i -> 2<i>
    def usid(kind, i): return f"{(0x10 if kind=='spine' else 0x20) + i:02x}"
    spine_usid = {n: usid("spine", i) for i, n in enumerate(spines)}
    leaf_usid  = {n: usid("leaf",  i) for i, n in enumerate(leaves)}

    # host <-> leaf adjacency from links
    adj = {}
    for l in links:
        a0, b0 = (e.split(":")[0] for e in l["endpoints"])
        adj.setdefault(a0, []).append(b0); adj.setdefault(b0, []).append(a0)
    host_leaf = {}
    for h in hosts:
        host_leaf[h] = next((x for x in adj.get(h, []) if x in leaf_usid), None)

    fabric = {
        "usid_block": a.block, "node_len": a.node_len,
        "host_ua": a.host_ua, "host_decap": a.host_decap,
        "spines": [{"name": s, "usid": spine_usid[s]} for s in spines],
        "leaves": [{"name": l, "usid": leaf_usid[l]} for l in leaves],
        "hosts":  [{"name": h, "leaf": host_leaf[h],
                    "addr": f"{a.tenant}:{idx(h):02d}::2", "decap": a.host_decap}
                   for h in hosts],
        "links": [[e.split(":")[0] for e in l["endpoints"]] for l in links],
        # default steerable flow = first host -> last host (override in UI/API)
        "flow": {"src": hosts[0], "dst": hosts[-1]} if len(hosts) >= 2 else {},
    }
    json.dump(fabric, open(a.out, "w"), indent=2)
    print(f"wrote {a.out}: {len(spines)} spines, {len(leaves)} leaves, {len(hosts)} hosts")

if __name__ == "__main__":
    main()
