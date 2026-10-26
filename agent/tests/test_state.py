"""Тесты состояния с историей."""

from __future__ import annotations

import pytest

from kupol.core.state import State
from tests.conftest import make_telemetry


def feed(state: State, n: int, step_s: float = 1.0, **kw: object) -> None:
    for i in range(n):
        state.update(make_telemetry(i * step_s, **kw))


def test_empty_state() -> None:
    st = State()
    assert st.latest() is None and st.now is None
    assert st.series("temp_c") == []
    assert st.slope("temp_c", 5) is None
    assert st.age("temp_c") is None
    assert st.previous("temp_c") is None


def test_update_and_latest() -> None:
    st = State()
    feed(st, 3)
    latest = st.latest()
    assert latest is not None and (latest.ts - make_telemetry(0).ts).total_seconds() == 2
    assert "temp_c" in st.fields()


def test_ring_buffer_evicts_old() -> None:
    st = State(history_minutes=1)
    feed(st, 200)
    snaps = st.snapshots()
    span = (snaps[-1].ts - snaps[0].ts).total_seconds()
    assert span <= 60 and len(snaps) < 200


def test_out_of_order_snapshot_ignored() -> None:
    st = State()
    st.update(make_telemetry(10))
    st.update(make_telemetry(5))
    assert len(st.snapshots()) == 1


def test_series_window() -> None:
    st = State()
    feed(st, 120)
    assert len(st.series("temp_c", 1)) == 61  # 60 с окна включительно
    assert len(st.series("temp_c")) == 120
    assert st.series("unknown_field") == []


def test_slope_linear() -> None:
    st = State()
    for i in range(0, 300):
        st.update(make_telemetry(i, temp_c=20.0 + 0.5 * (i / 60.0)))  # +0.5 °C/мин
    assert st.slope("temp_c", 4) == pytest.approx(0.5, rel=1e-6)


def test_slope_needs_enough_data() -> None:
    st = State()
    feed(st, 2)
    assert st.slope("temp_c", 5) is None
    st2 = State()
    feed(st2, 30)  # 30 с покрывают меньше половины окна в 5 минут
    assert st2.slope("temp_c", 5) is None


def test_slope_flat_is_zero() -> None:
    st = State()
    feed(st, 200)
    assert st.slope("temp_c", 3) == pytest.approx(0.0, abs=1e-12)


def test_age_since_last_change() -> None:
    st = State()
    for i in range(10):
        st.update(make_telemetry(i, temp_c=22.0 + (0.1 if i < 4 else 0.0) * i))
    # последнее изменение на i=3 (22.3), затем значения 22.0 на i=4 (изменение), дальше 22.0
    assert st.age("temp_c") == pytest.approx(5.0)
    assert st.age("humidity_pct") == pytest.approx(9.0)


def test_previous_returns_second_last() -> None:
    st = State()
    st.update(make_telemetry(0, temp_c=20.0))
    st.update(make_telemetry(1, temp_c=21.0))
    st.update(make_telemetry(2, temp_c=22.0))
    assert st.previous("temp_c") == 21.0
