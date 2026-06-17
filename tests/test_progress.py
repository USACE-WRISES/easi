"""Offline test: the assess() progress counter advances to total (no network)."""
from __future__ import annotations

import asyncio

from easi import assessment
from easi.metrics.base import AnalysisContext, MetricResult


def _stub_offline(monkeypatch):
    monkeypatch.setattr(assessment.streamcat, "metrics_by_comid", lambda *a, **k: {})
    monkeypatch.setattr(assessment.nlcd, "watershed_landcover", lambda *a, **k: {})
    monkeypatch.setattr(assessment.wbd, "huc12_at_point", lambda *a, **k: None)
    monkeypatch.setattr(assessment.threedep, "reach_geomorphology", lambda *a, **k: {})
    monkeypatch.setattr(assessment.bieger, "bankfull_geometry", lambda *a, **k: {
        "width_m": 5.0, "depth_m": 1.0, "area_m2": 5.0,
        "division": "USA", "division_name": "National curve", "regional": False})


def _fake_registry(n):
    def mk(mid):
        return lambda ctx: MetricResult(mid, value=1, value_text="x", rating="Good",
                                        confidence="M", source="test")
    return {f"fn{i}": mk(f"fn{i}") for i in range(n)}


def test_progress_counts_to_total(monkeypatch):
    _stub_offline(monkeypatch)
    monkeypatch.setattr(assessment.registry, "REGISTRY", _fake_registry(3))
    ctx = AnalysisContext(lat=40.0, lon=-83.0, comid=1, drainage_area_sqkm=50.0)
    prog: dict = {}
    asyncio.run(assessment.assess(ctx, progress=prog))
    assert prog["total"] == 3
    assert prog["done"] == 3       # every adapter advanced the counter


def test_progress_respects_selection(monkeypatch):
    _stub_offline(monkeypatch)
    monkeypatch.setattr(assessment.registry, "REGISTRY", _fake_registry(4))
    ctx = AnalysisContext(lat=40.0, lon=-83.0, comid=1, drainage_area_sqkm=50.0)
    prog: dict = {}
    asyncio.run(assessment.assess(ctx, metric_ids=["fn0", "fn1"], progress=prog))
    assert prog["total"] == 2 and prog["done"] == 2   # only the selected adapters counted
