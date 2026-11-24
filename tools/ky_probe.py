#!/usr/bin/env python3
"""凯源燃气接口调试脚本。

用途：在接入 Home Assistant 之前先确认账号能登录、并把各接口的原始返回 dump 出来，
方便核对字段名（不同城市/租户的字段偶尔会不一样）。

用法：
    pip install requests cryptography
    python tools/ky_probe.py --mobile 13800000000 --password '你的密码'
    python tools/ky_probe.py --mobile 13800000000 --password '你的密码' --cons-no 户号 --dump out.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
API_FILE = os.path.join(HERE, "..", "custom_components", "kaiyuan_gas", "api.py")


def _load_api():
    """直接按文件加载 api.py。

    走 `custom_components.kaiyuan_gas.api` 会连带执行包 __init__，那里 import
    homeassistant；本脚本是脱离 HA 单独调试用的，所以绕开包导入。
    """
    spec = importlib.util.spec_from_file_location("kaiyuan_gas_api", API_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


_api = _load_api()
KaiyuanGasAuthError = _api.KaiyuanGasAuthError
KaiyuanGasClient = _api.KaiyuanGasClient
KaiyuanGasError = _api.KaiyuanGasError


def show(title: str, data) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(data, ensure_ascii=False, indent=2)[:4000])


def main() -> int:
    parser = argparse.ArgumentParser(description="凯源燃气接口调试")
    parser.add_argument("--mobile", required=True, help="登录手机号")
    parser.add_argument("--password", required=True, help="登录密码")
    parser.add_argument("--cons-no", default=None, help="户号（可选）")
    parser.add_argument("--dump", default=None, help="把全部原始数据写入该 JSON 文件")
    args = parser.parse_args()

    client = KaiyuanGasClient(args.mobile, args.password, args.cons_no)
    dump: dict[str, object] = {}

    try:
        client.handshake()
        print("握手成功，secret =", client.secret)
        client.login()
        print("登录成功，token =", (client.token or "<未返回 token，可能用 cookie 维持会话>"))

        info = client.get_user_info()
        dump["userInfo"] = info
        show("my/getUserInfo", info)

        cons_no = client._cons_no()  # noqa: SLF001
        print("\n使用户号:", cons_no)

        checks = [
            ("micro/bill/accountInfo", {"consNo": cons_no}),
            ("micro/bill/arrearage", {"consNo": cons_no}),
            ("micro/bill/feeRecord", {"consNo": cons_no, "pageNo": 1, "pageSize": 10, "settleFlag": 0, "feeType": 0}),
            ("micro/bill/payMentRecord", {"consNo": cons_no, "pageNo": 1, "pageSize": 10}),
            ("micro/readMeter/mrInfo", {"consNo": cons_no, "pageSize": 12}),
        ]
        for path, payload in checks:
            try:
                resp = client._post(path, payload)  # noqa: SLF001
            except KaiyuanGasError as err:
                print(f"\n===== {path} =====\n失败: {err}")
                continue
            dump[path] = resp
            show(path, resp.get("obj"))

    except KaiyuanGasAuthError as err:
        print(f"\n[认证失败] {err}", file=sys.stderr)
        return 2
    except KaiyuanGasError as err:
        print(f"\n[接口错误] {err}", file=sys.stderr)
        return 1

    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as fh:
            json.dump(dump, fh, ensure_ascii=False, indent=2)
        print(f"\n原始数据已写入 {args.dump}")

    print("\n完成。把上面的输出发给我，就能把卡片字段对上。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
