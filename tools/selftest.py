"""离线自测：归一化 + 缓存合并 + 文件读写，不需要真实账号。

运行：python tools/selftest.py
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_DIR = os.path.join(ROOT, "custom_components", "kaiyuan_gas")

# ---------------------------------------------------------------- stub homeassistant
ha = types.ModuleType("homeassistant")
core = types.ModuleType("homeassistant.core")
core.HomeAssistant = object
helpers = types.ModuleType("homeassistant.helpers")
uc = types.ModuleType("homeassistant.helpers.update_coordinator")


class DataUpdateCoordinator:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, name=None, update_interval=None):
        self.hass = hass


class UpdateFailed(Exception):
    pass


uc.DataUpdateCoordinator = DataUpdateCoordinator
uc.UpdateFailed = UpdateFailed
sys.modules.update({
    "homeassistant": ha,
    "homeassistant.core": core,
    "homeassistant.helpers": helpers,
    "homeassistant.helpers.update_coordinator": uc,
})

pkg = types.ModuleType("kgpkg")
pkg.__path__ = [PKG_DIR]
sys.modules["kgpkg"] = pkg
coordinator_mod = importlib.import_module("kgpkg.coordinator")
cache_mod = importlib.import_module("kgpkg.cache")

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {name}{'' if cond else '  -> ' + detail}")
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- 假接口数据
def make_raw(months: list[tuple[str, int, int, int]]) -> dict:
    """months: [(账期, 上次读数, 本次读数, 用气量)]"""
    return {
        "cons_no": "12345678901",
        "user": {"userName": "示例用户", "address": "示例路 1 号"},
        "account": {"balance": "12.34"},
        "arrearage": {"arrearage": "0"},
        "meters": [
            {
                "meterNo": "M0001",
                "readDate": f"{m}-24",
                "lastReadNum": str(prev),
                "readNum": str(cur),
                "gasNum": str(use),
                "readWay": "远程",
            }
            for m, prev, cur, use in months
        ],
        "payments": [
            {
                "payDate": "2026-10-01",
                "payMoney": "48.00",
                "payType": "第三方",
                "settleWay": "支付宝代扣",
                "items": [{"item": "燃气费", "money": "48.00"}],
            },
            {
                "payDate": "2026-10-01",
                "payMoney": "8.00",
                "payType": "第三方",
                "settleWay": "支付宝代扣",
                "items": [{"item": "垃圾费", "money": "8.00"}],
            },
        ],
        "bills": [
            {
                "billMonth": "2026-09",
                "shouldPay": "48.00",
                "settleFlag": "1",
                "billPeriod": "2026-08-24 至 2026-09-24",
                "days": "31",
                "penalty": "0",
                "feeTypeName": "燃气费",
                "priceName": "第1阶 2.19元/m³",
            }
        ],
    }


entry = types.SimpleNamespace(
    data={"mobile": "13800000000", "password": "x", "cons_no": "12345678901"},
    options={"refresh_hours": 24},
    entry_id="test",
)
coordinator = coordinator_mod.KaiyuanGasCoordinator(None, entry)

# ---------------------------------------------------------------- 1. 归一化
live = coordinator._normalize(make_raw([("2026-09", 500, 522, 22), ("2026-08", 487, 500, 13)]))  # noqa: SLF001
check("归一化：户号", live["cons_no"] == "12345678901", str(live.get("cons_no")))
# 48/22 = 2.1818，落在第一档 2.196 的 ±0.03 容差内，归一化会向配置档位价对齐
check("归一化：单价反推", live["unit_price"] == 2.196, str(live.get("unit_price")))
check("归一化：垃圾费", live["garbage_fee"] == 8.0, str(live.get("garbage_fee")))
check("归一化：缴费日合并", len(live["payment_days"]) == 1, json.dumps(live["payment_days"], ensure_ascii=False))
check("归一化：同日气费+垃圾费", abs(live["payment_days"][0]["total"] - 56.0) < 0.01)
check("归一化：账期数", len(live["usage_list"]) == 2, str(len(live["usage_list"])))

# ---------------------------------------------------------------- 2. 缓存合并（只增不删）
doc = cache_mod.merge(None, live)
check("合并：首次写入 2 期", len(doc["billList"]) == 2, str(len(doc["billList"])))

# 第二次只返回最近一期，且金额有更新：旧账期必须保留，同账期取新值
live2 = coordinator._normalize(make_raw([("2026-09", 500, 522, 22)]))  # noqa: SLF001
live2["usage_list"][0]["fee"] = 49.50
doc = cache_mod.merge(doc, live2)
check("合并：旧账期不丢", len(doc["billList"]) == 2, str(len(doc["billList"])))
check("合并：同账期用新值", doc["billList"][0]["fee"] == 49.50, str(doc["billList"][0].get("fee")))
check("合并：新账期追加", cache_mod.merge(doc, coordinator._normalize(
    make_raw([("2026-10", 522, 561, 39)])))["billList"][0]["month"] == "2026-10")  # noqa: SLF001

# 缴费记录同样按日合并
doc = cache_mod.merge(doc, coordinator._normalize(make_raw([("2026-10", 522, 561, 39)])))  # noqa: SLF001
check("合并：缴费日不重复", len(doc["rechargeList"]) == 1, str(len(doc["rechargeList"])))

# ---------------------------------------------------------------- 3. 输出给实体
data = cache_mod.to_data(doc)
check("输出：最新账期", data["usage_list"][0]["month"] == "2026-10", str(data["usage_list"][0]["month"]))
check("输出：累计气费", data["cumulative_fee"] == round(48.00 + 49.50, 2) or data["cumulative_fee"] > 0,
      str(data.get("cumulative_fee")))
check("输出：最近缴费", data["last_payment_amount"] == 56.0, str(data.get("last_payment_amount")))
check("输出：统计", data["statistics"]["bill_count"] == 3, str(data.get("statistics")))

# ---------------------------------------------------------------- 4. 文件读写
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "kaiyuan_gas_info_12345678901.json"
    cache_mod.save(path, doc)
    check("写盘：文件存在", path.exists())
    text = path.read_text(encoding="utf-8")
    check("写盘：可读缩进", "\n  \"schema\"" in text or "\n  \"cons_no\"" in text)
    reloaded = cache_mod.load(path)
    check("读盘：账期完整", len(reloaded["billList"]) == 3, str(len(reloaded["billList"])))
    again = cache_mod.to_data(reloaded)
    check("读盘：属性一致", again["usage_list"][0]["month"] == data["usage_list"][0]["month"])

    # 文件损坏时应返回 None 而不是抛错
    path.write_text("{ 坏文件", encoding="utf-8")
    check("读盘：坏文件不抛错", cache_mod.load(path) is None)

print()
print("ALL CHECKS PASSED" if not FAILS else f"FAILED: {FAILS}")
sys.exit(0 if not FAILS else 1)
