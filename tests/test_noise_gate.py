"""Noise-aware ABBA promotion: the verdict must not promote measurement noise."""
import random

import pytest

from aka.legacy.evaluation.abba import verification_schedule
from aka.light.acceptance.noise_gate import NoisePolicy, decide, shape_regressions, spread_pct
from aka.light.acceptance.verifier import score_verification_payload


def _payload(incumbent, candidate, *, inc_shapes=None, cand_shapes=None):
    """Build an ABBA payload in schedule order from per-repeat latencies."""
    repeats = len(incumbent)
    rows = []
    for step in verification_schedule(repeats):
        side = step["revision"]
        latency = (incumbent if side == "incumbent" else candidate)[step["repeat"]]
        result = {"all_pass": True, "latency_us_geomean": latency}
        shapes = inc_shapes if side == "incumbent" else cand_shapes
        if shapes is not None:
            result["latency_us_by_shape"] = shapes[step["repeat"]]
        rows.append(dict(step, exit_code=0, result=result))
    return {"schema_version": 1, "runs": rows}, verification_schedule(repeats)


def _score(incumbent, candidate, min_pct=0.0, policy=NoisePolicy(), **kw):
    payload, schedule = _payload(incumbent, candidate, **kw)
    return score_verification_payload(payload, schedule=schedule, repeats=len(incumbent),
                                      min_improvement_pct=min_pct, noise_policy=policy)


def test_clear_improvement_passes():
    verdict = _score([100.0, 101.0, 100.5, 100.2], [90.0, 90.5, 90.2, 90.1])
    assert verdict.passed, verdict.error
    assert verdict.noise["passed"] is True


def test_win_inside_incumbent_noise_is_rejected():
    # Incumbent varies by 4%; candidate is 1% faster on average.
    verdict = _score([100.0, 104.0, 100.0, 104.0], [99.0, 103.0, 99.0, 102.9])
    assert not verdict.passed
    assert "noise" in verdict.error


def test_inconsistent_pairs_are_rejected():
    verdict = _score([100.0, 100.0, 100.0, 100.0], [80.0, 101.0, 80.0, 80.0],
                     policy=NoisePolicy(noise_multiplier=0.0))
    assert not verdict.passed
    assert "paired repeats [1]" in verdict.error


def test_per_shape_regression_cap_blocks_hidden_regression():
    inc = [{"a": 100.0, "b": 100.0}] * 4
    cand = [{"a": 50.0, "b": 120.0}] * 4  # GeoMean improves ~22.5%, Shape b regresses 20%.
    verdict = _score([100.0] * 4, [77.46] * 4, inc_shapes=inc, cand_shapes=cand)
    assert not verdict.passed
    assert "Shape b regressed" in verdict.error
    relaxed = _score([100.0] * 4, [77.46] * 4, inc_shapes=inc, cand_shapes=cand,
                     policy=NoisePolicy(max_shape_regression_pct=None))
    assert relaxed.passed


def test_min_improvement_threshold_still_applies():
    verdict = _score([100.0] * 4, [99.0] * 4, min_pct=2.0)
    assert not verdict.passed and "did not exceed" in verdict.error


def test_single_pair_recorded_path_still_works():
    # The recorded Gateway verifier scores one aggregated pair (repeats=1).
    verdict = _score([100.0], [90.0])
    assert verdict.passed, verdict.error


def test_old_rule_promotes_noise_new_rule_does_not():
    """A/A trials (identical code, 2% jitter): count false promotions."""
    rng = random.Random(0)
    false_new = 0
    false_old = 0
    trials = 400
    for _ in range(trials):
        inc = [100.0 * (1 + rng.gauss(0, 0.02)) for _ in range(4)]
        cand = [100.0 * (1 + rng.gauss(0, 0.02)) for _ in range(4)]
        false_new += _score(inc, cand).passed
        # Old default: one A/B/B/A block (2 per side), point estimate > 0%.
        old = _score(inc[:2], cand[:2], policy=NoisePolicy(noise_multiplier=0.0,
                     max_shape_regression_pct=None, require_paired_agreement=False))
        false_old += old.passed
    assert false_old / trials > 0.4          # roughly half of A/A trials "win"
    assert false_new / trials < 0.05         # noise-aware gate stays below 5%


def test_helpers():
    assert spread_pct([100.0, 110.0]) == pytest.approx(10.0)
    assert spread_pct([100.0]) is None
    assert shape_regressions([{"x": 110.0}], [{"x": 100.0}]) == {"x": pytest.approx(10.0)}
    assert decide(improvement_pct=5.0, paired=[(95, 100), (95, 100)],
                  incumbent_latencies=[100, 100]).passed


def test_policy_from_environment_and_validation():
    policy = NoisePolicy.from_environment({"ATREX_ABBA_NOISE_MULTIPLIER": "2",
                                           "ATREX_ABBA_MAX_SHAPE_REGRESSION_PCT": "off",
                                           "ATREX_ABBA_REQUIRE_PAIRED_AGREEMENT": "0"})
    assert policy == NoisePolicy(2.0, None, False)
    with pytest.raises(ValueError):
        NoisePolicy(noise_multiplier=-1)
    with pytest.raises(ValueError):
        NoisePolicy.from_environment({"ATREX_ABBA_REQUIRE_PAIRED_AGREEMENT": "maybe"})
