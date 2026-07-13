import numpy as np
import pandas as pd
import pytest

from src.stock_analyzer import StockTrendAnalyzer


def _bars(close: np.ndarray) -> pd.DataFrame:
    close = np.asarray(close, dtype=float)
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=len(close), freq="D"),
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(len(close), 1000.0),
        }
    )


def test_kdj_flat_market_falls_back_to_neutral_values() -> None:
    analyzer = StockTrendAnalyzer()
    result = analyzer.analyze(_bars(np.full(60, 10.0)), "TEST")

    assert result.kdj_k == pytest.approx(50.0)
    assert result.kdj_d == pytest.approx(50.0)
    assert result.kdj_j == pytest.approx(50.0)
    assert result.kdj_signal == "KDJ中性，短线方向暂不明确"


def test_kdj_values_are_exposed_in_dict_and_formatted_analysis() -> None:
    analyzer = StockTrendAnalyzer()
    close = np.linspace(10.0, 30.0, 60) + np.sin(np.arange(60) / 3)
    result = analyzer.analyze(_bars(close), "TEST")

    payload = result.to_dict()
    assert payload["kdj_k"] == pytest.approx(result.kdj_k)
    assert payload["kdj_d"] == pytest.approx(result.kdj_d)
    assert payload["kdj_j"] == pytest.approx(result.kdj_j)
    assert payload["kdj_signal"]
    assert np.isfinite([result.kdj_k, result.kdj_d, result.kdj_j]).all()

    formatted = analyzer.format_analysis(result)
    assert "KDJ指标(9,3,3)" in formatted
    assert f"K: {result.kdj_k:.1f}" in formatted
    assert result.kdj_signal in formatted
