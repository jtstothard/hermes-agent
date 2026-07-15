"""Tests for the shared Fibonacci helper."""

import pytest

from utils import fibonacci


def test_fibonacci_returns_base_cases():
    assert fibonacci(0) == 0
    assert fibonacci(1) == 1


def test_fibonacci_returns_sequence_values():
    assert [fibonacci(n) for n in range(8)] == [0, 1, 1, 2, 3, 5, 8, 13]


def test_fibonacci_rejects_negative_values():
    with pytest.raises(ValueError, match="n must be non-negative"):
        fibonacci(-1)


def test_fibonacci_rejects_non_integer_values():
    with pytest.raises(TypeError, match="n must be an integer"):
        fibonacci(1.5)

    with pytest.raises(TypeError, match="n must be an integer"):
        fibonacci(True)
