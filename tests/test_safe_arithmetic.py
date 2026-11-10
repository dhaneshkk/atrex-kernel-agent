import pytest

from aka.legacy.execution.profiling.common.compute_utilization import _parse_variables, safe_arithmetic


def test_arithmetic_and_variables():
    variables = _parse_variables(["BM=128", "BN=128", "K=4096"])
    assert safe_arithmetic("2*BM*BN*K", variables) == 2 * 128 * 128 * 4096
    assert safe_arithmetic("(BM*K + BN*K + BM*BN)*2", variables) == (128 * 4096 * 2 + 128 * 128) * 2
    assert safe_arithmetic("-2**10 // 3 % 7") == float(-2**10 // 3 % 7)


@pytest.mark.parametrize("expr", [
    "__import__('os').system('true')",
    "(1).__class__",
    "open('x')",
    "[1][0]",
    "2**100000",
    "unknown*2",
])
def test_rejects_code_and_unknowns(expr):
    with pytest.raises(ValueError):
        safe_arithmetic(expr)


def test_bad_variable_syntax():
    with pytest.raises(ValueError):
        _parse_variables(["1BM=3"])
