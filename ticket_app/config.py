from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
CHINA = timezone(timedelta(hours=8))
SEATS = {
    "商务座": "SWZ_", "特等座": "TZ_", "优选一等座": "GG_",
    "一等座": "ZY_", "二等座": "ZE_", "高级软卧": "GR_",
    "软卧": "RW_", "动卧": "RW_", "一等卧": "RW_",
    "硬卧": "YW_", "二等卧": "YW_", "软座": "RZ_", "硬座": "YZ_", "无座": "WZ_",
}


def stations() -> dict[str, str]:
    return json.loads((ROOT / "data/stations.json").read_text(encoding="utf-8"))


def defaults() -> dict:
    tomorrow = (datetime.now(CHINA) + timedelta(days=1)).date().isoformat()
    return {"from": "", "to": "", "travelDate": tomorrow,
            "saleAt": tomorrow + "T09:15:00+08:00", "passengers": [], "preferences": [],
            "allowNoSeat": False, "berth": "不限", "waitlist": "提醒", "seatPosition": "自动分配",
            "pollIntervalMs": 5000, "maxRunMinutes": 10, "browser": "chrome", "fastStart": True}


def validate(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("配置必须是一个 JSON 对象。")
    c = defaults() | raw
    if c["seatPosition"] not in ("自动分配", "靠窗优先", "靠过道优先"):
        raise ValueError("座位偏好请选择自动分配、靠窗优先或靠过道优先。")
    if set(raw) - set(defaults()):
        raise ValueError("配置包含无法识别的字段，请使用本程序保存的配置。")
    for key in ("from", "to", "travelDate", "saleAt"):
        if not isinstance(c[key], str):
            raise ValueError("车站、日期和时间应填写为文字。")
        c[key] = c[key].strip()
    names = stations()
    if c["from"] not in names or c["to"] not in names:
        raise ValueError("请填写精确车站名称，例如“北京西”“西安北”。")
    if c["from"] == c["to"]:
        raise ValueError("出发站与到达站不能相同。")
    try:
        trip = date.fromisoformat(c["travelDate"])
        if trip.isoformat() != c["travelDate"]:
            raise ValueError()
        sale = datetime.fromisoformat(c["saleAt"])
        if sale.tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("日期格式为 YYYY-MM-DD，开售时间需包含时区。") from None
    if sale.astimezone(CHINA).date() > trip:
        raise ValueError("开售日期不能晚于乘车日期。")
    for key, low, high in (("passengers", 1, 9), ("preferences", 1, 30)):
        if not isinstance(c[key], list) or not low <= len(c[key]) <= high:
            raise ValueError(f"请添加 {low}–{high} 个{'乘客' if key == 'passengers' else '车次与席别组合'}。")
    people = []
    for p in c["passengers"]:
        if not isinstance(p, dict) or set(p) - {"name", "ticket"}:
            raise ValueError("乘客配置格式不正确。")
        name = p.get("name", "")
        ticket = p.get("ticket", "成人票")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60 or ticket not in ("成人票", "学生票"):
            raise ValueError("乘客姓名不能为空，票种支持成人票和学生票。")
        people.append({"name": name.strip(), "ticket": ticket})
    if len({p["name"] for p in people}) != len(people):
        raise ValueError("乘客姓名不能重复；同名乘客请在网站人工处理。")
    prefs = []
    for p in c["preferences"]:
        if not isinstance(p, dict) or set(p) != {"train", "seat"} or not isinstance(p["train"], str):
            raise ValueError("车次与席别配置格式不正确。")
        train = p["train"].strip().upper()
        if not re.fullmatch(r"[GDCZTKYS]?\d{1,5}", train) or p["seat"] not in SEATS:
            raise ValueError("请检查车次编号和席别，例如 G87 / 二等座。")
        prefs.append({"train": train, "seat": p["seat"]})
    if len({(p["train"], p["seat"]) for p in prefs}) != len(prefs):
        raise ValueError("车次和席别组合不能重复。")
    if type(c["fastStart"]) is not bool:
        raise ValueError("开售快速查询选项必须为布尔值。")
    if type(c["allowNoSeat"]) is not bool:
        raise ValueError("是否接受无座必须为布尔值。")
    if not c["allowNoSeat"] and any(p["seat"] == "无座" for p in prefs):
        raise ValueError("已选择无座席别，请先勾选“接受无座”。")
    for key, low, high in (("pollIntervalMs", 100, 60000), ("maxRunMinutes", 0.1, 120)):
        if type(c[key]) not in (int, float) or not low <= c[key] <= high:
            raise ValueError("查询间隔需为 0.1–60 秒，查询时长需为 0.1–120 分钟。")
    if c["berth"] not in ("不限", "中铺优先", "下铺优先", "必须下铺") or c["waitlist"] not in ("提醒", "关闭"):
        raise ValueError("铺位或候补选项不正确。")
    if c["browser"] not in ("chrome", "msedge", "chromium"):
        raise ValueError("浏览器选项不正确。")
    c["passengers"], c["preferences"] = people, prefs
    return c


def load_config(path: Path) -> dict:
    return validate(json.loads(path.read_text(encoding="utf-8-sig")))


def save_config(path: Path, config: dict) -> None:
    c = validate(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def query_url(c: dict) -> str:
    codes = stations()
    params = {"linktypeid": "dc", "fs": f'{c["from"]},{codes[c["from"]]}',
              "ts": f'{c["to"]},{codes[c["to"]]}', "date": c["travelDate"], "flag": "N,N,Y"}
    return "https://kyfw.12306.cn/otn/leftTicket/init?" + urlencode(params, safe=",")


def choose_offer(c: dict, offers: list[dict]) -> dict | None:
    for pref in c["preferences"]:
        for offer in offers:
            stock = re.sub(r"\s+", "", offer["availability"]).removesuffix("折")
            enough = stock == "有" or (stock.isdecimal() and int(stock) >= len(c["passengers"]))
            if offer["train"] == pref["train"] and offer["seat"] == pref["seat"] and offer["bookable"] and enough:
                return offer
    return None


def journey_key(c: dict) -> str:
    # Preserve the exact JS serialization so migration cannot bypass prior submit guards.
    key = [c["from"], c["to"], c["travelDate"], sorted(p["name"] for p in c["passengers"])]
    return hashlib.sha256(json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:24]
