import os
import sys
import unittest

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from bot import backtest as B  # noqa: E402
from bot import engine, features as F, indicators as I, paper, risk  # noqa: E402
from synthetic import M15, FakeClient  # noqa: E402

CFG = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "config.yaml"), encoding="utf-8"))
NEUTRAL = lambda syms, cfg, now: {"ok": True, "enabled": True, "global": 0.0, "coins": {}, "notes": [], "n": 5, "llm": False,
                                  "summary": None, "failed": []}
SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]


class Indicators(unittest.TestCase):
    def test_ema_sma(self):
        v = [float(i) for i in range(1, 61)]
        self.assertAlmostEqual(I.sma(v, 5)[-1], 58.0)
        self.assertIsNone(I.ema(v, 20)[18])
        self.assertIsNotNone(I.ema(v, 20)[19])

    def test_rsi_bounds(self):
        up = [float(i) for i in range(1, 60)]
        self.assertEqual(I.rsi(up)[-1], 100.0)


class Risk(unittest.TestCase):
    def test_size_from_stop_distance_and_cap(self):
        q = risk.position_size(1000, 1000, 100, 98, CFG, 1.0)
        self.assertAlmostEqual(q * 100, 300.0)             # سقف 30%
        q = risk.position_size(1000, 1000, 100, 90, CFG, 1.0)
        self.assertAlmostEqual(q * 10, 7.5)                # مخاطرة 0.75% = 7.5$

    def test_drawdown_reduces_risk_never_raises(self):
        self.assertEqual(risk.risk_multiplier(1000, 1000, CFG), 1.0)
        self.assertEqual(risk.risk_multiplier(960, 1000, CFG), 0.5)
        self.assertEqual(risk.risk_multiplier(930, 1000, CFG), 0.25)

    def test_gates(self):
        st = engine.new_state(CFG, 1_760_000_000_000)
        self.assertTrue(risk.gate(st, 1000, 1_760_000_000_000, CFG)[0])
        self.assertFalse(risk.gate(st, 979, 1_760_000_000_000, CFG)[0])      # -2.1% يوم
        self.assertFalse(risk.gate(st, 1021, 1_760_000_000_000, CFG)[0])     # قفل المكسب
        st2 = engine.new_state(CFG, 1_760_000_000_000)
        st2["peak_equity"] = 1200
        self.assertFalse(risk.gate(st2, 1000, 1_760_000_000_000, CFG)[0])    # kill switch (-16%)
        self.assertTrue(st2["killed"])


class Paper(unittest.TestCase):
    def mk(self):
        return paper.new_position("X", 100.0, 2.0, {"stop_dist": 10.0, "atr1": 6.0}, 0, 0, 80, ["t"], CFG)

    def test_stop_first_when_both_hit(self):
        p = self.mk()
        ev = paper.process_candle(p, 100, 130, 80, 100, M15, CFG)   # الهدف والستوب في نفس الشمعة
        self.assertEqual(ev[0]["type"], "stop")
        self.assertFalse(p["tp1_done"])

    def test_gap_down_fills_at_open(self):
        p = self.mk()
        ev = paper.process_candle(p, 85, 86, 80, 82, M15, CFG)
        self.assertLessEqual(ev[0]["price"], 85)

    def test_tp1_moves_stop_to_breakeven(self):
        p = self.mk()
        ev = paper.process_candle(p, 100, 111, 99, 110, M15, CFG)
        self.assertEqual(ev[0]["type"], "tp1")
        self.assertGreater(p["stop"], p["entry"])
        self.assertAlmostEqual(p["qty_open"], 1.0)

    def test_costs_make_flat_trade_lose(self):
        p = self.mk()
        paper.time_exit(p, 25 * 3_600_000, p["entry"], CFG)
        self.assertLess(p["realized"], 0)


class EngineAndBacktest(unittest.TestCase):
    def test_live_cycles_run_and_state_consistent(self):
        client = FakeClient(SYMS)
        state = engine.new_state(CFG, client.now)
        cfg = {**CFG, "entry": {**CFG["entry"], "score_threshold": 50}}
        entered = 0
        for k in range(300):
            now = client.now + k * M15
            client.now = now
            rep = engine.run_cycle(client, cfg, state, now, NEUTRAL)
            self.assertFalse(rep["errors"], rep["errors"])
            entered += sum(1 for a in rep["actions"] if a["type"] == "entry")
            eq = state["cash"] + sum(p["qty_open"] * p["mark"] for p in state["positions"].values())
            self.assertAlmostEqual(eq, state["equity"], places=4)
            self.assertGreaterEqual(state["cash"], -1e-6)
            self.assertLessEqual(len(state["positions"]), cfg["risk"]["max_open_positions"])
            client.rows = {s: r + FakeClient([s], n=len(r) + 400).rows[s][len(r):] for s, r in client.rows.items()} \
                if (client.now + M15) > client.rows[SYMS[0]][-1][0] else client.rows
        st = engine.stats(state)
        self.assertIn("profit_factor", st)
        engine.snapshot(state, cfg)

    def test_backtest_runs_no_lookahead_crash(self):
        client = FakeClient(SYMS, n=5000)
        start = client.start + 800 * M15
        end = client.start + 4800 * M15
        feats = B.prepare(client, SYMS, start, end)
        cfg = {**CFG, "entry": {**CFG["entry"], "score_threshold": 55}}
        res = B.run(feats, cfg, start, end)
        s = B.summarize(res, cfg, feats["BTCUSDT"], start, end)
        self.assertGreater(len(res["curve"]), 100)
        for t in res["trades"]:
            self.assertLess(t["fees"], t["entry"] * t["qty"] * 0.05)
        self.assertIsNotNone(s["max_drawdown_pct"])


if __name__ == "__main__":
    unittest.main()
