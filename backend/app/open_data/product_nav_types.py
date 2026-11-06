"""Reviewed actual unit-NAV classifications; money income is excluded."""
from __future__ import annotations

NAV_UNIT_TYPES = frozenset({
    "股票型", "混合型", "混合型-偏股", "混合型-偏债", "混合型-灵活", "混合型-股债平衡",
    "债券型", "债券型-长债", "债券型-中短债", "债券型-利率债", "债券型-信用债",
    "债券型-混合一级", "债券型-混合二级", "指数型-股票", "指数型-固收",
    "FOF-稳健型", "FOF-均衡型", "FOF-进取型", "REITs", "QDII", "保本型",
    "QDII-混合偏股", "QDII-普通股票", "QDII-纯债", "QDII-混合灵活", "QDII-混合债",
    "QDII-商品", "QDII-FOF", "QDII-混合平衡", "QDII-REITs",
})
