#!/usr/bin/env python3
"""دورة واحدة من البوت (للـGitHub Actions)."""
import json
import os
import sys
import time

import yaml

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from bot import data, engine  # noqa: E402

STATE = os.path.join(ROOT, "data", "state.json")
SNAP = os.path.join(ROOT, "dashboard", "snapshot.json")


def main() -> int:
    with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    now = int(time.time() * 1000)
    state = engine.load_state(STATE, cfg, now)
    rep = engine.run_cycle(data.BinanceData(), cfg, state, now)
    engine.save_state(STATE, state)
    with open(SNAP, "w", encoding="utf-8") as f:
        json.dump(engine.snapshot(state, cfg), f, ensure_ascii=False, separators=(",", ":"))
    print(json.dumps({"equity": round(state["equity"], 2), "open": list(state["positions"]), "actions": rep["actions"],
                      "gate": rep.get("gate"), "regime": (rep.get("regime") or {}).get("reason"),
                      "errors": rep["errors"]}, ensure_ascii=False, indent=2))
    return 1 if rep["errors"] and not rep.get("top") else 0


if __name__ == "__main__":
    sys.exit(main())
