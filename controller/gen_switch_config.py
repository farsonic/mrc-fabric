#!/usr/bin/env python3
"""Regenerate per-switch SONiC config from a clab topology.

Delegates to the segment-routing/srv6-mrc-emulator generator when available
(Apache-2.0; produces config_db.json + frr.conf with static uSID SIDs).
Without it, the already-committed configs under lab/switch-config/ remain the
canonical static config — switches are not dynamically programmed.
"""
import argparse, os, shutil, subprocess, sys

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topo", required=True, help="containerlab topology yaml")
    ap.add_argument("--out", required=True, help="output dir (per-node subdirs)")
    ap.add_argument("--emulator", default=os.environ.get("SRV6_MRC_EMULATOR_PATH", ""),
                    help="path to a checkout of segment-routing/srv6-mrc-emulator")
    args = ap.parse_args()

    if args.emulator and os.path.isdir(args.emulator):
        gen = os.path.join(args.emulator, "generators", "gen_configs.py")
        if os.path.isfile(gen):
            os.makedirs(args.out, exist_ok=True)
            print(f"[gen] running upstream generator: {gen}")
            subprocess.check_call([sys.executable, gen, "--topo", args.topo, "--out", args.out])
            return
    print("[gen] upstream generator not configured (set --emulator or SRV6_MRC_EMULATOR_PATH).")
    print(f"[gen] lab/switch-config/ already ships static configs for this topology.")
    if not os.path.isdir(args.out):
        print(f"[gen] {args.out!r} does not exist; nothing to do.")
        sys.exit(1)
    nodes = sorted(d for d in os.listdir(args.out) if os.path.isdir(os.path.join(args.out, d)))
    for n in nodes: print(f"  {n}")

if __name__ == "__main__":
    main()
