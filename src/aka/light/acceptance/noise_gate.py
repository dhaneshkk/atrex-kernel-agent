"""Noise-aware promotion checks for paired A/B/B/A latency evidence.

The original Light rule promoted whenever the GeoMean latency reduction was
strictly greater than ``min_improvement_pct`` (default 0%), using a single
A/B/B/A block by default. That point estimate cannot distinguish a real win
from run-to-run jitter, and repeated campaigns ratchet on noise.

This module adds three deterministic checks on top of the existing threshold:

1. **Paired agreement** - every repeat's candidate/incumbent ratio must show an
   improvement. Each repeat measures both sides inside the same allocation, so
   the pair cancels slow drift; under a symmetric-noise null the chance that all
   ``k`` pairs agree by luck is ``2**-k``.
2. **Noise floor** - the GeoMean improvement must exceed the incumbent's own
   observed spread (``max/min - 1``) times ``noise_multiplier``. Measuring
   the same code twice tells us how large a difference jitter alone produces.
3. **Per-Shape regression cap** - no individual Shape may get slower than
   ``max_shape_regression_pct``. A GeoMean win must not hide a production
   Shape regression.

Every check is pure, uses no randomness, and returns its own diagnostics so a
rejected candidate explains itself.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

DEFAULT_NOISE_MULTIPLIER = 1.0
DEFAULT_MAX_SHAPE_REGRESSION_PCT = 5.0
NOISE_MULTIPLIER_ENV = "ATREX_ABBA_NOISE_MULTIPLIER"
MAX_SHAPE_REGRESSION_ENV = "ATREX_ABBA_MAX_SHAPE_REGRESSION_PCT"
PAIRED_AGREEMENT_ENV = "ATREX_ABBA_REQUIRE_PAIRED_AGREEMENT"


@dataclass(frozen=True)
class NoisePolicy:
    noise_multiplier: float = DEFAULT_NOISE_MULTIPLIER
    max_shape_regression_pct: float | None = DEFAULT_MAX_SHAPE_REGRESSION_PCT
    require_paired_agreement: bool = True

    def __post_init__(self) -> None:
        if not math.isfinite(self.noise_multiplier) or self.noise_multiplier < 0:
            raise ValueError("noise_multiplier must be finite and non-negative")
        cap = self.max_shape_regression_pct
        if cap is not None and (not math.isfinite(cap) or cap < 0):
            raise ValueError("max_shape_regression_pct must be finite and non-negative, or None")

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "NoisePolicy":
        """Read optional operator overrides; malformed values fail closed."""
        environment = os.environ if environment is None else environment
        multiplier = float(environment.get(NOISE_MULTIPLIER_ENV, DEFAULT_NOISE_MULTIPLIER))
        raw_cap = environment.get(MAX_SHAPE_REGRESSION_ENV, str(DEFAULT_MAX_SHAPE_REGRESSION_PCT))
        cap = None if raw_cap.strip().lower() in {"", "none", "off"} else float(raw_cap)
        raw_paired = environment.get(PAIRED_AGREEMENT_ENV, "1").strip().lower()
        if raw_paired not in {"0", "1", "true", "false", "yes", "no"}:
            raise ValueError(f"{PAIRED_AGREEMENT_ENV} must be a boolean")
        return cls(multiplier, cap, raw_paired in {"1", "true", "yes"})


@dataclass
class NoiseDecision:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    paired_ratios: list[float] = field(default_factory=list)
    incumbent_spread_pct: float | None = None
    noise_floor_pct: float = 0.0
    worst_shape: str | None = None
    worst_shape_regression_pct: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "paired_ratios": list(self.paired_ratios),
            "incumbent_spread_pct": self.incumbent_spread_pct,
            "noise_floor_pct": self.noise_floor_pct,
            "worst_shape": self.worst_shape,
            "worst_shape_regression_pct": self.worst_shape_regression_pct,
        }


def _geomean(values: Iterable[float]) -> float | None:
    values = list(values)
    if not values or any(not math.isfinite(v) or v <= 0 for v in values):
        return None
    return math.exp(sum(math.log(v) for v in values) / len(values))


def spread_pct(values: list[float]) -> float | None:
    """Relative range of repeated measurements of the same revision."""
    if len(values) < 2 or any(not math.isfinite(v) or v <= 0 for v in values):
        return None
    return (max(values) / min(values) - 1.0) * 100.0


def shape_regressions(
    candidate_shapes: list[Mapping[str, float]],
    incumbent_shapes: list[Mapping[str, float]],
) -> dict[str, float]:
    """Per-Shape latency change in percent (positive means the candidate is slower).

    Shapes that are missing on either side are skipped; completeness is already
    enforced by the evaluator before a run counts as ``all_pass``.
    """
    names = set()
    for row in (*candidate_shapes, *incumbent_shapes):
        names.update(row)
    changes: dict[str, float] = {}
    for name in sorted(names):
        candidate = _geomean(row[name] for row in candidate_shapes if name in row)
        incumbent = _geomean(row[name] for row in incumbent_shapes if name in row)
        if candidate and incumbent:
            changes[name] = (candidate / incumbent - 1.0) * 100.0
    return changes


def decide(
    *,
    improvement_pct: float,
    paired: list[tuple[float, float]],
    incumbent_latencies: list[float],
    candidate_shapes: list[Mapping[str, float]] = (),
    incumbent_shapes: list[Mapping[str, float]] = (),
    policy: NoisePolicy = NoisePolicy(),
) -> NoiseDecision:
    """Apply the noise checks. ``paired`` holds ``(candidate, incumbent)`` per repeat."""
    decision = NoiseDecision(passed=True)
    decision.paired_ratios = [c / i for c, i in paired if i > 0]
    if policy.require_paired_agreement and len(decision.paired_ratios) >= 2:
        losing = [index for index, ratio in enumerate(decision.paired_ratios) if ratio >= 1.0]
        if losing:
            decision.passed = False
            decision.reasons.append(
                f"paired repeats {losing} did not improve; the GeoMean win is not consistent across A/B/B/A pairs"
            )
    decision.incumbent_spread_pct = spread_pct(incumbent_latencies)
    if decision.incumbent_spread_pct is not None:
        decision.noise_floor_pct = decision.incumbent_spread_pct * policy.noise_multiplier
        if improvement_pct <= decision.noise_floor_pct:
            decision.passed = False
            decision.reasons.append(
                f"improvement {improvement_pct:.3f}% is within the incumbent's own run-to-run noise "
                f"({decision.noise_floor_pct:.3f}%)"
            )
    if policy.max_shape_regression_pct is not None and candidate_shapes and incumbent_shapes:
        changes = shape_regressions(list(candidate_shapes), list(incumbent_shapes))
        if changes:
            worst = max(changes, key=changes.__getitem__)
            decision.worst_shape, decision.worst_shape_regression_pct = worst, changes[worst]
            if changes[worst] > policy.max_shape_regression_pct:
                decision.passed = False
                decision.reasons.append(
                    f"Shape {worst} regressed {changes[worst]:.3f}% (cap {policy.max_shape_regression_pct:.3f}%)"
                )
    return decision
