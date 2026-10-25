#!/usr/bin/env python3
"""Per-processor timings of a block-processing run, as a JSON report and a table.

    processor_timings_report.py --log DRIVER_LOG --out REPORT.json
        --first-block N --last-block M --sync-seconds S [--uninstrumented-sync-seconds U]

DRIVER_LOG is haf_app_driver.py's output for a run with haf_fyp.processor_timings = on
(db/process_block_range.sql): one `haf_fyp_processor_timing {...}` line per processor
per batch and one `haf_fyp_operation_counts {...}` line per batch. Every other line is
ignored. The batches must cover blocks N..M exactly, each with every processor and one
operation count; anything else -- no timings at all, a line that is not JSON, a negative
time, a missing or unknown processor, a gap or an overlap -- exits 1 naming the problem
and writes no report.

The report holds each processor's wall time over the whole run and per 1,000,000 blocks
(a batch counts towards the million its first block is in), the rows each processor read
from hive.operations_view by op type, the custom_json ids the reblog question turns on,
and the run's wall time as measured by the caller.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The processors db/process_block_range.sql dispatches, in dispatch order.
PROCESSORS: tuple[str, ...] = (
    "process_posts",
    "comments",
    "reblogs",
    "votes",
    "payout_windows",
    "unsubscribe",
    "subscribe",
    "process_interactions",
    "resolve_hivemind_post_ids",
)

BUCKET_BLOCKS = 1_000_000

#: Below this many reblogs of both forms together the counts decide nothing.
REBLOG_SAMPLE = 1000

LIMITS: tuple[str, ...] = (
    "Blocks 1 to 5,000,000 hold little custom_json: the Splinterlands-era volume comes "
    "later, so the custom_json processors' share here understates their share of a full replay.",
    "The shared HAF has no hivemind: hive.app_add_dependency('haf_fyp','hivemind_app') is "
    "skipped there and resolve_hivemind_post_ids finds nothing, so the wall time is haf_fyp's "
    "own processing only.",
)

_LINE = re.compile(r"\b(haf_fyp_processor_timing|haf_fyp_operation_counts) (\{.*\})\s*$")


class TimingsError(Exception):
    """The timings cannot make a report: empty, malformed, or not covering the range."""


@dataclass(frozen=True)
class _Timing:
    first_block: int
    last_block: int
    processor: str
    seconds: float
    reads: tuple[str, ...]


@dataclass(frozen=True)
class _Count:
    op_type: str
    custom_json_id: str | None
    json_action: str | None
    rows: int


@dataclass(frozen=True)
class _Counts:
    first_block: int
    last_block: int
    seconds: float
    counts: tuple[_Count, ...]


def _field(record: Mapping[str, Any], key: str, kind: type | tuple[type, ...], where: str) -> Any:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, kind):
        raise TimingsError(f"{where}: '{key}' is {value!r}")
    return value


def _seconds(record: Mapping[str, Any], where: str) -> float:
    value = float(_field(record, "seconds", (int, float), where))
    if value < 0:
        raise TimingsError(f"{where}: 'seconds' is negative ({value})")
    return value


def _range(record: Mapping[str, Any], where: str) -> tuple[int, int]:
    first = _field(record, "first_block", int, where)
    last = _field(record, "last_block", int, where)
    if first < 1 or last < first:
        raise TimingsError(f"{where}: block range {first}..{last}")
    return first, last


def _timing(record: Mapping[str, Any], where: str) -> _Timing:
    first, last = _range(record, where)
    processor = _field(record, "processor", str, where)
    if processor not in PROCESSORS:
        raise TimingsError(f"{where}: unknown processor {processor!r}")
    reads = _field(record, "reads", list, where)
    if not all(isinstance(r, str) for r in reads):
        raise TimingsError(f"{where}: 'reads' is {reads!r}")
    return _Timing(first, last, processor, _seconds(record, where), tuple(reads))


def _counts(record: Mapping[str, Any], where: str) -> _Counts:
    first, last = _range(record, where)
    counts = []
    for c in _field(record, "counts", list, where):
        if not isinstance(c, dict):
            raise TimingsError(f"{where}: a count is {c!r}")
        rows = _field(c, "rows", int, where)
        if rows < 0:
            raise TimingsError(f"{where}: 'rows' is negative ({rows})")
        cj_id, action = c.get("custom_json_id"), c.get("json_action")
        if not (cj_id is None or isinstance(cj_id, str)) or not (action is None or isinstance(action, str)):
            raise TimingsError(f"{where}: a count is {c!r}")
        counts.append(_Count(_field(c, "op_type", str, where), cj_id, action, rows))
    return _Counts(first, last, _seconds(record, where), tuple(counts))


def parse_log(lines: Iterable[str]) -> tuple[list[_Timing], list[_Counts]]:
    """The timing and operation-count records in a driver log, validated one by one."""
    timings: list[_Timing] = []
    counts: list[_Counts] = []
    for number, line in enumerate(lines, 1):
        m = _LINE.search(line)
        if m is None:
            continue
        where = f"line {number}"
        try:
            record = json.loads(m.group(2))
        except json.JSONDecodeError as exc:
            raise TimingsError(f"{where}: not JSON ({exc})") from exc
        if not isinstance(record, dict):
            raise TimingsError(f"{where}: not a JSON object")
        if m.group(1) == "haf_fyp_processor_timing":
            timings.append(_timing(record, where))
        else:
            counts.append(_counts(record, where))
    return timings, counts


def _batches(timings: list[_Timing], counts: list[_Counts], first_block: int,
             last_block: int) -> list[tuple[tuple[int, int], dict[str, _Timing], _Counts]]:
    by_batch: dict[tuple[int, int], dict[str, _Timing]] = defaultdict(dict)
    for t in timings:
        batch = by_batch[(t.first_block, t.last_block)]
        if t.processor in batch:
            raise TimingsError(f"batch {t.first_block}..{t.last_block}: {t.processor} is timed twice")
        batch[t.processor] = t
    counts_by_batch: dict[tuple[int, int], _Counts] = {}
    for c in counts:
        key = (c.first_block, c.last_block)
        if key in counts_by_batch:
            raise TimingsError(f"batch {key[0]}..{key[1]}: operations are counted twice")
        counts_by_batch[key] = c
    if not by_batch:
        raise TimingsError("no processor timings: was haf_fyp.processor_timings on for the run?")
    for key in by_batch.keys() | counts_by_batch.keys():
        missing = [p for p in PROCESSORS if p not in by_batch.get(key, {})]
        if missing:
            raise TimingsError(f"batch {key[0]}..{key[1]}: no timing for {', '.join(missing)}")
        if key not in counts_by_batch:
            raise TimingsError(f"batch {key[0]}..{key[1]}: no operation counts")
    expected = first_block
    for first, last in sorted(by_batch):
        if first != expected:
            raise TimingsError(f"batch {first}..{last} follows block {expected - 1}: "
                               + ("a gap" if first > expected else "an overlap"))
        expected = last + 1
    if expected != last_block + 1:
        raise TimingsError(f"the batches end at block {expected - 1}, not {last_block}")
    return [(key, by_batch[key], counts_by_batch[key]) for key in sorted(by_batch)]


def _rounded(seconds: float) -> float:
    return round(seconds, 3)


def build_report(timings: list[_Timing], counts: list[_Counts], *, first_block: int, last_block: int,
                 sync_seconds: float, uninstrumented_sync_seconds: float | None = None) -> dict[str, Any]:
    """The report for one run: totals, the per-million split, rows read and the reblog ids.

    :raises TimingsError: the records do not tile ``first_block..last_block`` with every
        processor timed and the operations counted once per batch.
    """
    if sync_seconds < 0 or (uninstrumented_sync_seconds is not None and uninstrumented_sync_seconds < 0):
        raise TimingsError("a sync wall time is negative")
    batches = _batches(timings, counts, first_block, last_block)
    totals: dict[str, float] = dict.fromkeys(PROCESSORS, 0.0)
    rows_read: dict[str, dict[str, int]] = {p: defaultdict(int) for p in PROCESSORS}
    operations: dict[str, int] = defaultdict(int)
    custom_json: dict[str, int] = defaultdict(int)
    reblog_id = follow_reblog = 0
    instrumentation = 0.0
    buckets: dict[int, dict[str, Any]] = {}
    for (first, _last), by_processor, batch_counts in batches:
        per_type: dict[str, int] = defaultdict(int)
        for c in batch_counts.counts:
            per_type[c.op_type] += c.rows
            operations[c.op_type] += c.rows
            if c.custom_json_id is not None:
                custom_json[c.custom_json_id] += c.rows
            if c.custom_json_id == "reblog":
                reblog_id += c.rows
            if c.custom_json_id == "follow" and c.json_action == "reblog":
                follow_reblog += c.rows
        instrumentation += batch_counts.seconds
        index = (first - 1) // BUCKET_BLOCKS
        bucket = buckets.setdefault(index, {
            "first_block": index * BUCKET_BLOCKS + 1,
            "last_block": (index + 1) * BUCKET_BLOCKS,
            "batches": 0,
            "seconds": dict.fromkeys(PROCESSORS, 0.0),
        })
        bucket["batches"] += 1
        for p, t in by_processor.items():
            totals[p] += t.seconds
            bucket["seconds"][p] += t.seconds
            for op_type in t.reads:
                rows_read[p][op_type] += per_type.get(op_type, 0)
    processing = sum(totals.values())
    per_million = []
    for index in sorted(buckets):
        bucket = buckets[index]
        bucket["seconds"] = {p: _rounded(s) for p, s in bucket["seconds"].items()}
        bucket["total_seconds"] = _rounded(sum(bucket["seconds"].values()))
        per_million.append(bucket)
    blocks = last_block - first_block + 1
    report: dict[str, Any] = {
        "first_block": first_block,
        "last_block": last_block,
        "batches": len(batches),
        "sync_wall_seconds": _rounded(sync_seconds),
        "blocks_per_second": round(blocks / sync_seconds, 1) if sync_seconds > 0 else None,
        "uninstrumented_sync_wall_seconds": (
            None if uninstrumented_sync_seconds is None else _rounded(uninstrumented_sync_seconds)),
        "processing_seconds": _rounded(processing),
        "instrumentation_seconds": _rounded(instrumentation),
        "processors": {
            p: {"seconds": _rounded(totals[p]), "share": round(totals[p] / processing, 4) if processing else 0.0}
            for p in PROCESSORS
        },
        "per_million": per_million,
        "rows_read": {p: dict(sorted(r.items())) for p, r in rows_read.items()},
        "operations": dict(sorted(operations.items())),
        "custom_json_by_id": dict(sorted(custom_json.items())),
        "reblog_ids": {
            "custom_json_id_reblog": reblog_id,
            "custom_json_id_follow_payload_reblog": follow_reblog,
            "finding": _reblog_finding(reblog_id, follow_reblog),
        },
        "limits": list(LIMITS),
    }
    return report


def _reblog_finding(reblog_id: int, follow_reblog: int) -> str:
    total = reblog_id + follow_reblog
    if total < REBLOG_SAMPLE:
        return (f"Only {total} reblogs of either form in this range: too few to say which form is "
                "in use. Measure a later range.")
    if follow_reblog > reblog_id:
        return ("The follow/[\"reblog\", ...] form is the one in use: haf_fyp matches only "
                "id='reblog', so it misses most reblogs in this range.")
    return "id='reblog' is at least as common as the follow/[\"reblog\", ...] form in this range."


def summary_table(report: Mapping[str, Any]) -> str:
    """The report's per-processor seconds, total and per million blocks, as fixed-width text."""
    buckets = report["per_million"]
    heads = [f"{b['first_block'] // BUCKET_BLOCKS}M-{b['last_block'] // BUCKET_BLOCKS}M" for b in buckets]
    width = max(len(p) for p in PROCESSORS)
    lines = [f"{'processor':<{width}}  {'total s':>9}  {'share':>6}  " + "  ".join(f"{h:>9}" for h in heads)]
    for p in PROCESSORS:
        entry = report["processors"][p]
        lines.append(f"{p:<{width}}  {entry['seconds']:>9.2f}  {entry['share']:>6.1%}  "
                     + "  ".join(f"{b['seconds'][p]:>9.2f}" for b in buckets))
    lines.append(f"{'all processors':<{width}}  {report['processing_seconds']:>9.2f}  {'':>6}  "
                 + "  ".join(f"{b['total_seconds']:>9.2f}" for b in buckets))
    lines.append(f"sync wall {report['sync_wall_seconds']:.1f}s over {report['batches']} batches"
                 f" (blocks {report['first_block']}..{report['last_block']}),"
                 f" instrumentation {report['instrumentation_seconds']:.1f}s")
    if report["uninstrumented_sync_wall_seconds"] is not None:
        lines.append(f"uninstrumented sync wall {report['uninstrumented_sync_wall_seconds']:.1f}s")
    ids = report["reblog_ids"]
    lines.append(f"reblogs: id='reblog' {ids['custom_json_id_reblog']},"
                 f" id='follow' with [\"reblog\", ...] {ids['custom_json_id_follow_payload_reblog']}."
                 f" {ids['finding']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--log", required=True, type=Path, help="haf_app_driver.py's output")
    parser.add_argument("--out", required=True, type=Path, help="where the JSON report goes")
    parser.add_argument("--first-block", required=True, type=int)
    parser.add_argument("--last-block", required=True, type=int)
    parser.add_argument("--sync-seconds", required=True, type=float, help="the timed run's wall time")
    parser.add_argument("--uninstrumented-sync-seconds", type=float,
                        help="the same range's wall time with the timings off")
    args = parser.parse_args(argv)
    try:
        with args.log.open(encoding="utf-8", errors="replace") as log:
            timings, counts = parse_log(log)
        report = build_report(timings, counts, first_block=args.first_block, last_block=args.last_block,
                              sync_seconds=args.sync_seconds,
                              uninstrumented_sync_seconds=args.uninstrumented_sync_seconds)
    except (OSError, TimingsError) as exc:
        print(f"no report: {exc}", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(summary_table(report))
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
