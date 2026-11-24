"""凯源燃气云平台 API 客户端。

协议说明（逆向自 H5 端 https://wx.kaiyuan.qjcode.com/page/）：

1. 握手：POST /api/sec/serverKey  {key: 客户端RSA公钥(SPKI base64), data: ""}
   返回 obj.key = 会话 secret（后续请求放请求头 secret），obj.data = 服务端 RSA 公钥。
2. 请求：正文 {data: AES密文, key: RSA密文}
   - 每次请求随机生成 32 位字符串作为 AES 密钥（AES/ECB/PKCS7），
     明文为业务 JSON，密文做 base64 放入 data。
   - 用服务端 RSA 公钥（PKCS1v15）加密该 AES 密钥，base64 放入 key。
3. 响应：同样返回 {data, key}，key 用客户端私钥解出 AES 密钥，再解 data。
4. 登录：POST /api/login/doLoginByPwd {mobile, password, agreement:true}，
   成功后拿到 token，后续请求放请求头 token。
"""

from __future__ import annotations

import base64
import json
import logging
import random
import re
import string
import time
from typing import Any

import requests

# cryptography 首次导入要两百毫秒上下，放在模块级会被 HA 判成
# "在事件循环里做阻塞调用"；改成用到时再导入（那时已在 executor 线程）。
_LOGGER = logging.getLogger(__name__)

BASE_URL = "http://wx.kaiyuan.qjcode.com/api/"
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.40"
)

CODE_OK = {"0", "200"}

# 逐期账单详情最多拉几期：每期一个请求。默认拉满两年（24 期），
# 已经拿到费用的月份会跳过（见 fetch_all 的 known_months），所以日常刷新只补新月份。
BILL_DETAIL_LIMIT = 24

# 租户 ID：业务接口必须带 fromDomainId 请求头，否则统一返回 code 999 / msg 为空。
# 登录响应里的 user.domainId 优先，取不到时用这个默认值（凯源燃气重庆）。
DEFAULT_DOMAIN_ID = "3000000036"


class KaiyuanGasError(Exception):
    """通用错误。"""


class KaiyuanGasConnectionError(KaiyuanGasError):
    """网络层错误：连不上服务器 / 超时 / 返回非 JSON。"""


class KaiyuanGasAuthError(KaiyuanGasError):
    """登录态失效 / 账号密码错误。"""


class KaiyuanGasLockedError(KaiyuanGasAuthError):
    """账号被临时锁定（连续密码错误触发）。"""


class KaiyuanGasApiError(KaiyuanGasError):
    """接口返回了业务错误（服务能连上，但业务拒绝）。"""


# 账号被锁定的服务端提示特征
_LOCK_HINTS = ("超过最大限制", "限制24小时", "锁定", "暂时无法登录", "频繁")


def _pkcs7_pad(data: bytes) -> bytes:
    n = 16 - len(data) % 16
    return data + bytes([n]) * n


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    n = data[-1]
    if n < 1 or n > 16:
        return data
    return data[:-n]


class KaiyuanGasClient:
    """同步客户端，调用方负责放到线程池里执行。"""

    def __init__(self, mobile: str, password: str, cons_no: str | None = None,
                 timeout: int = 25) -> None:
        self.mobile = mobile.strip()
        self.password = password
        self.cons_no = cons_no
        self.timeout = timeout

        # RSA 密钥推迟到 handshake 里再生成：ctor 可能在事件循环里跑，
        # 2048 位密钥生成是纯 CPU 活，放在这里会被 HA 判为 blocking call。
        self._private_key: Any = None
        self._public_b64: str | None = None

        self._server_pub: Any = None
        self.secret: str | None = None
        self.token: str | None = None
        self.user_info: dict[str, Any] = {}
        self.domain_id: str = DEFAULT_DOMAIN_ID
        # 排查用：每个接口最近一次的入参与结果（诊断文件里会带上）
        self.last_payloads: dict[str, Any] = {}
        self.endpoint_debug: dict[str, Any] = {}

        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json; charset=utf-8",
            "part": "WECHAT",
            "fromDomainId": self.domain_id,
            "Origin": "http://wx.kaiyuan.qjcode.com",
            "Referer": "http://wx.kaiyuan.qjcode.com/page/",
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        })

    # ------------------------------------------------------------------ 基础
    @staticmethod
    def _aes_key() -> str:
        alphabet = string.ascii_letters + string.digits
        return "".join(random.choice(alphabet) for _ in range(32))

    def _encrypt(self, payload: dict[str, Any]) -> dict[str, str]:
        if self._server_pub is None:
            raise KaiyuanGasError("尚未完成密钥交换")

        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        plaintext = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        aes_key = self._aes_key()

        encryptor = Cipher(algorithms.AES(aes_key.encode()), modes.ECB()).encryptor()
        cipher_text = encryptor.update(_pkcs7_pad(plaintext)) + encryptor.finalize()

        wrapped = self._server_pub.encrypt(aes_key.encode(), padding.PKCS1v15())
        return {
            "data": base64.b64encode(cipher_text).decode(),
            "key": base64.b64encode(wrapped).decode(),
        }

    def _ensure_keys(self) -> None:
        """按需生成客户端 RSA 密钥对（幂等）。"""
        if self._private_key is not None:
            return
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._public_b64 = base64.b64encode(
            self._private_key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).decode()

    def _decrypt(self, body: dict[str, Any]) -> dict[str, Any]:
        self._ensure_keys()
        try:
            from cryptography.hazmat.primitives.asymmetric import padding
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

            aes_key = self._private_key.decrypt(  # type: ignore[union-attr]
                base64.b64decode(body["key"]), padding.PKCS1v15()
            ).decode()
            decryptor = Cipher(algorithms.AES(aes_key.encode()), modes.ECB()).decryptor()
            plain = decryptor.update(base64.b64decode(body["data"])) + decryptor.finalize()
            return json.loads(_pkcs7_unpad(plain).decode())
        except Exception as err:  # noqa: BLE001
            raise KaiyuanGasError(f"响应解密失败: {err}") from err

    def handshake(self) -> None:
        """获取会话 secret 与服务端公钥。"""
        self._ensure_keys()
        try:
            resp = self._session.post(
                BASE_URL + "sec/serverKey",
                data=json.dumps({"key": self._public_b64, "data": ""}, separators=(",", ":")),
                timeout=self.timeout,
            )
            data = resp.json()
        except requests.RequestException as err:
            raise KaiyuanGasConnectionError(f"无法连接凯源燃气服务器: {err}") from err
        except ValueError as err:
            if resp.status_code in (407, 502, 503, 504):
                raise KaiyuanGasConnectionError(
                    f"网关返回 HTTP {resp.status_code}，请求很可能被代理拦截"
                    "（如 Clash 7897），请把 wx.kaiyuan.qjcode.com 设为直连"
                ) from err
            raise KaiyuanGasConnectionError(f"服务器返回非 JSON (HTTP {resp.status_code})") from err

        obj = data.get("obj") or {}
        if not obj.get("key") or not obj.get("data"):
            raise KaiyuanGasConnectionError(f"密钥交换失败: {data.get('msg') or data}")

        self.secret = obj["key"]
        self._session.headers["secret"] = self.secret
        from cryptography.hazmat.primitives import serialization

        self._server_pub = serialization.load_der_public_key(base64.b64decode(obj["data"]))

    def _post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._server_pub is None:
            self.handshake()

        self.last_payloads.setdefault(path, []).append(payload or {})
        body = self._encrypt(payload or {})
        headers = {}
        if self.token:
            headers["token"] = self.token

        try:
            resp = self._session.post(
                BASE_URL + path.lstrip("/"), json=body, headers=headers, timeout=self.timeout
            )
        except requests.RequestException as err:
            raise KaiyuanGasConnectionError(f"无法连接凯源燃气服务器: {err}") from err

        # 502/504/407 基本都不是凯源的问题：本机或 HA 主机挂了代理（Clash 等）时，
        # 代理会先返回自己的错误页，明文 JSON 解析必然失败，这里直接说清楚。
        if resp.status_code in (407, 502, 503, 504):
            raise KaiyuanGasConnectionError(
                f"网关返回 HTTP {resp.status_code}，请求很可能被本机/路由上的代理拦截"
                "（如 Clash 7897），请给 wx.kaiyuan.qjcode.com 加直连或临时关闭代理"
            )

        try:
            data = resp.json()
        except ValueError as err:
            raise KaiyuanGasConnectionError(
                f"接口返回非 JSON (HTTP {resp.status_code})"
            ) from err

        if isinstance(data, dict) and data.get("key") and data.get("data"):
            data = self._decrypt(data)

        if not isinstance(data, dict):
            raise KaiyuanGasApiError(f"接口返回异常: {data!r}")

        code = str(data.get("code") or "")
        if data.get("success") or code in CODE_OK:
            return data

        msg = str(data.get("msg") or data.get("message") or "").strip()
        # 服务端偶尔返回 success=false 但 msg 为空（例如缺租户头时的 999），
        # 不能直接当"未知错误"抛出去，否则完全没法排查。
        if not msg:
            msg = f"接口返回失败但未给出原因（code={code or '空'}）"
            if code == "999":
                msg += "，通常是租户标识 fromDomainId 缺失或已变更"
        # 账号被连续错误密码锁定时，服务端仍能正常连通，不能当成"连接失败"
        if any(hint in msg for hint in _LOCK_HINTS):
            raise KaiyuanGasLockedError(msg)
        if code in {"120", "401", "403"} or "未登录" in msg:
            raise KaiyuanGasAuthError(f"登录态失效: {msg}")
        if code == "600" or "密码" in msg or "账号" in msg:
            raise KaiyuanGasAuthError(msg)
        raise KaiyuanGasApiError(msg)

    # ------------------------------------------------------------------ 业务
    def _set_domain_id(self, domain_id: str | None) -> None:
        """登录响应里的 user.domainId 是权威值，取到后同步到请求头。"""
        value = str(domain_id or "").strip()
        if not value or value == self.domain_id:
            return
        self.domain_id = value
        self._session.headers["fromDomainId"] = value
        _LOGGER.debug("切换到租户 fromDomainId=%s", value)

    def login(self) -> None:
        """手机号 + 密码登录。"""
        if self._server_pub is None:
            self.handshake()

        try:
            resp = self._post("login/doLoginByPwd", {
                "mobile": self.mobile,
                "password": self.password,
                "wechatProId": "",
                "fromAccountId": "",
                "shareType": "",
                "agreement": True,
            })
        except KaiyuanGasApiError as err:
            # 同一账号短时间连续登录时，服务端会拿这句笼统的"参数错误"顶回来，
            # 参数本身没问题，隔一会儿就好，别让人去翻代码找参数。
            if "参数错误" in str(err):
                raise KaiyuanGasApiError(
                    "服务器判定本次登录请求异常（多见于短时间重复登录触发风控），"
                    "请等 1 分钟再试"
                ) from err
            raise

        obj = resp.get("obj") or {}

        # 新版登录直接把 user 塞在 obj 里，先收下来：既省一次请求，
        # 也让 my/getUserInfo 不可用时仍能拿到 consNo。
        if isinstance(obj, dict) and isinstance(obj.get("user"), dict):
            self.user_info = obj["user"]
            self._set_domain_id(
                self.user_info.get("domainId") or self.user_info.get("currentDomainId")
            )

        token = (
            obj.get("token")
            or obj.get("accessToken")
            or obj.get("access_token")
            or resp.get("token")
        )
        if not token and isinstance(obj, str):
            token = obj
        if token:
            self.token = str(token)
            self._session.headers["token"] = self.token
        else:
            _LOGGER.debug("登录响应未直接给出 token，将携带既有会话继续请求: %s", obj)

    def get_user_info(self) -> dict[str, Any]:
        """用户信息：登录响应里已带回时直接用，避免多余请求。"""
        if self.user_info:
            return self.user_info
        try:
            resp = self._post("my/getUserInfo", {})
        except KaiyuanGasApiError as err:
            # 账号信息不是关键数据，拿不到就让调用方回退到接口返回的 consNo
            _LOGGER.debug("my/getUserInfo 不可用: %s", err)
            return self.user_info
        obj = resp.get("obj") or {}
        if isinstance(obj, list):
            obj = obj[0] if obj else {}
        self.user_info = obj if isinstance(obj, dict) else {}
        if self.user_info:
            self._set_domain_id(
                self.user_info.get("domainId") or self.user_info.get("currentDomainId")
            )
        return self.user_info

    def _cons_no(self) -> str:
        if self.cons_no:
            return self.cons_no
        # 先看账号对象顶层，再看卡片列表的第一张，最后才递归找：
        # 账号里可能绑着好几个户，深挖容易挖到别人家的号。
        if isinstance(self.user_info, dict):
            for key in ("consNo", "cons_no", "gasCardNo", "cardNum", "accountNo"):
                value = self.user_info.get(key)
                if value not in (None, "", [], {}):
                    self.cons_no = str(value).strip()
                    return self.cons_no
        cards = first_list(self.user_info)
        for item in cards:
            if isinstance(item, dict) and item.get("consNo"):
                self.cons_no = str(item["consNo"]).strip()
                return self.cons_no
        for key in ("consNo", "cons_no", "cardNum", "accountNo"):
            value = deep_get(self.user_info, key)
            if value:
                self.cons_no = str(value)
                return self.cons_no
        raise KaiyuanGasError("未找到户号，请在集成选项里手动填写")

    def get_account_info(self) -> dict[str, Any]:
        resp = self._post("micro/bill/accountInfo", {"consNo": self._cons_no()})
        obj = resp.get("obj")
        return obj if isinstance(obj, dict) else {}

    def get_nc_bill(self) -> dict[str, Any]:
        """账单汇总：H5「账单查询」页用的就是这个接口。

        一次返回两段，字段名比别的接口稳得多：

        ``obj.resConsFee``    上期读数 lastRead、本期读数 nearRead、累计欠费 allFee、
                              欠燃气费 amt、违约金 totalRcvblPenalty、初装费 otherFee、
                              当前余额 lastBalance
        ``obj.resBillInfos``  逐期账单：抄表日期 readMeterDate、用气量 gasNum、
                              燃气费 gasAmt、服务费 serviceAmt、本期合计 totalAmt
        """
        resp = self._post("micro/bill/ncBill", {"consNo": self._cons_no()})
        obj = resp.get("obj")
        return obj if isinstance(obj, dict) else {}

    def get_arrearage(self) -> Any:
        try:
            resp = self._post("micro/bill/arrearage", {"consNo": self._cons_no()})
        except KaiyuanGasError as err:
            _LOGGER.debug("欠费接口失败: %s", err)
            return None
        return resp.get("obj")

    def get_pay_records(self, page_size: int = 10, max_pages: int = 6) -> list[dict[str, Any]]:
        """缴费记录。H5 用的是 {consNo, pageNo, pageSize}，返回 obj.result[]。

        服务端对 pageSize 有上限（有的租户一页只回 5 条），只看第一页会漏掉更早的
        缴费记录，卡片上就只剩最近几次。这里一路翻页翻到底，并按记录去重。
        """
        base = {"consNo": self._cons_no(), "pageNo": 1, "pageSize": page_size}
        return self._paged_records(
            "micro/bill/payMentRecord",
            [
                base,
                {**base, "pageSize": max(page_size, 24)},
                {**base, "settleFlag": ""},
                {**base, "beginTime": "", "endTime": ""},
            ],
            max_pages=max_pages,
        )

    def get_bill_records(self, page_size: int = 24, max_pages: int = 6) -> list[dict[str, Any]]:
        """费用（账单）记录 —— H5「账单查询」的列表就是它。

        每行给：``ym`` 账期、``gasAmount`` 用气量、``billAmt`` 该期账单金额、
        ``payableGasFee`` 应收（结清后是 0.00）、``settleFlag``/``settleFlagCis``
        结清标记、``id`` 逐期详情要用的 billId。

        参数按 H5 原样（不带 feeType）优先，别的变体只是兜底。
        """
        base = {"consNo": self._cons_no(), "pageNo": 1, "pageSize": page_size,
                "settleFlag": "", "beginTime": "", "endTime": ""}
        return self._paged_records(
            "micro/bill/feeRecord",
            [
                # H5「账单查询」原样参数：settleFlag 空串、不带 feeType
                base,
                {**base, "feeType": "1"},
                {**base, "settleFlag": 0, "feeType": 1},
            ],
            max_pages=max_pages,
        )

    def _paged_records(
        self,
        path: str,
        variants: list[dict[str, Any]],
        max_pages: int = 6,
    ) -> list[dict[str, Any]]:
        """按参数变体找到能出记录的写法，再翻页取全（按 totalCount / 空页停）。"""
        last_err: Exception | None = None
        for payload in variants:
            try:
                rows: list[dict[str, Any]] = []
                seen: set[str] = set()
                total: int | None = None
                for page in range(1, max_pages + 1):
                    body = {**payload, "pageNo": page}
                    resp = self._post(path, body)
                    obj = resp.get("obj")
                    if total is None and isinstance(obj, dict):
                        total = to_int(obj.get("totalCount") or obj.get("total"))
                    batch = as_record_list(obj)
                    fresh = [r for r in batch if _record_key(r) not in seen]
                    if not fresh:
                        # 服务端忽略 pageNo（每页都一样）或者已经到底，都从这里退出
                        break
                    for r in fresh:
                        seen.add(_record_key(r))
                    rows.extend(fresh)
                    if total is not None and len(rows) >= total:
                        break
                if rows:
                    _LOGGER.debug("%s 共取到 %s 条（服务端 total=%s）", path, len(rows), total)
                    return rows
            except KaiyuanGasAuthError:
                raise
            except KaiyuanGasError as err:
                last_err = err
                continue
        if last_err is not None:
            raise last_err
        return []

    def get_bill_detail(self, bill_id: Any) -> dict[str, Any]:
        """单期账单详情 —— H5「用气详情」页就调这个（``micro/bill/feeDetail``）。

        返回一个把整期说清楚的对象：``addrSecret`` 用户地址、``userName`` 用户姓名、
        ``payableGasFee`` 燃气费、``payableLateFee`` 违约金、``gasAmount`` 用气量、
        ``lastRdNum``/``thisRdNum`` 起止读数、``lastReadTime``/``thisReadTime`` 抄表时间、
        ``arreasAll`` 欠费、``balance`` 余额、``ym`` 账期、``settleFlag`` 结清标记、
        ``stepFeeData`` 该期阶梯用量。

        首页「用气地址」和「未缴费那期没有费用构成」都是缺了这一步。
        """
        try:
            resp = self._post("micro/bill/feeDetail", {"billId": bill_id})
        except KaiyuanGasAuthError:
            raise
        except KaiyuanGasError:
            # 个别租户要求带上户号，H5 只传 billId；失败就补一次
            resp = self._post(
                "micro/bill/feeDetail", {"billId": bill_id, "consNo": self._cons_no()}
            )
        obj = resp.get("obj")
        return obj if isinstance(obj, dict) else {}

    def _fetch_records(self, path: str, variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按参数变体依次尝试，返回第一个有记录的列表。"""
        last_err: Exception | None = None
        for payload in variants:
            try:
                resp = self._post(path, payload)
            except KaiyuanGasAuthError:
                raise
            except KaiyuanGasError as err:
                last_err = err
                continue
            records = as_record_list(resp.get("obj"))
            if records:
                return records
        if last_err is not None:
            raise last_err
        return []

    def get_meter_records(self, page_size: int = 24) -> list[dict[str, Any]]:
        payload = {"consNo": self._cons_no(), "pageSize": page_size}
        resp = self._post("micro/readMeter/mrInfo", payload)
        return as_record_list(resp.get("obj"))

    # ------------------------------------------------------------------ 汇总
    def fetch_all(self, known_months: set[str] | None = None) -> dict[str, Any]:
        """拉一次完整数据，供 HA Coordinator 使用。

        ``known_months``：缓存里已经有费用明细的账期，逐期详情可以跳过，避免每次
        刷新都对历史月份重复请求。
        """
        if not self.token:
            self.login()
        if not self.user_info:
            self.get_user_info()

        result: dict[str, Any] = {
            "user": self.user_info,
            "cons_no": self._cons_no(),
            "account": {},
            "nc_bill": {},
            "arrearage": None,
            "bills": [],
            "payments": [],
            "meters": [],
            "updated_at": time.time(),
        }

        self.endpoint_debug = {}
        self.last_payloads = {}
        for key, func in (
            # ncBill 放在第一个：金额、抄表日期、用气量全靠它，拿不到再退回其它接口
            ("nc_bill", self.get_nc_bill),
            ("account", self.get_account_info),
            ("arrearage", self.get_arrearage),
            ("bills", self.get_bill_records),
            ("payments", self.get_pay_records),
            ("meters", self.get_meter_records),
        ):
            try:
                value = func()
                result[key] = value
                self.endpoint_debug[key] = {"ok": True, **_shape_of(value)}
            except KaiyuanGasAuthError:
                raise
            except KaiyuanGasError as err:
                self.endpoint_debug[key] = {"ok": False, "error": str(err)}
                _LOGGER.warning("获取 %s 失败: %s", key, err)

        # 逐期账单详情：H5「用气详情」页的数据源，一次给全地址、户名、燃气费、
        # 违约金、阶梯。账单接口本身没有这些，缺了它首页「用气地址」和未缴费期的
        # 「费用构成」就都是空的。
        # 已结清的月份金额不会再变，缓存里已经有费用的月份直接跳过（known_months），
        # 只有新账期和仍未结清的最近两期才重新拉，日常刷新的请求数不会越滚越大。
        details: list[dict[str, Any]] = []
        detail_errors = 0
        skip = {m for m in (known_months or []) if m}

        def _already_known(month: str | None) -> bool:
            """缓存里这一期已经有金额了？账期与抄表月差一个月，两个都算。"""
            if not month:
                return False
            return month in skip or (prev_month(month) or "") in skip

        bills = [b for b in (result.get("bills") or []) if isinstance(b, dict)]
        always = {month_of(b) for b in bills[:2]}  # 最新两期总是重拉，未结清时金额会变
        for bill in bills:
            if len(details) >= BILL_DETAIL_LIMIT:
                break
            month = month_of(bill)
            if _already_known(month) and month not in always:
                continue
            bill_id = bill.get("id") or bill.get("billId") or bill.get("rcvblId")
            if bill_id in (None, ""):
                continue
            try:
                detail = self.get_bill_detail(bill_id)
            except KaiyuanGasAuthError:
                raise
            except KaiyuanGasError as err:
                detail_errors += 1
                _LOGGER.debug("账单详情 %s 获取失败: %s", bill_id, err)
                continue
            if detail:
                # billId 是「这条详情属于哪一期」的唯一可靠线索：detail 自带的 ym
                # 有时是划线月，按它归月会把整期挂到隔壁月份上。
                detail["billId"] = bill_id
                details.append(detail)
        result["bill_details"] = details
        self.endpoint_debug["bill_details"] = {
            "ok": bool(details),
            "count": len(details),
            "errors": detail_errors,
            "skipped_months": sorted(skip),
            **_shape_of(details),
        }

        # 出问题时点「下载诊断信息」就能看到：每个接口的入参、返回条数、字段名
        result["debug"] = {
            "endpoints": self.endpoint_debug,
            "payloads": dict(self.last_payloads),
            # 户号显示不对时，看这里：账号里所有像户号的字段都在
            "cons_candidates": cons_candidates(self.user_info)[:20],
        }
        return result


# ---------------------------------------------------------------------- 工具
def to_int(value: Any) -> int | None:
    """'12' / 12 / 12.0 -> 12（分页里的 totalCount 常是字符串）。"""
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def prev_month(month: str) -> str | None:
    """'2026-01' -> '2025-12'（账期与抄表月常差一个月，比对时两个都认）。"""
    try:
        year, mon = (int(x) for x in month.split("-")[:2])
    except (ValueError, AttributeError):
        return None
    mon -= 1
    if mon == 0:
        return f"{year - 1}-12"
    return f"{year}-{mon:02d}"


def month_of(record: dict[str, Any]) -> str | None:
    """把账期 '202610' / '2026-10' / '2026年10月' 统一成 '2026-10'。"""
    for key in ("ym", "billMonth", "feeMonth", "rcvblym", "billYm", "month"):
        raw = record.get(key)
        if raw in (None, ""):
            continue
        text = str(raw).strip()
        hit = re.match(r"^(\d{4})\s*[-/年.]\s*(\d{1,2})", text)
        if hit:
            month = int(hit.group(2))
            if 1 <= month <= 12:
                return f"{hit.group(1)}-{month:02d}"
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) >= 6:
            month = int(digits[4:6])
            if 1 <= month <= 12:
                return f"{digits[:4]}-{month:02d}"
    return None


def deep_get(data: Any, *keys: str) -> Any:
    """在嵌套结构里按 key 深度优先查找第一个非空值。"""
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if value not in (None, "", [], {}):
                return value
        for value in data.values():
            found = deep_get(value, *keys)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(data, list):
        for item in data:
            found = deep_get(item, *keys)
            if found not in (None, "", [], {}):
                return found
    return None


def first_list(data: Any) -> list[Any]:
    """找到结构里第一个非空列表。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list) and value:
                return value
        for value in data.values():
            found = first_list(value)
            if found:
                return found
    return []


# 账号对象里可能藏着好几个「号」：户号、燃气卡号、缴费号、表号。
# 户号显示不对时，把每个候选值连同它所在的路径一起列出来，比猜快得多。
CONS_KEYS = ("consNo", "cons_no", "cardNum", "accountNo", "gasCardNo", "userCode", "custNo")


def cons_candidates(data: Any, prefix: str = "") -> list[dict[str, str]]:
    """列出结构里所有像户号的字段（带路径），用于诊断。"""
    out: list[dict[str, str]] = []
    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if key in CONS_KEYS and isinstance(value, (str, int)) and str(value).strip():
                out.append({"path": path, "value": str(value).strip()})
            else:
                out.extend(cons_candidates(value, path))
    elif isinstance(data, list):
        for index, item in enumerate(data[:5]):
            out.extend(cons_candidates(item, f"{prefix}[{index}]"))
    return out


def _record_key(record: dict[str, Any]) -> str:
    """分页取回来的记录可能重复，按账期 / 缴费时间 / id 去重。"""
    for key in ("id", "rcvblId", "billId", "ym", "billMonth", "rcvblym",
                "paymentDate", "payDate", "paymentTime"):
        value = record.get(key)
        if value not in (None, ""):
            return f"{key}:{value}"
    return json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)[:200]


def _shape_of(value: Any) -> dict[str, Any]:
    """概括一个接口的返回：几条记录、记录里有哪些字段名。"""
    if isinstance(value, list):
        first = value[0] if value else None
        return {
            "count": len(value),
            "fields": list(first.keys())[:40] if isinstance(first, dict) else None,
        }
    if isinstance(value, dict):
        return {"count": 1, "fields": list(value.keys())[:40]}
    return {"count": 0 if value is None else 1, "fields": None}


def as_record_list(data: Any) -> list[dict[str, Any]]:
    """把各种分页结构统一成记录列表。"""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        # 缴费记录 / 费用记录这两个接口都是 { obj: { result: [...], currentPage, totalCount } }
        for key in ("result", "records", "rows", "list", "dataList", "resultList", "items",
                    "content", "datas", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        found = first_list(data)
        if found and isinstance(found[0], dict):
            return found
    return []
