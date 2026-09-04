import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from monitor import MonitorError, build_alert, parse_meter_page


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
        "warning_threshold_kwh": 30,
        "critical_threshold_kwh": 15,
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
        self.assertIsNone(reading.yesterday_kwh)
        self.assertEqual(reading.price_cny_per_kwh, 0.48)
        self.assertEqual(reading.level, "ok")
        self.assertEqual(reading.recent_purchases[0]["quantity_kwh"], "100")

    def test_critical_level_is_independent_per_meter(self):
        reading = parse_meter_page(
            PAGE.format(meter_id="44229", kind="[空调]", remaining="0"),
            {
                **meter(kind="[空调]", meter_id="44229"),
                "key": "air_conditioner",
                "name": "空调电表",
                "warning_threshold_kwh": 10,
                "critical_threshold_kwh": 5,
            },
            max_stale_hours=36,
            now=NOW,
        )
        self.assertEqual(reading.level, "critical")
        alert = build_alert([reading], [])
        self.assertIn("空调电表", alert)
        self.assertIn("剩余 0 kWh", alert)

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


if __name__ == "__main__":
    unittest.main()
