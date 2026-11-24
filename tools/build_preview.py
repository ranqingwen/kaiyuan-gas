"""把 tools/demo_attrs.json 写进 preview.html 的演示数据块，刷新卡片静态预览。

用法：python tools/demo_data.py && python tools/build_preview.py
然后用浏览器打开 preview.html 即可（无需 Home Assistant）。
"""

from __future__ import annotations

import io
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ATTRS = os.path.join(ROOT, "tools", "demo_attrs.json")
PAGE = os.path.join(ROOT, "preview.html")

START = "        // DEMO_ATTRS_START"
END = "        // DEMO_ATTRS_END"


def main() -> None:
    with io.open(ATTRS, encoding="utf-8") as fp:
        data = json.load(fp)

    # 卡片兼容旧字段名
    data["last_pay_time"] = data.get("last_payment_date")
    data["last_pay_at"] = data.get("last_payment_at")
    data["last_pay_amount"] = data.get("last_payment_amount")
    data["last_pay_gas"] = data.get("last_payment_gas")
    data["last_pay_garbage"] = data.get("last_payment_garbage")
    data["last_pay_penalty"] = data.get("last_payment_penalty")
    data["records"] = data.get("bill_count")

    lines = json.dumps(data, ensure_ascii=False, indent=2).split("\n")[1:-1]
    inner = "\n".join("        " + line for line in lines)

    with io.open(PAGE, encoding="utf-8") as fp:
        html = fp.read()
    head, rest = html.split(START, 1)
    _, tail = rest.split(END, 1)
    html = head + START + " 由 tools/build_preview.py 生成，请勿手工修改\n" + inner + "\n" + END + tail
    with io.open(PAGE, "w", encoding="utf-8") as fp:
        fp.write(html)
    print("preview.html 已更新 ->", PAGE)


if __name__ == "__main__":
    main()
