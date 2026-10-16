"""悬浮面板、两个标签和对手阵容卡图窗口共用的颜色和常量（单独放在这里，标签和卡图窗口不用
反过来 import window）。"""

BG = "#1e1e24"
MUTED = "#8c8c96"  # 页眉、提示、状态
OFFSCREEN = "1x1+-32000+-32000"  # 第一次显示时放在屏幕外，免得在左上角闪一下
TITLE = "#d4d4dc"  # 没有"下一步"时的标题（战斗中、对局结束）
RULES_TAG = "#e6b450"  # 金黄
AI_TAG = "#4fc9e0"  # 青色
OPP_TAG = "#c9a0dc"  # 淡紫
REVIEW_TAG = "#9fd3a0"  # 淡绿
CORE_TAG = "#ff8fc2"  # 粉红：商店里的核心卡（"核心"标签；和金、青、淡紫、淡绿、橙都分得开）
TEXT = "#f0f0f4"
DIM = "#b4b4bc"  # 次要文字：比 MUTED 亮一点，仍然退后
WARN = "#ff9f43"  # 橙色
DIVIDER = "#3a3a46"  # 详细模式的块间分隔线（1 像素）
NOW_BG = "#2a2a34"  # 详细模式"现在"块的底色
# 对手阵容卡图窗口（对手阵容卡图窗口设计文档第 4.4 节）
HEALTH_RING = "#e5534b"  # 生命徽章的外圈（攻击徽章是金色 RULES_TAG）
PORTRAIT_RING = "#5a5a66"  # 普通随从的椭圆外圈（金色随从是 RULES_TAG）
SHIELD_RING = "#f5f0b0"  # 圣盾：椭圆外面再套一圈浅黄
POISON_MARK = "#63c76a"  # "剧""毒"字徽章
REBORN_MARK = "#e0b0ff"  # "复"字徽章
WINDFURY_MARK = "#7fc4ff"  # "风"字徽章
