"""POST /api/analyze 冲突判定回归测试。

场景（航段均为 (0,300)→(1000,300)，10 km @ 60 km/h，08:00 起飞 → 08:10 到达，
f 与时刻线性对应：f=0.1 ↔ 1 分钟）：

1. 凹形禁飞区被走廊两次穿过、中间留有空隙 —— 时窗仅罩住空隙时不得误报；
2. 时窗罩住两段时各自独立成条，首个冲突为实际最早进入的一段；
3. 时窗只覆盖其中一段时仅报告该段；
4. 时窗截短区间时，enter/exit 参数、见证点、高度范围随截短后的最终区间重算；
5. 边界擦碰（瞬时相交）按实际重叠判定：落在窗内才报告。

运行：python -m unittest test_analyze.py -v
"""
import os
import tempfile
import unittest

os.environ.setdefault(
    "DB_PATH", os.path.join(tempfile.gettempdir(), "airspace_console_test.db")
)

from app import app  # noqa: E402

# ∩ 形（拱形）凹多边形：两腿 x∈[300,400] / x∈[600,700]，缺口 x∈(400,600)、深至 y=320。
# 走廊 y∈[290,310]（半宽 10）只命中两腿 → f∈[0.3,0.4] 与 [0.6,0.7]，空隙对应 08:04–08:06。
ARCH = [[300, 200], [400, 200], [400, 320], [600, 320],
        [600, 200], [700, 200], [700, 400], [300, 400]]

# 菱形顶点 (500,312.5) 恰好擦到走廊上沿（半宽 12.5）→ f=0.5 瞬时擦碰，时刻 08:05。
DIAMOND = [[500, 312.5], [460, 360], [500, 410], [540, 360]]


def make_state(vertices, alt0=200, alt1=200, half_width=0.1, **nfz_kw):
    nfz = {
        "id": "n1", "name": "凹形区", "vertices": vertices,
        "minAlt": 0, "maxAlt": 500, "startTime": "", "endTime": "", "enabled": True,
    }
    nfz.update(nfz_kw)
    return {
        "route": {
            "departureTime": "2026-09-24T08:00",
            "unitScale": 100,
            "corridorHalfWidth": half_width,
            "speeds": [60],
            "waypoints": [
                {"id": "w1", "name": "A", "x": 0, "y": 300, "alt": alt0},
                {"id": "w2", "name": "B", "x": 1000, "y": 300, "alt": alt1},
            ],
        },
        "nfzs": [nfz],
    }


class AnalyzeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app.test_client()

    def post(self, state):
        resp = self.client.post("/api/analyze", json=state)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["errors"], [])
        return body

    def test_gap_window_no_false_positive(self):
        """时窗 08:04:30–08:05:30 仅罩住两段之间的空隙 → 不得报告冲突。"""
        body = self.post(make_state(
            ARCH, startTime="2026-09-24T08:04:30", endTime="2026-09-24T08:05:30"))
        self.assertEqual(body["entries"], [])
        self.assertIsNone(body["first"])

    def test_two_pieces_reported_independently(self):
        """不限时窗 → 两段各自成条，字段对应同一段冲突，首个为最早进入段。"""
        body = self.post(make_state(ARCH))
        self.assertEqual(len(body["entries"]), 2)
        e0, e1 = body["entries"]
        self.assertEqual(e0, {
            "nfzId": "n1", "nfzName": "凹形区", "segIndex": 0,
            "enterFrac": 0.3, "exitFrac": 0.4,
            "enterTime": "2026-09-24T08:03", "exitTime": "2026-09-24T08:04",
            "durationMinutes": 1.0, "altLow": 200.0, "altHigh": 200.0,
            "witness": {"x": 300.0, "y": 300.0, "alt": 200.0}, "instant": False,
        })
        self.assertEqual((e1["enterFrac"], e1["exitFrac"]), (0.6, 0.7))
        self.assertEqual((e1["enterTime"], e1["exitTime"]),
                         ("2026-09-24T08:06", "2026-09-24T08:07"))
        self.assertEqual(e1["witness"], {"x": 600.0, "y": 300.0, "alt": 200.0})
        first = body["first"]
        self.assertEqual(first["enterTime"], "2026-09-24T08:03")
        self.assertEqual(first["witness"], {"x": 300.0, "y": 300.0, "alt": 200.0})

    def test_window_covering_one_piece_reports_only_that_piece(self):
        """时窗 08:05 起 → 第一段被排除，仅报告第二段且不被截短。"""
        body = self.post(make_state(ARCH, startTime="2026-09-24T08:05"))
        self.assertEqual(len(body["entries"]), 1)
        e = body["entries"][0]
        self.assertEqual((e["enterFrac"], e["exitFrac"]), (0.6, 0.7))
        self.assertEqual(e["enterTime"], "2026-09-24T08:06")
        self.assertEqual(body["first"]["enterTime"], "2026-09-24T08:06")

    def test_window_truncation_moves_witness(self):
        """时窗起点 08:03:30 截短第一段 → 见证点/参数/高度范围随进入时刻移动。"""
        body = self.post(make_state(ARCH, alt0=100, alt1=300,
                                    startTime="2026-09-24T08:03:30"))
        self.assertEqual(len(body["entries"]), 2)
        e0 = body["entries"][0]
        self.assertEqual(e0["enterFrac"], 0.35)          # 截短后进入参数
        self.assertEqual(e0["exitFrac"], 0.4)
        self.assertEqual(e0["durationMinutes"], 0.5)
        self.assertEqual(e0["witness"], {"x": 350.0, "y": 300.0, "alt": 170.0})
        self.assertEqual((e0["altLow"], e0["altHigh"]), (170.0, 180.0))
        # 首个冲突 = 截短后的第一段
        self.assertEqual(body["first"]["witness"]["x"], 350.0)
        # 第二段未被截短，高度范围对应该段自身
        e1 = body["entries"][1]
        self.assertEqual((e1["enterFrac"], e1["exitFrac"]), (0.6, 0.7))
        self.assertEqual((e1["altLow"], e1["altHigh"]), (220.0, 240.0))
        self.assertEqual(e1["witness"]["alt"], 220.0)

    def test_boundary_touch_instant_within_window(self):
        """边界擦碰（f 退化为点）时刻落在窗内 → 报告一条瞬时冲突。"""
        body = self.post(make_state(
            DIAMOND, half_width=0.125,
            startTime="2026-09-24T08:00", endTime="2026-09-24T08:10"))
        self.assertEqual(len(body["entries"]), 1)
        e = body["entries"][0]
        self.assertTrue(e["instant"])
        self.assertEqual(e["enterFrac"], e["exitFrac"])
        self.assertEqual(e["enterFrac"], 0.5)
        self.assertEqual(e["enterTime"], "2026-09-24T08:05")
        self.assertEqual(e["exitTime"], "2026-09-24T08:05")

    def test_boundary_touch_outside_window_ignored(self):
        """擦碰时刻 08:05 在时窗外 → 不报告。"""
        body = self.post(make_state(DIAMOND, half_width=0.125,
                                    startTime="2026-09-24T08:06"))
        self.assertEqual(body["entries"], [])
        body = self.post(make_state(DIAMOND, half_width=0.125,
                                    endTime="2026-09-24T08:04"))
        self.assertEqual(body["entries"], [])


if __name__ == "__main__":
    unittest.main()
