from core.data.data_logger import OHLCCandle
from core.exit_sim import simulate_exit

CFG = {"stop_pct": 10, "max_bars": 100, "trail_activation_pct": 100, "trail_distance_pct": 15,
       "levels": [{"gain_pct": 30, "close_pct": 30, "breakeven": True},
                  {"gain_pct": 60, "close_pct": 30, "breakeven": True},
                  {"gain_pct": 100, "close_pct": 20, "breakeven": True}]}


def mk(rows):
    return [OHLCCandle("x", i * 60, o, h, l, c, 0, 1) for i, (o, h, l, c) in enumerate(rows)]


def test_stop_loss():
    assert abs(simulate_exit(mk([(1, 1.02, 0.95, 1.0), (1, 1.0, 0.85, 0.9)]), 0, CFG)[0] + 10) < 1e-6


def test_gap_below_stop_fills_at_open():
    assert abs(simulate_exit(mk([(1, 1.0, 0.99, 1.0), (0.8, 0.8, 0.7, 0.7)]), 0, CFG)[0] + 20) < 1e-6


def test_first_target_then_breakeven():
    assert abs(simulate_exit(mk([(1, 1.35, 0.99, 1.3), (1.3, 1.3, 0.95, 1.0)]), 0, CFG)[0] - 9.0) < 1e-6


def test_all_targets_then_trailing():
    assert abs(simulate_exit(mk([(1, 2.1, 0.99, 2.0), (2.0, 2.0, 1.6, 1.7)]), 0, CFG)[0] - 62.7) < 1e-6


def test_same_candle_stop_beats_target():
    assert simulate_exit(mk([(1, 3.0, 0.5, 0.6)]), 0, CFG)[0] < 0
