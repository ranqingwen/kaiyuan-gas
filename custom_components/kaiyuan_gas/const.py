"""常量定义。"""

DOMAIN = "kaiyuan_gas"
MANUFACTURER = "凯源燃气"
MODEL = "微网厅"

CONF_MOBILE = "mobile"
CONF_PASSWORD = "password"
CONF_CONS_NO = "cons_no"
CONF_REFRESH_HOURS = "refresh_hours"

# 阶梯气价：年用气量分档 + 各档单价（接口不返回，由用户在配置/选项里填）
CONF_LADDER1 = "ladder1_limit"
CONF_LADDER2 = "ladder2_limit"
CONF_PRICE1 = "price1"
CONF_PRICE2 = "price2"
CONF_PRICE3 = "price3"

# 默认值取重庆中心城区居民生活用气标准（可改）
DEFAULT_LADDER1 = 500.0
DEFAULT_LADDER2 = 660.0
DEFAULT_PRICE1 = 2.196
DEFAULT_PRICE2 = 2.366
DEFAULT_PRICE3 = 2.716

# 自动刷新间隔（小时）：最低 12，最高 48，默认 24
MIN_REFRESH_HOURS = 12
MAX_REFRESH_HOURS = 48
DEFAULT_REFRESH_HOURS = 24

# 本地缓存文件名：<配置目录>/kaiyuan_gas_info_<户号>.json
CACHE_PREFIX = "kaiyuan_gas_info_"
CACHE_SUFFIX = ".json"
