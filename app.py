"""低空航线空域时窗审查台 — Flask + SQLite 后端。

模型（画布局部平面坐标系，单位由用户约定，例如 km；高度单位 m；速度 km/h）：
- 航线由有序航点构成，航段折线按“走廊半宽”缓冲成扫掠走廊（平面 2D）。
- 每段速度独立，起飞时刻 + 航段距离/速度 推出各航点预达时刻（ETA）。
- 禁飞多边形带高度层 [minAlt, maxAlt] 与生效时窗 [startTime, endTime]（端点可空）。
- 冲突 = 平面走廊与多边形相交 ∩ 高度层重叠 ∩ 时间窗重叠（三维 + 时间）。
"""
import json
import math
import os
import sqlite3
from datetime import datetime

from flask import Flask, g, jsonify, request, send_from_directory
from shapely.geometry import LineString, Polygon
from shapely.validation import explain_validity

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "console.db"))

app = Flask(__name__, static_folder="static", static_url_path="/static")


# ---------------------------------------------------------------- 数据库
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute(PRAGMA_FOREIGN_KEYS)
    return g.db


PRAGMA_FOREIGN_KEYS = "PRAGMA foreign_keys=ON"


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """CREATE TABLE IF NOT EXISTS kv(
               k TEXT PRIMARY KEY,
               v TEXT NOT NULL
           )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS versions(
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               name TEXT NOT NULL,
               created_at TEXT NOT NULL,
               payload TEXT NOT NULL
           )"""
    )
    if db.execute("SELECT 1 FROM kv WHERE k='state'").fetchone() is None:
        db.execute(
            "INSERT INTO kv(k,v) VALUES('state',?)",
            (json.dumps(SEED_STATE, ensure_ascii=False),),
        )
    db.commit()
    db.close()


# ---------------------------------------------------------------- 初始样例
SEED_STATE = {
    "route": {
        "departureTime": "2026-09-24T08:00",
        "unitScale": 100,
        "corridorHalfWidth": 0.3,
        "speeds": [22, 26, 24],
        "waypoints": [
            {"id": "w1", "name": "WP-1", "x": 120, "y": 420, "alt": 200},
            {"id": "w2", "name": "WP-2", "x": 380, "y": 380, "alt": 200},
            {"id": "w3", "name": "WP-3", "x": 640, "y": 300, "alt": 350},
            {"id": "w4", "name": "WP-4", "x": 880, "y": 360, "alt": 250},
        ],
    },
    "nfzs": [
        {
            "id": "n1",
            "name": "临时禁飞区-A",
            "vertices": [[450, 280], [620, 280], [620, 350], [450, 350]],
            "minAlt": 150,
            "maxAlt": 300,
            "startTime": "2026-09-24T08:08",
            "endTime": "2026-09-24T08:13",
            "enabled": True,
        },
        {
            "id": "n2",
            "name": "高空限制区(500m+)",
            "vertices": [[700, 140], [830, 140], [830, 220], [700, 220]],
            "minAlt": 500,
            "maxAlt": 1200,
            "startTime": "",
            "endTime": "",
            "enabled": True,
        },
        {
            "id": "n3",
            "name": "夜间管控区",
            "vertices": [[760, 330], [920, 330], [920, 430], [760, 430]],
            "minAlt": 0,
            "maxAlt": 400,
            "startTime": "2026-09-24T20:00",
            "endTime": "2026-09-24T21:00",
            "enabled": True,
        },
    ],
}


# ---------------------------------------------------------------- 工具
def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.strip().replace(" ", "T"))
    except (ValueError, AttributeError):
        return None


def fmt_dt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M") if dt else None


def to_float(v, default=None):
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def interp(a, b, f):
    return a + (b - a) * f


def altitude_frac_ranges(a0, a1, lo, hi):
    """返回航段参数 f∈[0,1] 上线性高度落在 [lo,hi] 的子区间列表。"""
    eps = 1e-9
    if lo - eps <= a0 <= hi + eps and lo - eps <= a1 <= hi + eps:
        return [(0.0, 1.0)]
    cuts = {0.0, 1.0}
    if abs(a1 - a0) > eps:
        for bound in (lo, hi):
            f = (bound - a0) / (a1 - a0)
            if -eps <= f <= 1 + eps:
                cuts.add(min(1.0, max(0.0, f)))
    out = []
    for f0, f1 in zip(sorted(cuts), sorted(cuts)[1:]):
        mid = (f0 + f1) / 2
        a = interp(a0, a1, mid)
        if lo - eps <= a <= hi + eps:
            out.append((f0, f1))
    return out


def overlap_frac(ranges, f_lo, f_hi, eps=1e-9):
    """区间列表与单区间求交；零宽交点（边界擦碰）保留为退化区间，供瞬时判定。"""
    out = []
    for a, b in ranges:
        lo, hi = max(a, f_lo), min(b, f_hi)
        if hi >= lo - eps:
            out.append((lo, max(lo, hi)))
    return out


def component_frac_ranges(geom, seg):
    """相交几何的每个连通部件在航段方向上投影出的 f 区间（不跨部件合并）。

    凹形禁飞区可能让同一条走廊先后穿过多个互不相连的部件（中间留有
    空隙），必须逐部件保留各自区间——若笼统取所有点的 min/max，空隙
    也会被算作冲突。投影是线性的，部件投影的极值必在其顶点上取得，
    因此投影顶点坐标即得该部件的 f 范围。
    """
    x0, y0, ux, uy, d = seg["x0"], seg["y0"], seg["ux"], seg["uy"], seg["d"]
    ranges = []

    def emit(coords):
        fs = [min(1.0, max(0.0, ((x - x0) * ux + (y - y0) * uy) / d)) for x, y in coords]
        if fs:
            ranges.append((min(fs), max(fs)))

    def visit(g):
        gtype = g.geom_type
        if gtype == "Polygon":
            coords = list(g.exterior.coords)
            for ring in g.interiors:
                coords.extend(ring.coords)
            emit(coords)
        elif gtype == "LineString":
            emit(list(g.coords))
        elif gtype == "Point":
            emit([(g.x, g.y)])
        elif gtype in ("MultiPolygon", "MultiLineString", "MultiPoint", "GeometryCollection"):
            for sub in g.geoms:
                if not sub.is_empty:
                    visit(sub)

    if not geom.is_empty:
        visit(geom)
    return ranges


def merge_frac_ranges(ranges, eps=1e-9):
    """投影重叠/相接的区间求并集：同一 f 上任一部件覆盖即算平面冲突。"""
    merged = []
    for lo, hi in sorted(ranges):
        if merged and lo <= merged[-1][1] + eps:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def windows_for_nfz(nfz):
    st, en = parse_dt(nfz.get("startTime")), parse_dt(nfz.get("endTime"))
    if st and en and en < st:
        return None, "禁飞区结束时刻早于开始时刻"
    return [(st, en)], None


# ---------------------------------------------------------------- 核心分析
def analyze(state):
    errors = []
    route = state.get("route") or {}
    wps = route.get("waypoints") or []
    speeds = route.get("speeds") or []
    half_w = to_float(route.get("corridorHalfWidth"), 3.0)
    if half_w <= 0:
        errors.append({"scope": "route", "message": "走廊半宽必须为正数"})
        half_w = 3.0

    # —— 航段距离与 ETA（画布单位 ÷ unitScale = km）
    unit_scale = to_float(route.get("unitScale"), 100.0)
    if unit_scale <= 0:
        errors.append({"scope": "route", "message": "画布比例（单位/km）必须为正数"})
        unit_scale = 100.0
    distances, times = [], [None] * len(wps)
    dep = parse_dt(route.get("departureTime"))
    if wps:
        if dep is None:
            errors.append({"scope": "route", "message": "起飞时刻缺失或格式错误"})
        else:
            times[0] = dep
    total_len = 0.0
    from datetime import timedelta

    for k in range(max(0, len(wps) - 1)):
        p0, p1 = wps[k], wps[k + 1]
        d_px = math.hypot(to_float(p1.get("x"), 0) - to_float(p0.get("x"), 0),
                          to_float(p1.get("y"), 0) - to_float(p0.get("y"), 0))
        d = d_px / unit_scale  # km
        distances.append(round(d, 3))
        total_len += d
        if times[k] is not None:
            spd = to_float(speeds[k] if k < len(speeds) else None)
            if spd is None or spd <= 0:
                errors.append({"scope": f"seg{k}", "message": f"航段 {k + 1} 速度无效，后续时刻无法推算"})
            elif d_px <= 0:
                times[k + 1] = times[k]
            else:
                times[k + 1] = times[k] + timedelta(minutes=d / spd * 60.0)

    route_info = {
        "length": round(total_len, 3),
        "distances": distances,
        "times": [fmt_dt(t) for t in times],
        "durationMinutes": round((times[-1] - dep).total_seconds() / 60.0, 2)
        if dep and len(times) > 1 and times[-1] is not None else None,
    }

    # —— 航段走廊
    corridors = []
    for k in range(max(0, len(wps) - 1)):
        p0, p1 = wps[k], wps[k + 1]
        x0, y0 = to_float(p0.get("x")), to_float(p0.get("y"))
        x1, y1 = to_float(p1.get("x")), to_float(p1.get("y"))
        a0, a1 = to_float(p0.get("alt")), to_float(p1.get("alt"))
        if None in (x0, y0, x1, y1, a0, a1):
            errors.append({"scope": f"seg{k}", "message": f"航段 {k + 1} 坐标/高度数据不完整"})
            corridors.append(None)
            continue
        d_px = math.hypot(x1 - x0, y1 - y0)
        if d_px <= 0:
            corridors.append(None)
            continue
        # 走廊半宽按 km 录入，缓冲换算成画布单位
        corridor = LineString([(x0, y0), (x1, y1)]).buffer(half_w * unit_scale)
        ux, uy = (x1 - x0) / d_px, (y1 - y0) / d_px  # 单位方向
        corridors.append({"corridor": corridor, "x0": x0, "y0": y0, "ux": ux, "uy": uy,
                          "d": d_px, "a0": a0, "a1": a1})

    # —— 禁飞多边形
    nfz_polys = []
    for idx, nfz in enumerate(state.get("nfzs") or []):
        if not nfz.get("enabled", True):
            nfz_polys.append(None)
            continue
        raw = nfz.get("vertices") or []
        coords = []
        for v in raw:
            x, y = to_float(v[0] if isinstance(v, (list, tuple)) else v.get("x")), \
                   to_float(v[1] if isinstance(v, (list, tuple)) else v.get("y"))
            if x is not None and y is not None:
                coords.append((x, y))
        lo, hi = to_float(nfz.get("minAlt"), 0.0), to_float(nfz.get("maxAlt"), 0.0)
        if lo is None or hi is None or hi < lo:
            errors.append({"scope": nfz.get("id", f"nfz{idx}"),
                           "message": f"禁飞区「{nfz.get('name', idx)}」高度层无效"})
            nfz_polys.append(None)
            continue
        wins, werr = windows_for_nfz(nfz)
        if werr:
            errors.append({"scope": nfz.get("id", f"nfz{idx}"),
                           "message": f"禁飞区「{nfz.get('name', idx)}」{werr}"})
            nfz_polys.append(None)
            continue
        if len(coords) < 3:
            errors.append({"scope": nfz.get("id", f"nfz{idx}"),
                           "message": f"禁飞区「{nfz.get('name', idx)}」顶点少于 3 个"})
            nfz_polys.append(None)
            continue
        poly = Polygon(coords).buffer(0)
        if poly.is_empty or not poly.geom_type.startswith(("Polygon", "MultiPolygon")):
            errors.append({"scope": nfz.get("id", f"nfz{idx}"),
                           "message": f"禁飞区「{nfz.get('name', idx)}」多边形无效：{explain_validity(Polygon(coords))}"})
            nfz_polys.append(None)
            continue
        nfz_polys.append({"poly": poly, "lo": lo, "hi": hi, "windows": wins})

    # —— 相交检测
    entries = []
    for k, seg in enumerate(corridors):
        if seg is None:
            continue
        t0, t1 = times[k] if k < len(times) else None, times[k + 1] if k + 1 < len(times) else None
        for idx, nz in enumerate(nfz_polys):
            if nz is None:
                continue
            nfz = state["nfzs"][idx]
            inter = seg["corridor"].intersection(nz["poly"])
            if inter.is_empty:
                continue

            # 平面相交 → 按连通部件分别投影到航段方向，再求并集。
            # 凹形禁飞区会让走廊先后穿过多个分离部件（中间留有空隙），
            # 若笼统取 min/max，空隙也会被算作冲突，时窗仅罩住空隙时就会误报。
            f_planar = merge_frac_ranges(component_frac_ranges(inter, seg))
            if not f_planar:
                continue

            # ∩ 高度重叠子区间（逐段分别求交）
            alt_ranges = altitude_frac_ranges(seg["a0"], seg["a1"], nz["lo"], nz["hi"])
            spatial = []
            for fa, fb in f_planar:
                spatial.extend(overlap_frac(alt_ranges, fa, fb))
            if not spatial:
                continue

            # ∩ 时间窗（逐段判定：时窗只罩住哪一段就只报告哪一段）
            for f_a, f_b in spatial:
                instant = f_b - f_a <= 1e-9
                for w_start, w_end in nz["windows"]:
                    have_time = t0 is not None and t1 is not None
                    enter = t0 + (t1 - t0) * f_a if have_time else None
                    exit_t = t0 + (t1 - t0) * f_b if have_time else None
                    f_enter, f_exit = f_a, f_b
                    if instant:
                        # 瞬时擦碰：仅当该时刻落在生效窗内才算
                        if w_start and enter and enter < w_start:
                            continue
                        if w_end and enter and enter > w_end:
                            continue
                    else:
                        if w_start and exit_t and exit_t <= w_start:
                            continue
                        if w_end and enter and enter >= w_end:
                            continue
                        if w_start and enter and enter < w_start:
                            enter = w_start
                        if w_end and exit_t and exit_t > w_end:
                            exit_t = w_end
                        # 时窗截短区间 → 进入/离开参数随截短后的时刻回算，
                        # 见证点与高度范围随之对应截短后的最终区间
                        if have_time:
                            span = (t1 - t0).total_seconds()
                            if span > 0:
                                f_enter = min(1.0, max(0.0, (enter - t0).total_seconds() / span))
                                f_exit = min(1.0, max(0.0, (exit_t - t0).total_seconds() / span))
                    if enter and exit_t and exit_t < enter:
                        continue
                    # 以下字段全部对应同一段最终冲突区间 [f_enter, f_exit]
                    alt_at_enter = interp(seg["a0"], seg["a1"], f_enter)
                    alt_at_exit = interp(seg["a0"], seg["a1"], f_exit)
                    alt_clip_lo = round(max(min(alt_at_enter, alt_at_exit), nz["lo"]), 2)
                    alt_clip_hi = round(min(max(alt_at_enter, alt_at_exit), nz["hi"]), 2)
                    wx = seg["x0"] + seg["ux"] * seg["d"] * f_enter
                    wy = seg["y0"] + seg["uy"] * seg["d"] * f_enter
                    entries.append({
                        "nfzId": nfz.get("id"),
                        "nfzName": nfz.get("name", f"禁飞区{idx + 1}"),
                        "segIndex": k,
                        "enterFrac": round(f_enter, 5),
                        "exitFrac": round(f_exit, 5),
                        "enterTime": fmt_dt(enter),
                        "exitTime": fmt_dt(exit_t),
                        "durationMinutes": round((exit_t - enter).total_seconds() / 60.0, 2)
                        if enter and exit_t else None,
                        "altLow": alt_clip_lo,
                        "altHigh": alt_clip_hi,
                        "witness": {"x": round(wx, 2), "y": round(wy, 2),
                                    "alt": round(alt_at_enter, 2)},
                        "instant": instant,
                    })

    entries.sort(key=lambda e: (e["enterTime"] is None, e["enterTime"] or ""))
    first = None
    if entries:
        e = entries[0]
        first = {
            "nfzId": e["nfzId"], "nfzName": e["nfzName"], "segIndex": e["segIndex"],
            "enterTime": e["enterTime"], "exitTime": e["exitTime"],
            "durationMinutes": e["durationMinutes"],
            "witness": e["witness"], "altLow": e["altLow"], "altHigh": e["altHigh"],
        }

    return {"ok": True, "errors": errors, "route": route_info,
            "entries": entries, "first": first}


# ---------------------------------------------------------------- 路由
@app.get("/")
def index():
    return send_from_directory("templates", "index.html")


@app.get("/api/state")
def get_state():
    row = get_db().execute("SELECT v FROM kv WHERE k='state'").fetchone()
    return jsonify(json.loads(row["v"]) if row else {"route": {}, "nfzs": []})


@app.put("/api/state")
def put_state():
    state = request.get_json(force=True, silent=True)
    if not isinstance(state, dict):
        return jsonify({"ok": False, "error": "请求体必须是对象"}), 400
    db = get_db()
    db.execute(
        "INSERT INTO kv(k,v) VALUES('state',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (json.dumps(state, ensure_ascii=False),),
    )
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/analyze")
def api_analyze():
    state = request.get_json(force=True, silent=True)
    if not isinstance(state, dict):
        return jsonify({"ok": False, "error": "请求体必须是对象"}), 400
    return jsonify(analyze(state))


@app.get("/api/versions")
def list_versions():
    rows = get_db().execute(
        "SELECT id,name,created_at FROM versions ORDER BY id DESC"
    ).fetchall()
    return jsonify([{"id": r["id"], "name": r["name"], "createdAt": r["created_at"]} for r in rows])


@app.post("/api/versions")
def create_version():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    state = body.get("state")
    if not isinstance(state, dict):
        return jsonify({"ok": False, "error": "缺少 state"}), 400
    created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not name:
        name = f"版本 {created}"
    db = get_db()
    cur = db.execute(
        "INSERT INTO versions(name,created_at,payload) VALUES(?,?,?)",
        (name, created, json.dumps(state, ensure_ascii=False)),
    )
    db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid, "name": name, "createdAt": created})


@app.get("/api/versions/<int:vid>")
def get_version(vid):
    row = get_db().execute(
        "SELECT name,created_at,payload FROM versions WHERE id=?", (vid,)
    ).fetchone()
    if row is None:
        return jsonify({"ok": False, "error": "版本不存在"}), 404
    return jsonify({"id": vid, "name": row["name"], "createdAt": row["created_at"],
                    "state": json.loads(row["payload"])})


@app.delete("/api/versions/<int:vid>")
def delete_version(vid):
    db = get_db()
    db.execute("DELETE FROM versions WHERE id=?", (vid,))
    db.commit()
    return jsonify({"ok": True})


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
