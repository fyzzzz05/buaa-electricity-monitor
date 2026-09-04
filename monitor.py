#!/usr/bin/env python3
"""BUAA dorm electricity monitor for local runs and GitHub Actions.

The school endpoint returns a server-rendered HTML page.  This script extracts
the two meter widgets, validates that each meter still points at the expected
address, writes a machine-readable result, and optionally sends a Telegram
alert.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


USER_AGENT = "buaa-dorm-electricity-monitor/1.0"
NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


class MonitorError(RuntimeError):
    """A recoverable monitoring/configuration error."""


class MeterPageParser(HTMLParser):
    """Extract the useful fields from the server-rendered meter page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []
        self.widgets: dict[str, str] = {}
        self.table_rows: list[list[str]] = []

        self._paragraph: list[str] | None = None
        self._svg_stack: list[str | None] = []
        self._current_svg: str | None = None
        self._widget_text: list[str] | None = None
        self._widget_target: str | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if tag == "p":
            self._paragraph = []
        elif tag == "svg":
            self._svg_stack.append(self._current_svg)
            self._current_svg = attributes.get("id") or self._current_svg
        elif tag == "tspan" and self._current_svg in {"canvas1", "canvas2"}:
            self._widget_target = self._current_svg
            self._widget_text = []
        elif tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._paragraph is not None:
            self._paragraph.append(data)
        if self._widget_text is not None:
            self._widget_text.append(data)
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self._paragraph is not None:
            text = normalize_text("".join(self._paragraph).replace("哈哈", ""))
            if text:
                self.paragraphs.append(text)
            self._paragraph = None
        elif tag == "tspan" and self._widget_text is not None:
            text = normalize_text("".join(self._widget_text))
            if self._widget_target and text:
                self.widgets[self._widget_target] = text
            self._widget_text = None
            self._widget_target = None
        elif tag == "svg":
            self._current_svg = self._svg_stack.pop() if self._svg_stack else None
        elif tag in {"td", "th"} and self._cell is not None:
            if self._row is not None:
                self._row.append(normalize_text("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.table_rows.append(self._row)
            self._row = None


def normalize_text(value: str) -> str:
    return " ".join(value.split())


def text_after_prefix(lines: list[str], prefix: str) -> str | None:
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return None


def parse_number(value: str, field_name: str) -> float:
    match = NUMBER_RE.search(value.replace(",", ""))
    if not match:
        raise MonitorError(f"无法从 {field_name} 解析数值：{value!r}")
    return float(match.group(0))


def parse_optional_number(value: str | None) -> float | None:
    if not value or "未知" in value:
        return None
    match = NUMBER_RE.search(value.replace(",", ""))
    return float(match.group(0)) if match else None


@dataclass(frozen=True)
class Reading:
    key: str
    name: str
    meter_id: str
    address: str
    price_cny_per_kwh: float | None
    remaining_kwh: float
    yesterday_kwh: float | None
    cutoff: str
    source_age_hours: float | None
    checked_at: str
    level: str
    warning_threshold_kwh: float
    critical_threshold_kwh: float
    recent_purchases: list[dict[str, str]]


def parse_meter_page(
    html: str,
    meter: dict[str, Any],
    *,
    max_stale_hours: float,
    now: datetime | None = None,
) -> Reading:
    parser = MeterPageParser()
    parser.feed(html)

    meter_id = text_after_prefix(parser.paragraphs, "购电表号:")
    address = text_after_prefix(parser.paragraphs, "地址:")
    price_text = text_after_prefix(parser.paragraphs, "电价:")
    cutoff_line = next(
        (line for line in parser.paragraphs if "截止" in line), None
    )
    remaining_text = parser.widgets.get("canvas1")

    if meter_id != str(meter["meter_id"]):
        raise MonitorError(
            f"电表号校验失败：期望 {meter['meter_id']}，页面返回 {meter_id!r}"
        )
    if not address:
        raise MonitorError("页面中没有找到电表地址")
    marker = str(meter["expected_address_contains"])
    if marker not in address:
        raise MonitorError(
            f"地址校验失败：期望包含 {marker!r}，页面返回 {address!r}"
        )
    if not cutoff_line:
        raise MonitorError("页面中没有找到数据截止时间")
    if remaining_text is None:
        raise MonitorError("页面中没有找到剩余电量组件 #canvas1")

    cutoff = cutoff_line.replace("截止", "", 1).strip("[] ")
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    source_age_hours: float | None = None
    try:
        cutoff_dt = datetime.strptime(cutoff, "%Y/%m/%d %H:%M:%S").replace(
            tzinfo=ZoneInfo("Asia/Shanghai")
        )
        source_age_hours = max(0.0, (now - cutoff_dt).total_seconds() / 3600)
    except ValueError:
        # Keep monitoring when the university changes only the date formatting.
        pass

    if source_age_hours is not None and source_age_hours > max_stale_hours:
        raise MonitorError(
            f"学校数据已超过 {max_stale_hours:g} 小时未更新（截止 {cutoff}）"
        )

    remaining = parse_number(remaining_text, "剩余电量")
    warning = float(meter["warning_threshold_kwh"])
    critical = float(meter["critical_threshold_kwh"])
    if remaining <= critical:
        level = "critical"
    elif remaining <= warning:
        level = "warning"
    else:
        level = "ok"

    purchases: list[dict[str, str]] = []
    for row in parser.table_rows:
        if len(row) == 4 and row[0] != "日期" and "年" in row[0]:
            purchases.append(
                {
                    "date": row[0],
                    "quantity_kwh": row[1],
                    "amount_cny": row[2],
                    "operator": row[3],
                }
            )

    return Reading(
        key=str(meter["key"]),
        name=str(meter["name"]),
        meter_id=str(meter["meter_id"]),
        address=address,
        price_cny_per_kwh=(
            parse_optional_number(price_text) if price_text is not None else None
        ),
        remaining_kwh=remaining,
        yesterday_kwh=parse_optional_number(parser.widgets.get("canvas2")),
        cutoff=cutoff,
        source_age_hours=(
            round(source_age_hours, 2) if source_age_hours is not None else None
        ),
        checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        level=level,
        warning_threshold_kwh=warning,
        critical_threshold_kwh=critical,
        recent_purchases=purchases,
    )


def fetch_text(url: str, *, timeout: float, retries: int) -> str:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        request = Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise MonitorError(f"HTTP {status}")
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except (HTTPError, URLError, TimeoutError, MonitorError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(2**attempt, 4))
    raise MonitorError(f"请求 {url} 失败：{last_error}")


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MonitorError(f"无法读取配置文件 {path}：{exc}") from exc

    meters = config.get("meters")
    if not isinstance(meters, list) or not meters:
        raise MonitorError("config.json 中必须提供非空 meters 列表")
    required = {
        "key",
        "name",
        "meter_id",
        "expected_address_contains",
        "warning_threshold_kwh",
        "critical_threshold_kwh",
    }
    keys: set[str] = set()
    for meter in meters:
        missing = required.difference(meter)
        if missing:
            raise MonitorError(f"电表配置缺少字段：{', '.join(sorted(missing))}")
        if meter["key"] in keys:
            raise MonitorError(f"电表 key 重复：{meter['key']}")
        keys.add(str(meter["key"]))
        if float(meter["critical_threshold_kwh"]) > float(
            meter["warning_threshold_kwh"]
        ):
            raise MonitorError(f"{meter['name']} 的严重阈值不能高于预警阈值")
    return config


def query_all(config: dict[str, Any]) -> tuple[list[Reading], list[str]]:
    request_config = config.get("request", {})
    template = str(
        request_config.get(
            "url_template", "https://shsd.buaa.edu.cn/PubBuaa?id={meter_id}"
        )
    )
    timeout = float(request_config.get("timeout_seconds", 20))
    retries = int(request_config.get("retries", 2))
    max_stale = float(request_config.get("max_stale_hours", 36))

    readings: list[Reading] = []
    errors: list[str] = []
    for meter in config["meters"]:
        try:
            url = template.format(meter_id=meter["meter_id"])
            page = fetch_text(url, timeout=timeout, retries=retries)
            readings.append(
                parse_meter_page(page, meter, max_stale_hours=max_stale)
            )
        except (KeyError, ValueError, MonitorError) as exc:
            errors.append(f"{meter.get('name', meter.get('key', '未知电表'))}: {exc}")
    return readings, errors


def telegram_send(message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise MonitorError(
            "缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID；请配置 GitHub Actions Secrets"
        )

    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urlencode(
        {"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}
    ).encode("utf-8")
    request = Request(
        endpoint,
        data=payload,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise MonitorError(f"Telegram 消息发送失败：{exc}") from exc
    if not result.get("ok"):
        raise MonitorError(f"Telegram API 返回失败：{result.get('description', result)}")


def format_number(value: float | None) -> str:
    if value is None:
        return "未知"
    return f"{value:g}"


def build_alert(readings: list[Reading], errors: list[str]) -> str | None:
    abnormal = [reading for reading in readings if reading.level != "ok"]
    if not abnormal and not errors:
        return None

    lines = ["⚡ 北航宿舍电量告警"]
    for reading in abnormal:
        icon = "🚨" if reading.level == "critical" else "⚠️"
        threshold = (
            reading.critical_threshold_kwh
            if reading.level == "critical"
            else reading.warning_threshold_kwh
        )
        lines.append(
            f"{icon} {reading.name}：剩余 {format_number(reading.remaining_kwh)} kWh"
            f"（阈值 {format_number(threshold)} kWh）"
        )
        lines.append(f"数据截止：{reading.cutoff}")
    for error in errors:
        lines.append(f"❌ 查询异常：{error}")
    lines.append("请登录学校购电页面复核；网站数据可能与实际值存在偏差。")
    return "\n".join(lines)


def build_markdown(readings: list[Reading], errors: list[str]) -> str:
    labels = {"ok": "正常", "warning": "预警", "critical": "严重"}
    lines = [
        "# 宿舍电费监控结果",
        "",
        "| 电表 | 表号 | 类型 | 剩余电量 | 昨日用电 | 数据截止 |",
        "|---|---:|---|---:|---:|---|",
    ]
    for reading in readings:
        lines.append(
            "| {name} | {meter_id} | {level} | {remaining} kWh | {yesterday} | {cutoff} |".format(
                name=reading.name,
                meter_id=reading.meter_id,
                level=labels[reading.level],
                remaining=format_number(reading.remaining_kwh),
                yesterday=(
                    f"{format_number(reading.yesterday_kwh)} kWh"
                    if reading.yesterday_kwh is not None
                    else "未知"
                ),
                cutoff=reading.cutoff,
            )
        )
    if errors:
        lines.extend(["", "## 查询异常", ""])
        lines.extend(f"- {error}" for error in errors)
    lines.extend(
        [
            "",
            "> 学校页面注明：以上数据仅供参考，可能与实际数据存在偏差。",
        ]
    )
    return "\n".join(lines) + "\n"


def write_outputs(
    output_path: Path, readings: list[Reading], errors: list[str]
) -> None:
    result = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "readings": [asdict(reading) for reading in readings],
        "errors": errors,
    }
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    markdown = build_markdown(readings, errors)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write(markdown)
    print(markdown)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="北航宿舍电费监控")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("config.json"),
        help="配置文件路径（默认使用脚本旁的 config.json）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("monitor-result.json"),
        help="机器可读结果的输出路径",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="只查询，不发送 Telegram 消息",
    )
    parser.add_argument(
        "--test-notification",
        action="store_true",
        help="先发送一条 Telegram 测试消息，再执行查询",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        if args.test_notification and not args.no_notify:
            telegram_send("✅ 北航宿舍电费监控：Telegram 通知测试成功。")

        readings, errors = query_all(config)
        write_outputs(args.output, readings, errors)

        alert = build_alert(readings, errors)
        if alert and not args.no_notify:
            telegram_send(alert)
        elif not alert:
            print("两个电表均高于各自预警阈值，无需发送告警。")

        return 1 if errors else 0
    except MonitorError as exc:
        print(f"监控失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
