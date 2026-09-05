import json
import os
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from monitor import (
    MonitorError,
    build_daily_report,
    generate_chart,
    main,
    mark_notification_sent,
    notification_already_sent,
    parse_meter_page,
    telegram_send_photo,
    update_history,
)


PAGE = """
<!doctype html>
<html><body>
  <div class="box-shadow">
    <p>4公寓南楼-3-314</p>
    <p>购电表号: {meter_id}</p>
    <p>电<i style="color: transparent">哈哈</i>价: 0.4800</p>
    <p>地<i style="color: transparent">哈哈</i>址: 4公寓南楼-3-314{kind}</p>
  </div>
  <p>[截止 2026/9/4 0:00:00]</p>
  <svg id="canvas1"><text><tspan>{remaining}</tspan></text></svg>
  <svg id="canvas2"><text><tspan>未知</tspan></text></svg>
  <table>
    <tr><th>日期</th><th>电量</th><th>金额</th><th>收费员</th></tr>
    <tr><td>2026年08月29日 23:10:13</td><td>100</td><td>¥ 48.00</td><td>校园支付平台</td></tr>
  </table>
</body></html>
"""


def meter(kind="[照明]", meter_id="44588"):
    return {
        "key": "lighting",
        "name": "照明电表",
        "meter_id": meter_id,
        "expected_address_contains": kind,
        "alert_threshold_cny": 10,
    }


NOW = datetime(2026, 9, 4, 8, 17, tzinfo=ZoneInfo("Asia/Shanghai"))


class ParserTests(unittest.TestCase):
    def test_parses_meter_page_and_purchase(self):
        reading = parse_meter_page(
            PAGE.format(meter_id="44588", kind="[照明]", remaining="91"),
            meter(),
            max_stale_hours=36,
            now=NOW,
        )
        self.assertEqual(reading.meter_id, "44588")
        self.assertEqual(reading.remaining_kwh, 91)
        self.assertEqual(reading.remaining_cny, 43.68)
        self.assertIsNone(reading.yesterday_kwh)
        self.assertEqual(reading.price_cny_per_kwh, 0.48)
        self.assertEqual(reading.level, "ok")
        self.assertEqual(reading.recent_purchases[0]["quantity_kwh"], "100")

    def test_money_alert_level_is_independent_per_meter(self):
        reading = parse_meter_page(
            PAGE.format(meter_id="44229", kind="[空调]", remaining="0"),
            {
                **meter(kind="[空调]", meter_id="44229"),
                "key": "air_conditioner",
                "name": "空调电表",
            },
            max_stale_hours=36,
            now=NOW,
        )
        self.assertEqual(reading.level, "alert")
        report = build_daily_report([reading], [])
        self.assertIn("空调电表", report)
        self.assertIn("剩余 0 kWh ≈ ¥0", report)

    def test_rejects_wrong_meter_address(self):
        with self.assertRaisesRegex(MonitorError, "地址校验失败"):
            parse_meter_page(
                PAGE.format(meter_id="44588", kind="[空调]", remaining="91"),
                meter(),
                max_stale_hours=36,
                now=NOW,
            )

    def test_rejects_stale_source_data(self):
        with self.assertRaisesRegex(MonitorError, "未更新"):
            parse_meter_page(
                PAGE.format(meter_id="44588", kind="[照明]", remaining="91"),
                meter(),
                max_stale_hours=3,
                now=NOW,
            )

    def test_history_upsert_and_chart(self):
        lighting = parse_meter_page(
            PAGE.format(meter_id="44588", kind="[照明]", remaining="91"),
            meter(),
            max_stale_hours=36,
            now=NOW,
        )
        air_conditioner = parse_meter_page(
            PAGE.format(meter_id="44229", kind="[空调]", remaining="0"),
            {
                **meter(kind="[空调]", meter_id="44229"),
                "key": "air_conditioner",
                "name": "空调电表",
            },
            max_stale_hours=36,
            now=NOW,
        )
        report = build_daily_report([air_conditioner, lighting], [])
        self.assertIn("空调电表", report)
        self.assertIn("照明电表", report)

        with TemporaryDirectory() as directory:
            history_path = Path(directory) / "history.csv"
            chart_path = Path(directory) / "chart.png"
            rows = update_history(history_path, [air_conditioner, lighting])
            rows = update_history(history_path, [air_conditioner, lighting])
            self.assertEqual(len(rows), 2)
            generate_chart(rows, [air_conditioner, lighting], chart_path)
            self.assertGreater(chart_path.stat().st_size, 1_000)

    def test_notification_state_deduplicates_by_china_date(self):
        first_run = datetime(
            2026, 9, 5, 0, 17, tzinfo=ZoneInfo("UTC")
        )
        same_china_day = datetime(
            2026, 9, 5, 1, 17, tzinfo=ZoneInfo("UTC")
        )
        next_china_day = datetime(
            2026, 9, 6, 0, 17, tzinfo=ZoneInfo("UTC")
        )

        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "notification-state.json"
            self.assertFalse(
                notification_already_sent(state_path, now=first_run)
            )
            mark_notification_sent(state_path, now=first_run)
            self.assertTrue(
                notification_already_sent(state_path, now=same_china_day)
            )
            self.assertFalse(
                notification_already_sent(state_path, now=next_china_day)
            )

    def test_scheduled_retry_skips_query_after_daily_success(self):
        with TemporaryDirectory() as directory:
            directory_path = Path(directory)
            state_path = directory_path / "notification-state.json"
            config_path = directory_path / "config.json"
            config_path.write_text(
                json.dumps({"meters": [meter()]}), encoding="utf-8"
            )
            mark_notification_sent(state_path)

            with patch("monitor.query_all") as mocked_query:
                result = main(
                    [
                        "--config",
                        str(config_path),
                        "--notification-state",
                        str(state_path),
                        "--deduplicate-notification",
                    ]
                )

            self.assertEqual(result, 0)
            mocked_query.assert_not_called()

    def test_telegram_photo_uses_multipart_without_exposing_secrets(self):
        response = MagicMock()
        response.read.return_value = json.dumps({"ok": True}).encode()
        context = MagicMock()
        context.__enter__.return_value = response
        context.__exit__.return_value = False

        with TemporaryDirectory() as directory:
            chart_path = Path(directory) / "chart.png"
            chart_path.write_bytes(b"fake-png-data")
            with patch.dict(
                os.environ,
                {
                    "TELEGRAM_BOT_TOKEN": "123:test-token",
                    "TELEGRAM_CHAT_ID": "456",
                },
                clear=False,
            ), patch("monitor.urlopen", return_value=context) as mocked_urlopen:
                telegram_send_photo(chart_path, "daily report")

        request = mocked_urlopen.call_args.args[0]
        self.assertIn(b'name="photo"', request.data)
        self.assertIn(b"fake-png-data", request.data)
        self.assertIn(b"daily report", request.data)
        self.assertNotIn(b"test-token", request.data)


if __name__ == "__main__":
    unittest.main()
