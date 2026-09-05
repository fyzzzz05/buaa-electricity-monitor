#!/usr/bin/env python3
"""BUAA dorm electricity monitor for local runs and GitHub Actions.

The school endpoint returns a server-rendered HTML page.  This script extracts
the two meter widgets, validates that each meter still points at the expected
address, writes a machine-readable result, and optionally sends a Telegram
alert.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
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
    remaining_cny: float | None
    yesterday_kwh: float | None
    cutoff: str
    source_age_hours: float | None
    checked_at: str
    level: str
    alert_threshold_cny: float
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
    price = parse_optional_number(price_text)
    remaining_cny = round(remaining * price, 2) if price is not None else None
    alert_threshold_cny = float(meter["alert_threshold_cny"])
    level = (
        "alert"
        if remaining_cny is not None and remaining_cny <= alert_threshold_cny
        else "ok"
    )

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
        price_cny_per_kwh=price,
        remaining_kwh=remaining,
        remaining_cny=remaining_cny,
        yesterday_kwh=parse_optional_number(parser.widgets.get("canvas2")),
        cutoff=cutoff,
        source_age_hours=(
            round(source_age_hours, 2) if source_age_hours is not None else None
        ),
        checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        level=level,
        alert_threshold_cny=alert_threshold_cny,
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
        "alert_threshold_cny",
    }
    keys: set[str] = set()
    for meter in meters:
        missing = required.difference(meter)
        if missing:
            raise MonitorError(f"电表配置缺少字段：{', '.join(sorted(missing))}")
        if meter["key"] in keys:
            raise MonitorError(f"电表 key 重复：{meter['key']}")
        keys.add(str(meter["key"]))
        if float(meter["alert_threshold_cny"]) < 0:
            raise MonitorError(f"{meter['name']} 的金额阈值不能小于 0")
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


HISTORY_FIELDS = [
    "date",
    "checked_at",
    "cutoff",
    "key",
    "name",
    "meter_id",
    "remaining_kwh",
    "price_cny_per_kwh",
    "remaining_cny",
    "level",
]


def reading_date(reading: Reading) -> str:
    try:
        return datetime.strptime(
            reading.cutoff.split()[0], "%Y/%m/%d"
        ).date().isoformat()
    except ValueError:
        return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def read_history(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open(newline="", encoding="utf-8") as history_file:
            return list(csv.DictReader(history_file))
    except OSError as exc:
        raise MonitorError(f"无法读取历史文件 {path}：{exc}") from exc


def update_history(path: Path, readings: list[Reading]) -> list[dict[str, str]]:
    """Upsert one row per meter and source date, then return all rows."""
    rows = read_history(path)
    by_identity = {
        (row.get("date", ""), row.get("key", "")): row
        for row in rows
        if row.get("date") and row.get("key")
    }
    for reading in readings:
        row = {
            "date": reading_date(reading),
            "checked_at": reading.checked_at,
            "cutoff": reading.cutoff,
            "key": reading.key,
            "name": reading.name,
            "meter_id": reading.meter_id,
            "remaining_kwh": format_number(reading.remaining_kwh),
            "price_cny_per_kwh": format_number(reading.price_cny_per_kwh),
            "remaining_cny": format_number(reading.remaining_cny),
            "level": reading.level,
        }
        by_identity[(row["date"], row["key"])] = row

    rows = sorted(
        by_identity.values(), key=lambda row: (row["date"], row["key"])
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", newline="", encoding="utf-8") as history_file:
            writer = csv.DictWriter(history_file, fieldnames=HISTORY_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    except OSError as exc:
        raise MonitorError(f"无法写入历史文件 {path}：{exc}") from exc
    return rows


def notification_date(now: datetime | None = None) -> str:
    """Return the current calendar date used for daily notification deduping."""
    current = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return current.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()


def notification_already_sent(
    path: Path, *, now: datetime | None = None
) -> bool:
    """Return whether a Telegram report succeeded today in China time."""
    if not path.exists():
        return False
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # Prefer a possible duplicate over silently missing the whole day's report.
        print(f"通知状态文件不可用，将继续发送：{exc}", file=sys.stderr)
        return False
    return state.get("last_successful_date") == notification_date(now)


def mark_notification_sent(
    path: Path, *, now: datetime | None = None
) -> None:
    """Persist a successful Telegram report only after the API accepted it."""
    current = now or datetime.now(timezone.utc)
    state = {
        "last_successful_date": notification_date(current),
        "sent_at": current.astimezone(timezone.utc).isoformat(timespec="seconds"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(path)
    except OSError as exc:
        raise MonitorError(f"无法保存通知状态 {path}：{exc}") from exc


def generate_chart(
    rows: list[dict[str, str]],
    readings: list[Reading],
    output_path: Path,
    *,
    days: int = 30,
) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise MonitorError("生成折线图需要安装 requirements.txt 中的 matplotlib") from exc

    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    earliest = today - timedelta(days=days - 1)
    chart_names = {
        "air_conditioner": "Air conditioner",
        "lighting": "Lighting",
    }
    figure, axes = plt.subplots(
        max(1, len(readings)), 1, figsize=(9, 3.3 * max(1, len(readings)))
    )
    if len(readings) == 1:
        axes = [axes]

    for axis, reading in zip(axes, readings):
        points: list[tuple[date, float]] = []
        for row in rows:
            if row.get("key") != reading.key:
                continue
            try:
                day = date.fromisoformat(row["date"])
                value = float(row["remaining_kwh"])
            except (KeyError, ValueError):
                continue
            if day >= earliest:
                points.append((day, value))
        points.sort(key=lambda point: point[0])

        if points:
            axis.plot(
                [point[0] for point in points],
                [point[1] for point in points],
                color="#2563eb",
                marker="o",
                linewidth=2.2,
                markersize=5,
                label="Remaining",
            )
            latest_day, latest_value = points[-1]
            axis.annotate(
                f"{latest_value:g} kWh",
                (latest_day, latest_value),
                xytext=(6, 8),
                textcoords="offset points",
                fontsize=9,
            )

        if reading.price_cny_per_kwh:
            threshold_kwh = (
                reading.alert_threshold_cny / reading.price_cny_per_kwh
            )
            axis.axhline(
                threshold_kwh,
                color="#dc2626",
                linestyle="--",
                linewidth=1.6,
                label=f"CNY {reading.alert_threshold_cny:g} alert line",
            )

        axis.set_title(chart_names.get(reading.key, reading.key))
        axis.set_ylabel("Remaining (kWh)")
        axis.grid(True, alpha=0.25)
        axis.legend(loc="best", fontsize=8)
        axis.set_xlim(earliest, today + timedelta(days=1))
        axis.margins(y=0.15)
        axis.xaxis.set_major_locator(mdates.DayLocator(interval=5))
        axis.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))

    axes[-1].set_xlabel("Date (Asia/Shanghai)")
    figure.suptitle(f"Dorm electricity - last {days} days", fontsize=14)
    figure.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output_path, dpi=160, bbox_inches="tight")
    plt.close(figure)


def telegram_credentials() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise MonitorError(
            "缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID；请配置 GitHub Actions Secrets"
        )
    return token, chat_id


def telegram_error(exc: HTTPError) -> MonitorError:
    try:
        body = exc.read().decode("utf-8", errors="replace")
        details = json.loads(body).get("description", body)
    except (OSError, json.JSONDecodeError):
        details = str(exc)
    return MonitorError(f"Telegram API HTTP {exc.code}：{details}")


def telegram_send(message: str) -> None:
    token, chat_id = telegram_credentials()

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
    except HTTPError as exc:
        raise telegram_error(exc) from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise MonitorError(f"Telegram 消息发送失败：{exc}") from exc
    if not result.get("ok"):
        raise MonitorError(f"Telegram API 返回失败：{result.get('description', result)}")


def telegram_send_photo(image_path: Path, caption: str) -> None:
    token, chat_id = telegram_credentials()
    boundary = f"----electricity-monitor-{uuid.uuid4().hex}"
    body = bytearray()

    def add_field(name: str, value: str) -> None:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        body.extend(value.encode("utf-8"))
        body.extend(b"\r\n")

    add_field("chat_id", chat_id)
    add_field("caption", caption)
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        b'Content-Disposition: form-data; name="photo"; filename="electricity-history.png"\r\n'
    )
    body.extend(b"Content-Type: image/png\r\n\r\n")
    try:
        body.extend(image_path.read_bytes())
    except OSError as exc:
        raise MonitorError(f"无法读取折线图 {image_path}：{exc}") from exc
    body.extend(f"\r\n--{boundary}--\r\n".encode())

    request = Request(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data=bytes(body),
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise telegram_error(exc) from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise MonitorError(f"Telegram 图片发送失败：{exc}") from exc
    if not result.get("ok"):
        raise MonitorError(f"Telegram API 返回失败：{result.get('description', result)}")


def format_number(value: float | None) -> str:
    if value is None:
        return "未知"
    return f"{value:g}"


def build_daily_report(readings: list[Reading], errors: list[str]) -> str:
    has_alert = any(reading.level == "alert" for reading in readings) or bool(errors)
    lines = ["🚨 北航宿舍电量告警" if has_alert else "⚡ 北航宿舍电量日报"]
    for reading in readings:
        icon = "🚨" if reading.level == "alert" else "✅"
        lines.append(
            f"{icon} {reading.name}：剩余 {format_number(reading.remaining_kwh)} kWh"
            f" ≈ ¥{format_number(reading.remaining_cny)}"
        )
        lines.append(f"数据截止：{reading.cutoff}")
    for error in errors:
        lines.append(f"❌ 查询异常：{error}")
    lines.append("告警线：剩余价值 ≤ ¥10")
    lines.append("学校数据仅供参考，低电量时请登录购电页面复核。")
    return "\n".join(lines)


def build_markdown(readings: list[Reading], errors: list[str]) -> str:
    labels = {"ok": "正常", "alert": "告警"}
    lines = [
        "# 宿舍电费监控结果",
        "",
        "| 电表 | 表号 | 状态 | 剩余电量 | 折算金额 | 昨日用电 | 数据截止 |",
        "|---|---:|---|---:|---:|---:|---|",
    ]
    for reading in readings:
        lines.append(
            "| {name} | {meter_id} | {level} | {remaining} kWh | ¥{money} | {yesterday} | {cutoff} |".format(
                name=reading.name,
                meter_id=reading.meter_id,
                level=labels[reading.level],
                remaining=format_number(reading.remaining_kwh),
                money=format_number(reading.remaining_cny),
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
        "--history",
        type=Path,
        default=Path("data/history.csv"),
        help="跨运行保存的历史 CSV",
    )
    parser.add_argument(
        "--chart",
        type=Path,
        default=Path("electricity-history.png"),
        help="发送到 Telegram 的折线图路径",
    )
    parser.add_argument(
        "--notification-state",
        type=Path,
        default=Path("data/notification-state.json"),
        help="每日成功通知状态文件",
    )
    parser.add_argument(
        "--deduplicate-notification",
        action="store_true",
        help="今天已经成功发送时跳过本次运行",
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
        if args.deduplicate_notification and notification_already_sent(
            args.notification_state
        ):
            print("今天的 Telegram 日报已经成功发送，跳过本次补跑。")
            return 0
        if args.test_notification and not args.no_notify:
            telegram_send("✅ 北航宿舍电费监控：Telegram 通知测试成功。")

        readings, errors = query_all(config)
        write_outputs(args.output, readings, errors)
        history_rows = update_history(args.history, readings)

        chart_created = False
        if readings:
            generate_chart(history_rows, readings, args.chart)
            chart_created = True

        report = build_daily_report(readings, errors)
        if not args.no_notify:
            if chart_created:
                telegram_send_photo(args.chart, report)
            else:
                telegram_send(report)
            mark_notification_sent(args.notification_state)

        return 1 if errors else 0
    except MonitorError as exc:
        print(f"监控失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
