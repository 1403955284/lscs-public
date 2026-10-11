# lscs — 酒馆战棋 AI 助手

只读取炉石传说的日志文件（Power.log），不读内存、不修改游戏、不自动操作。

**玩家请看 [使用说明.md](使用说明.md)**：第一次准备、每次怎么用、怎么看懂建议、常见问题都在里面。

## 快速开始（Windows）

```powershell
uv run lscs fix-log-config      # 只需一次；看到"已写入"后完全重启炉石
Copy-Item config.example.toml config.toml; notepad config.toml   # 只需一次；填 DeepSeek Key
uv run lscs overlay-check       # 只需一次；先打开炉石，检查悬浮面板
uv run lscs watch               # 每次打游戏前运行，PowerShell 不要关；Ctrl+C 退出
```

## 常用命令

| 命令 | 作用 |
|---|---|
| `uv run lscs fix-log-config` | 检查并修复 log.config |
| `uv run lscs find-logs` | 列出 Power.log |
| `uv run lscs watch` | 实时监听，边玩边显示时间线、规则建议和 AI 建议（在 Windows 上用），同时在游戏上显示悬浮面板；`--no-ai` 只显示规则建议，`--no-overlay` 不开悬浮面板 |
| `uv run lscs replay <文件> --dump [--names]` | 回放录像并打印时间线（`--speed 10` 按 10 倍速回放） |
| `uv run lscs replay <文件> --advice [--names]` | 回放时附带每回合的规则建议（不调用大模型） |
| `uv run lscs replay <文件> --ai` | 回放时真的调用大模型（花费 token，用来检查 AI 建议） |
| `uv run lscs replay <文件> --overlay --speed 20` | 回放时打开悬浮面板预览（不开炉石、在 Mac 上也能看） |
| `uv run lscs overlay-check` | 逐项检查悬浮面板能否贴靠炉石、穿透鼠标（在 Windows 上先打开炉石再运行） |

`watch` 总是显示中文卡名；`replay` 加 `--names` 才显示。第一次使用会从 HearthstoneJSON 下载约 10MB 的卡牌数据。

## 配置 AI 与悬浮面板

详见 [使用说明.md](使用说明.md) 的"填写 DeepSeek Key"和"常用设置"。实现要点：

- AI 分两路调用，每局合计最多 `max_calls_per_game` 次（默认 80）：
  - 快速建议（不开思考，2~3 秒）：每个招募回合开始时一次，每次刷新后（商店稳定 1 秒）再一次；
  - 深度分析（`deep_thinking` 开思考，约 20 秒）：只在招募回合开始时和快速建议同时发出，`deep_analysis = false` 关闭。
  `watch` 启动时从头读当前的 Power.log，但只为读完后的当前回合调用（每路最多一次）。每次调用的提示词和回答记录在
  `recordings/llm-日期.jsonl`（`lane` 是 `fast` 或 `deep`），不包含 Key，也不进仓库。
- 局面消息开头附本局卡池（本局种族和中立随从、酒馆法术），末尾附上次的判断（方向、要找的牌、分析）；不附规则建议。
- `thinking`（快速建议）请保持 `"disabled"`，思考交给深度分析。
- 攻略要点在 `strategy/current/guide.md`，会原样发给 AI。
- 英雄技能能不能用以日志里的选项列表（`GameState.DebugPrintOptions`）为准，选项比招募开始晚几十毫秒才到，
  会等它一起生成招募开始的局面。规则只安排 0 费、不需要目标的技能；其余的交给 AI，提示词里写明费用、状态和可选目标。
- 悬浮面板用 tkinter 实现，Windows 上通过 Win32 贴在炉石窗口上，并设置鼠标穿透、不抢焦点、不显示在任务栏；
  失败时退回成屏幕右上角的普通置顶窗口，无法启动时只用终端。`uv run lscs replay <录像> --overlay --speed 20`
  可以在任何平台预览面板。

## 添加录像（在 Mac 上）

录像里有你和对手的战网昵称，只放在自己的私有仓库里，不要提交到公开仓库。

1. 压缩并放进测试目录（文件名用"日期_简短描述"）：
   ```bash
   gzip -9 -c ~/Downloads/Power.log > tests/fixtures/recordings/2026-10-12_rank1.log.gz
   ```
2. 打印时间线并**人工核对**：英雄、最终名次、你记得的几个回合的商店和阵容。
   ```bash
   uv run lscs replay tests/fixtures/recordings/2026-10-12_rank1.log.gz --dump --names
   ```
3. 核对无误后保存快照（不带 `--names`，避免受卡牌数据更新的影响）：
   ```bash
   uv run lscs replay tests/fixtures/recordings/2026-10-12_rank1.log.gz --dump > tests/fixtures/recordings/2026-10-12_rank1.timeline.txt
   ```
4. 运行 `uv run pytest`，通过后提交。

## 开发

```bash
uv sync
uv run pytest
uv run ruff check src tests && uv run ruff format --check src tests
uv run pytest -m gui   # 悬浮面板的图形界面冒烟测试（会真的打开窗口，需要显示器；默认不运行）
```
