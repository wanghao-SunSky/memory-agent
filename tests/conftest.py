from __future__ import annotations

import json
import os
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://memory:memory@localhost:5432/memory_test")
os.environ.setdefault("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")  # 测试默认不导出；live 组可自行打开
os.environ["JOB_BACKOFF_BASE"] = "0"  # 测试里退避立即到期

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from memory_agent.api import create_app
from memory_agent.db import get_engine
from memory_agent.migrate import migrate

TZ = timezone(timedelta(hours=8))
TABLES = "spaces, episodes, facts, retrieval_keys, memory_ops, jobs, deletion_log"


@pytest.fixture(scope="session", autouse=True)
def _db():
    assert "memory_test" in os.environ["DATABASE_URL"], "测试只能跑在 memory_test 库"
    migrate()
    with get_engine().begin() as c:
        c.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture(autouse=True)
def _isolate_jobs():
    yield
    with get_engine().begin() as c:  # 别让一个用例残留的作业被下一个用例领走
        c.execute(text("UPDATE jobs SET status='cancelled' WHERE status IN ('queued','running')"))


@pytest.fixture
def engine():
    return get_engine()


@pytest.fixture
def owner():
    return "t-" + uuid.uuid4().hex[:10]


@pytest.fixture(scope="session")
def client():
    return TestClient(create_app(instrument=False))


def at(day: int, hour: int = 10, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, 0, tzinfo=TZ)


# ---- 分组通过率汇总 ----
_results: dict[str, list[tuple[str, str]]] = defaultdict(list)


def _group(item) -> str:
    live = item.get_closest_marker("live") is not None
    reg = "regression" in item.nodeid
    return ("live" if live else "offline") + ("-回归20条" if reg else "-单元/集成")


def pytest_runtest_logreport(report):
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        item_group = getattr(report, "_group", None)
        _results[item_group].append((report.nodeid, report.outcome))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    rep._group = _group(item)


def pytest_terminal_summary(terminalreporter):
    if not _results:
        return
    terminalreporter.section("记得住你：分组通过率")
    summary = {}
    for g, rs in sorted(_results.items()):
        ok = sum(1 for _, o in rs if o == "passed")
        terminalreporter.write_line(f"{g}: {ok}/{len(rs)} 通过 ({ok / len(rs):.0%})")
        fails = [n for n, o in rs if o != "passed"]
        for n in fails:
            terminalreporter.write_line(f"    FAIL {n}")
        summary[g] = {"passed": ok, "total": len(rs), "failed": fails}
    out = Path(__file__).resolve().parents[1] / "reports"
    out.mkdir(exist_ok=True)
    tag = "live" if any(k.startswith("live") for k in summary) else "offline"
    (out / f"last_{tag}.json").write_text(json.dumps({"run_at": datetime.now(TZ).isoformat(), "groups": summary}, ensure_ascii=False, indent=2))
