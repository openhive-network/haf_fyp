"""scripts/processor_timings_report.py: the per-processor timings report of a replay.

The fixture is three batches (blocks 990001..1020000) of the shipped dispatcher's output,
recorded with haf_fyp.processor_timings on over the HAF stub and the hivemind model
(tests/hivemind_stub.sql): one batch waiting for hivemind, the batch that backfilled the
window, and one following it. The driver's own lines are around them, as haf_app_driver.py
writes them. The expected numbers are recomputed from the fixture here, independently of
the script, so a report that sums the wrong field or drops a batch disagrees with them.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "processor_timings_report.py"
FIXTURE = Path(__file__).resolve().parent / "data" / "processor_timings_driver_log.txt"
FIRST, LAST = 990001, 1020000

PROCESSORS = [
    "stage_operations", "unsubscribe", "subscribe", "hivemind_subscriptions", "hivemind_posts",
    "hivemind_interactions", "hivemind_engagement", "payout_windows",
]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("processor_timings_report", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["processor_timings_report"] = module
    spec.loader.exec_module(module)
    return module


report_script = _load()


def _records(lines: list[str], kind: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in lines:
        m = re.search(kind + r" (\{.*\})\s*$", line)
        if m:
            out.append(json.loads(m.group(1)))
    return out


def _run(tmp_path: Path, lines: list[str], *extra: str, first: int = FIRST,
         last: int = LAST) -> tuple[int, Path]:
    log = tmp_path / "driver.log"
    log.write_text("".join(lines), encoding="utf-8")
    out = tmp_path / "report.json"
    rc: int = report_script.main(["--log", str(log), "--out", str(out), "--first-block", str(first),
                             "--last-block", str(last), "--sync-seconds", "12.5", *extra])
    return rc, out


@pytest.fixture
def lines() -> list[str]:
    return FIXTURE.read_text(encoding="utf-8").splitlines(keepends=True)


def test_the_fixture_is_three_whole_batches(lines: list[str]) -> None:
    """Guards the fixture itself: the tests below lean on its shape."""
    timings = _records(lines, "haf_fyp_processor_timing")
    assert len(timings) == 3 * len(PROCESSORS)
    assert len(_records(lines, "haf_fyp_operation_counts")) == 3
    assert any("processed in" in line for line in lines), "the driver's own lines are part of the fixture"


def test_a_recorded_run_makes_the_report(tmp_path: Path, lines: list[str],
                                         capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = _run(tmp_path, lines, "--uninstrumented-sync-seconds", "11.0")
    assert rc == 0, capsys.readouterr().err
    report = json.loads(out.read_text(encoding="utf-8"))

    timings = _records(lines, "haf_fyp_processor_timing")
    want: defaultdict[str, float] = defaultdict(float)
    for t in timings:
        want[t["processor"]] += t["seconds"]
    assert list(report["processors"]) == PROCESSORS
    for p in PROCESSORS:
        assert report["processors"][p]["seconds"] == pytest.approx(want[p], abs=1e-3), p
    assert report["processing_seconds"] == pytest.approx(sum(want.values()), abs=1e-3)
    assert sum(e["share"] for e in report["processors"].values()) == pytest.approx(1.0, abs=1e-3)
    assert report["batches"] == 3
    assert (report["first_block"], report["last_block"]) == (FIRST, LAST)
    assert report["sync_wall_seconds"] == 12.5
    assert report["uninstrumented_sync_wall_seconds"] == 11.0
    assert report["blocks_per_second"] == pytest.approx(30000 / 12.5, abs=0.1)
    counts = _records(lines, "haf_fyp_operation_counts")
    assert report["instrumentation_seconds"] == pytest.approx(sum(c["seconds"] for c in counts), abs=1e-3)
    assert len(report["limits"]) == 3


def test_the_index_restore_is_reported_beside_the_sync_and_added_to_it(
        tmp_path: Path, lines: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = _run(tmp_path, lines, "--uninstrumented-sync-seconds", "11.0",
                   "--index-restore-seconds", "3.4", "--uninstrumented-index-restore-seconds", "2.5")
    assert rc == 0, capsys.readouterr().err
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["sync_wall_seconds"] == 12.5
    assert report["index_restore_seconds"] == 3.4
    assert report["sync_plus_restore_wall_seconds"] == 15.9
    assert report["uninstrumented_index_restore_seconds"] == 2.5
    assert report["uninstrumented_sync_plus_restore_wall_seconds"] == 13.5
    table = capsys.readouterr().out
    assert "index restore 3.4s, sync plus restore 15.9s" in table
    assert "uninstrumented index restore 2.5s, sync plus restore 13.5s" in table


def test_without_a_restore_time_the_report_has_no_total(tmp_path: Path, lines: list[str],
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = _run(tmp_path, lines)
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["index_restore_seconds"] is None
    assert report["sync_plus_restore_wall_seconds"] is None
    assert "index restore" not in capsys.readouterr().out


def test_a_negative_restore_time_writes_no_report(tmp_path: Path, lines: list[str],
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = _run(tmp_path, lines, "--index-restore-seconds", "-1")
    assert rc == 1
    assert not out.exists()
    assert "negative" in capsys.readouterr().err


def test_each_batch_counts_towards_the_million_its_first_block_is_in(tmp_path: Path, lines: list[str]) -> None:
    rc, out = _run(tmp_path, lines)
    assert rc == 0
    buckets = json.loads(out.read_text(encoding="utf-8"))["per_million"]
    assert [(b["first_block"], b["last_block"], b["batches"]) for b in buckets] == [
        (1, 1_000_000, 1), (1_000_001, 2_000_000, 2)]
    by_batch: defaultdict[int, dict[str, float]] = defaultdict(dict)
    for t in _records(lines, "haf_fyp_processor_timing"):
        by_batch[t["first_block"]][t["processor"]] = t["seconds"]
    p = "hivemind_interactions"
    assert buckets[0]["seconds"][p] == pytest.approx(by_batch[990001][p], abs=1e-3)
    assert buckets[1]["seconds"][p] == pytest.approx(by_batch[1000001][p] + by_batch[1010001][p], abs=1e-3)
    for b in buckets:
        assert b["total_seconds"] == pytest.approx(sum(b["seconds"].values()), abs=1e-2)


def test_rows_read_are_the_batch_counts_of_the_types_a_processor_reads(tmp_path: Path, lines: list[str]) -> None:
    rc, out = _run(tmp_path, lines)
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    per_type: defaultdict[str, int] = defaultdict(int)
    by_id: defaultdict[str, int] = defaultdict(int)
    for c in _records(lines, "haf_fyp_operation_counts"):
        for row in c["counts"]:
            per_type[row["op_type"]] += row["rows"]
            by_id[row["custom_json_id"]] += row["rows"]
    assert per_type["custom_json_operation"] > 0, "the fixture's batches hold custom_json"
    assert report["operations"] == dict(per_type)
    assert report["custom_json_by_id"] == dict(sorted(by_id.items()))
    assert report["rows_read"]["stage_operations"] == {"custom_json_operation": per_type["custom_json_operation"]}
    assert report["rows_read"]["subscribe"] == {"custom_json_operation": per_type["custom_json_operation"]}
    # The hivemind_* processors read hivemind's tables, not operations.
    assert report["rows_read"]["hivemind_interactions"] == {}
    assert report["rows_read"]["payout_windows"] == {}


def test_the_backfill_is_reported_from_its_own_line(tmp_path: Path, lines: list[str]) -> None:
    rc, out = _run(tmp_path, lines)
    assert rc == 0
    [want] = _records(lines, "haf_fyp_backfill")
    backfill = json.loads(out.read_text(encoding="utf-8"))["backfill"]
    assert backfill == {k: want[k] for k in ("seconds", "hivemind_block", "first_block", "history_days",
                                             "posts", "interactions", "commenters")}


def test_a_run_without_a_backfill_reports_none(tmp_path: Path, lines: list[str],
                                               capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = _run(tmp_path, [line for line in lines if "haf_fyp_backfill" not in line])
    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["backfill"] is None
    assert "no backfill in this run" in capsys.readouterr().out


def test_the_summary_table_shows_every_processor_and_the_backfill(
        tmp_path: Path, lines: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run(tmp_path, lines)
    assert rc == 0
    table = capsys.readouterr().out
    for p in PROCESSORS:
        assert re.search(rf"^{p}\s+\d+\.\d\d\s", table, re.M), p
    assert "0M-1M" in table and "1M-2M" in table
    assert "sync wall 12.5s over 3 batches" in table
    [want] = _records(lines, "haf_fyp_backfill")
    assert f"at hivemind block {want['hivemind_block']}" in table


def _drop(lines: list[str], needle: str) -> list[str]:
    hit = [i for i, line in enumerate(lines) if needle in line]
    assert hit, needle
    return lines[:hit[0]] + lines[hit[0] + 1:]


def _edit(lines: list[str], needle: str, old: str, new: str) -> list[str]:
    i = next(i for i, line in enumerate(lines) if needle in line)
    assert old in lines[i]
    return lines[:i] + [lines[i].replace(old, new, 1)] + lines[i + 1:]


BROKEN: dict[str, Callable[[list[str]], list[str]]] = {
    "no timings at all": lambda ls: [line for line in ls if "haf_fyp_" not in line],
    "an empty log": lambda ls: [],
    "a line that is not JSON": lambda ls: _edit(ls, '"processor" : "hivemind_interactions"', '"seconds"', '"seconds'),
    "a negative time": lambda ls: _edit(ls, '"processor" : "hivemind_interactions"', '"seconds" : ', '"seconds" : -'),
    "a time that is text": lambda ls: _edit(ls, '"processor" : "subscribe"', '"seconds" : ', '"seconds" : "1" , "x" : '),
    "a processor missing": lambda ls: _drop(ls, '"processor" : "hivemind_posts"'),
    "a processor timed twice": lambda ls: ls + [next(line for line in ls if '"processor" : "hivemind_engagement"' in line)],
    "two backfills": lambda ls: ls + [next(line for line in ls if "haf_fyp_backfill" in line)],
    "a backfill without its counts": lambda ls: _edit(ls, "haf_fyp_backfill", '"posts" : ', '"posts_" : '),
    "an unknown processor": lambda ls: _edit(ls, '"processor" : "subscribe"', '"subscribe"', '"subscribe_v2"'),
    "no operation counts for a batch": lambda ls: _drop(ls, "haf_fyp_operation_counts"),
    "a missing batch": lambda ls: [line for line in ls if '"first_block" : 1000001,' not in line],
}


@pytest.mark.parametrize("broken", BROKEN.values(), ids=BROKEN.keys())
def test_a_broken_timing_set_writes_no_report(tmp_path: Path, lines: list[str],
                                               broken: Callable[[list[str]], list[str]],
                                               capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = _run(tmp_path, broken(lines))
    assert rc == 1
    assert not out.exists()
    assert capsys.readouterr().err.startswith("no report: ")


@pytest.mark.parametrize(("first", "last"), [(980001, LAST), (FIRST, LAST + 10000), (FIRST, LAST - 10000)])
def test_batches_that_do_not_cover_the_range_write_no_report(tmp_path: Path, lines: list[str], first: int,
                                                              last: int, capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = _run(tmp_path, lines, first=first, last=last)
    assert rc == 1
    assert not out.exists()
    assert "no report: " in capsys.readouterr().err


def test_a_missing_log_writes_no_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "report.json"
    rc: int = report_script.main(["--log", str(tmp_path / "absent.log"), "--out", str(out), "--first-block", "1",
                             "--last-block", "10", "--sync-seconds", "1"])
    assert rc == 1
    assert not out.exists()
    assert "no report: " in capsys.readouterr().err
