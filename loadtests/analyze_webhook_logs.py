"""Azure Container Apps webhook metric logs analyzer."""
import re
import sys
from collections import defaultdict
from pathlib import Path

METRIC_PATTERN = re.compile(r"metric=(?P<metric>\S+)")
VALUE_PATTERN = re.compile(r"(?P<key>duration_ms|rate_limit_ms|validation_ms)=(?P<value>[0-9.]+)")


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile_value / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def read_lines(path: str | None):
    if path:
        return Path(path).read_text(encoding="utf-8").splitlines()
    return sys.stdin


def analyze(lines) -> dict[str, list[float]]:
    measurements: dict[str, list[float]] = defaultdict(list)
    for line in lines:
        metric_match = METRIC_PATTERN.search(line)
        if not metric_match:
            continue
        metric = metric_match.group("metric")
        for value_match in VALUE_PATTERN.finditer(line):
            key = value_match.group("key")
            measurements[f"{metric}.{key}"].append(float(value_match.group("value")))
    return measurements


def print_report(measurements: dict[str, list[float]]) -> None:
    if not measurements:
        print("計測ログが見つかりませんでした。metric=webhook を含むログを指定してください。")
        return

    print("metric                         count     min       p50       p95       p99       max       avg")
    print("-" * 105)
    for name in sorted(measurements):
        values = measurements[name]
        print(
            f"{name:<30} {len(values):>5} "
            f"{min(values):>9.2f} {percentile(values, 50):>9.2f} "
            f"{percentile(values, 95):>9.2f} {percentile(values, 99):>9.2f} "
            f"{max(values):>9.2f} {sum(values) / len(values):>9.2f}"
        )

    request_values = measurements.get("webhook_request.duration_ms", [])
    background_values = measurements.get("webhook_background.duration_ms", [])
    if request_values and background_values:
        request_p95 = percentile(request_values, 95)
        background_p95 = percentile(background_values, 95)
        print()
        print("判定:")
        if background_p95 > request_p95 * 2:
            print("- ボトルネック候補: バックグラウンド処理（DBまたはLINE API）")
        else:
            print("- Webhook受付とバックグラウンド処理の差は小さめです。DB/外部APIの内訳を追加計測してください。")


if __name__ == "__main__":
    input_path = sys.argv[1] if len(sys.argv) > 1 else None
    print_report(analyze(read_lines(input_path)))
