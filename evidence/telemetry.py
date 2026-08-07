"""Read the telemetry the services emit and evaluate the deployed alarms on it.

Alarm thresholds live in `deploy/aws/alarms.json` and are provisioned from that
same file. Evaluating them here proves the metric an alarm names is one the
system produces under the condition the alarm describes. It does not exercise
CloudWatch's own evaluation engine, which needs a deployed environment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ALARMS = Path("deploy/aws/alarms.json")

COMPARISONS = {
    "GreaterThanThreshold": lambda value, threshold: value > threshold,
    "GreaterThanOrEqualToThreshold": lambda value, threshold: value >= threshold,
    "LessThanThreshold": lambda value, threshold: value < threshold,
    "LessThanOrEqualToThreshold": lambda value, threshold: value <= threshold,
}


def read(paths: list[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def spans(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [record for record in records if record.get("telemetry") == "span"]


@dataclass
class Sample:
    metric: str
    namespace: str
    dimensions: dict[str, str]
    value: Any


def samples(records: list[dict[str, Any]]) -> list[Sample]:
    collected: list[Sample] = []
    for record in records:
        metadata = record.get("_aws")
        if not isinstance(metadata, dict):
            continue
        for directive in metadata.get("CloudWatchMetrics", []):
            names = {name for group in directive.get("Dimensions", []) for name in group}
            dimensions = {name: str(record[name]) for name in names if name in record}
            for definition in directive.get("Metrics", []):
                metric = definition["Name"]
                if metric in record:
                    collected.append(Sample(metric, directive["Namespace"], dimensions, record[metric]))
    return collected


def _statistic(name: str, values: list[float], counts: list[float]) -> float:
    match name:
        case "Sum":
            return sum(values)
        case "Maximum":
            return max(values)
        case "Minimum":
            return min(values)
        case "SampleCount":
            return sum(counts)
        case "Average":
            total = sum(counts)
            return sum(values) / total if total else 0.0
    raise ValueError(f"unsupported statistic {name}")


def _flatten(sample: Sample, statistic: str) -> tuple[float, float]:
    """Return (value, count) contributions of one sample to a statistic."""
    value = sample.value
    if isinstance(value, dict):
        count = float(value.get("Count", 0))
        if statistic == "Sum":
            return float(value.get("Sum", 0.0)), count
        if statistic == "Average":
            return float(value.get("Sum", 0.0)), count
        if statistic == "Maximum":
            return float(value.get("Max", value.get("Sum", 0.0))), count
        if statistic == "Minimum":
            return float(value.get("Min", value.get("Sum", 0.0))), count
        return count, count
    return float(value), 1.0


def load_alarms(path: Path = ALARMS) -> list[dict[str, Any]]:
    return list(json.loads(path.read_text(encoding="utf-8"))["alarms"])


def evaluate(alarm: dict[str, Any], collected: list[Sample], namespaces: dict[str, str]) -> dict[str, Any]:
    """Decide whether the samples this run produced breach one alarm.

    An alarm on a metric the platform publishes — queue depth, task count,
    database storage — has no local equivalent. Reporting it as breaching
    because nothing produced data would be a false claim, so it is reported as
    what it is: not observable without a deployment.
    """
    if str(alarm["namespace"]) not in namespaces:
        return {
            "alarm": alarm["name"],
            "state": "NOT_OBSERVABLE_LOCALLY",
            "namespace": alarm["namespace"],
            "observed": None,
            "threshold": alarm["threshold"],
            "samples": 0,
            "responded": False,
            "tested_condition": alarm.get("tested_condition"),
        }
    namespace = namespaces[str(alarm["namespace"])]
    matching = [
        sample
        for sample in collected
        if sample.metric == alarm["metric_name"]
        and sample.namespace == namespace
        and all(sample.dimensions.get(key) == value for key, value in alarm["dimensions"].items())
    ]
    if not matching:
        return {
            "alarm": alarm["name"],
            "state": "ALARM" if alarm["treat_missing_data"] == "breaching" else "INSUFFICIENT_DATA",
            "observed": None,
            "threshold": alarm["threshold"],
            "samples": 0,
            "responded": False,
            "tested_condition": alarm.get("tested_condition"),
        }
    contributions = [_flatten(sample, alarm["statistic"]) for sample in matching]
    observed = _statistic(alarm["statistic"], [value for value, _ in contributions], [count for _, count in contributions])
    breached = COMPARISONS[str(alarm["comparison_operator"])](observed, float(alarm["threshold"]))
    return {
        "alarm": alarm["name"],
        "state": "ALARM" if breached else "OK",
        "observed": observed,
        "threshold": alarm["threshold"],
        "statistic": alarm["statistic"],
        "samples": len(matching),
        # A rate alarm can stay below its paging threshold while still proving
        # the metric moved under the condition it watches.
        "responded": observed != 0,
        "tested_condition": alarm.get("tested_condition"),
    }


def percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)

    def at(fraction: float) -> float:
        index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
        return round(ordered[index], 3)

    return {
        "count": len(ordered),
        "min_ms": round(ordered[0], 3),
        "p50_ms": at(0.5),
        "p95_ms": at(0.95),
        "max_ms": round(ordered[-1], 3),
        "mean_ms": round(sum(ordered) / len(ordered), 3),
    }
