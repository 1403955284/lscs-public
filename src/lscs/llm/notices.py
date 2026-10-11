"""调度器发给显示端的固定提示。放在单独的轻量模块里，悬浮面板不必导入调度器（及 openai）
就能认出"AI 已被关闭"的提示，在之后的回合常驻显示。"""

AUTH_FAILURE_LIMIT = 3
AUTH_DISABLED = f"API Key 连续 {AUTH_FAILURE_LIMIT} 次无效，已关闭 AI"
BUDGET_EXHAUSTED = "本局 AI 预算已用完，之后只显示规则建议"
TIMEOUT = "AI 超时"  # 提供商和调度器超时时 AIResult.error 的文字
