# lscs — 酒馆战棋 AI 助手

只读取炉石传说的日志文件（Power.log），不读内存、不修改游戏、不自动操作。

**玩家请看 [使用说明.md](使用说明.md)**：第一次准备、每次怎么用、怎么看懂建议、常见问题都在里面。

## 快速开始（Windows）

```powershell
uv run lscs fix-log-config      # 只需一次；看到"已写入"后完全重启炉石
Copy-Item config.example.toml config.toml; notepad config.toml   # 只需一次；填 DeepSeek Key
uv run lscs overlay-check       # 只需一次；先打开炉石，检查悬浮面板
uv run lscs highlight-test      # 只需一次；在炉石上画出招募阶段金色框的位置，截图发给开发者校准
uv run lscs watch               # 每次打游戏前运行，PowerShell 不要关；Ctrl+C 退出
```

## 常用命令

Windows 上双击项目文件夹里的 `启动.bat` 等于 `uv run lscs watch`，双击 `更新.bat` 等于 `git pull`
（不是 git 下载的目录会提示重新下载 ZIP）。

| 命令 | 作用 |
|---|---|
| `uv run lscs fix-log-config` | 检查并修复 log.config |
| `uv run lscs find-logs` | 列出 Power.log |
| `uv run lscs watch` | 实时监听，边玩边显示时间线、规则建议和 AI 建议（在 Windows 上用），同时在游戏上显示悬浮面板；`--no-ai` 只显示规则建议，`--no-overlay` 不开悬浮面板 |
| `uv run lscs replay <文件> --dump [--names]` | 回放录像并打印时间线（`--speed 10` 按 10 倍速回放） |
| `uv run lscs replay <文件> --advice [--names]` | 回放时附带每回合的规则建议（不调用大模型） |
| `uv run lscs replay <文件> --ai` | 回放时真的调用大模型（花费 token，用来检查 AI 建议） |
| `uv run lscs replay <文件> --overlay --speed 20` | 回放时打开悬浮面板预览（不开炉石、在 Mac 上也能看） |
| `uv run lscs review <文件> [--ai] [--save-history]` | 给录像出对局复盘（只打复盘，不打时间线和每回合的建议；`--ai` 也让 AI 写，会重放整局的 AI 建议；`--save-history` 记进最近几局的统计） |
| `uv run lscs review --habits` | 看 `reviews/history.jsonl` 里最近几局的名次和常见的提醒 |
| `uv run lscs overlay-check` | 逐项检查悬浮面板能否贴靠炉石、穿透鼠标（在 Windows 上先打开炉石再运行） |
| `uv run lscs highlight-test` | 在炉石画面上画出画面高亮的位置（默认只画商店、场面和四个按钮），截图核对位置；`--seconds 60` 显示更久，`--rows choice`（`choice4`、`hero`……）画别的排；不是 Windows 时开一个预览窗口 |

`watch` 总是显示中文卡名；`replay` 加 `--names` 才显示。第一次使用会从 HearthstoneJSON 下载约 10MB 的卡牌数据。

## 配置 AI 与悬浮面板

详见 [使用说明.md](使用说明.md) 的"填写 DeepSeek Key"和"常用设置"。实现要点：

- AI 分四路调用，每局合计最多 `max_calls_per_game` 次（默认 100；凯瑞尔这种每回合都弹选项的英雄一局大约 60 次）：
  - 快速建议（不开思考，2~3 秒）：每个招募回合开始时一次，每次刷新后（商店稳定 1 秒）再一次；
  - 深度分析（`deep_thinking` 开思考，约 20 秒）：只在招募回合开始时和快速建议同时发出，`deep_analysis = false` 关闭；
  - 选英雄（参数同快速建议）：出现一组备选英雄后等 0.5 秒调用一次，`hero_advice = false` 关闭；
  - 选项（参数同快速建议）：游戏弹出"选一个"（`GameState.DebugPrintEntityChoices`：三连发现、黑暗之赐、饰品、
    技能升级）时立即调用一次，选完、换组、进战斗就作废，`choice_advice = false` 关闭。回合开始的技能升级和饰品
    在 `MAIN_ACTION` 之前弹出，所以这一路不等招募开始。
  `watch` 启动时从头读当前的 Power.log，但只为读完后的当前回合（和还没选完的那组选项）调用（每路最多一次）。
  每次调用的提示词和回答记录在 `recordings/llm-日期.jsonl`（`lane` 是 `fast`、`deep`、`hero`、`choice`，
  对局复盘是 `review`），不包含 Key，也不进仓库。
- AI 的回答解析不了、或方案过不了校验器（钱不够、场面放不下、编号不存在……）时，把校验器的原因发回去改正一次：
  多轮对话（原系统提示 + 原局面消息 + 模型自己的回答 + 改正要求），前缀和原请求相同，能命中 DeepSeek 的前缀缓存；
  每个请求最多一次，占一次调用；深度分析改正时不开思考；校验器仍是唯一标准，不从不合法的方案里截取前几步。
  `repair_invalid = false` 关闭。改正对话里的调用记为 `repair=true`（`user` 只记改正要求；改正的回答为空时的重试
  也算，所以按 `request_id` 分组统计更准）。
- 局面消息开头附本局卡池（本局种族和中立随从、酒馆法术），接着是所有对手的摘要（名次、血量、酒馆等级、威胁、
  上次见到的阵容；下一个对手列完整阵容，`all_opponents = false` 只写下一个对手）：调度器每回合第一次生成后整回合
  复用（对手在招募中途升本、加护甲也不改），同一回合内不变，放在回合内容之前，和卡池块一起命中缓存；跨回合的
  缓存前缀（系统提示 + 卡池块）不变。末尾附上次的判断（方向、要找的牌、分析）；不附规则建议。
- "记牌"是查过卡牌库的卡池估计（`advisor/pool.py`）：每级份数（`strategy/current/pool.toml`）减去我方场面、
  手牌和还没淘汰的对手上次的阵容（金色算 3，旧式 `TB_BaconUps_*` 按 `battlegroundsNormalDbfId` 映射），
  只算卡池随从；终端、面板和局面消息里给对子和要找的牌写"还剩约 N 张"。没有卡牌数据时都不显示。
- 对手一览（`advisor/opponents.py`）：终端每回合开始打一块 `👥`，面板招募时最下面两行（▶ 下一个、★ 最强威胁）、
  战斗中列出全部（`[overlay] opponents = false` 关闭）。对手的阵容只有交过手才看得到。
- 对局复盘（`review/`，`[review]`）：`RecordBuilder` 按相邻状态快照的差异认出每回合的买、卖、打出、刷新、升本、
  冻结、技能、三连和选项（不靠 BLOCK_START，两份真实日志按真值核对过），战斗结果在下一回合招募开始时结算；
  `flags.py` 按确定性的规则算提醒、挑 2~3 个关键时刻；`GameReviewer.tap` 挂进 `DisplayGroup` 记下本局显示过的
  建议、比较做了几个。对局结束后的下一次 `tick` 出规则版（终端 `📝`、面板"复盘"区、`reviews/日期_时间_英雄_第N名.md`、
  `reviews/history.jsonl`），有 AI 时再在工作线程里调一次（单独的系统提示，深度分析的思考和超时，`max_tokens`
  4000），结果经队列由 `tick` 交付；AI 写的回合和卡名都要核对，不合格改正一次（`repair_invalid`），仍不行就用
  规则版；长度放宽到提示词要求的约 1.5 倍，超出时在标点处截断。不占 `max_calls_per_game`，调度器关掉 AI
  （Key 无效）时不调。`watch` 打完的对局记进 history（同一天指纹相同的局认作已经复盘过，`watch` 重启时不重复），
  `replay` 和 `lscs review` 回放录像时默认不记。
  `reviews/` 不进仓库，文件里没有玩家名字。
- 提前显示战斗结果（`[combat_result]`，默认开）：GameState 日志在战斗一开始就把整场战斗写完了，比画面动画早
  20~70 秒。追踪器在 `COMBAT_STARTED` 记下双方有效血量（我方英雄、排行榜上的对手英雄），游戏 `STEP` 变成
  `MAIN_CLEANUP`（我被淘汰那场是 `FINAL_WRAPUP`）时再读一遍，发 `COMBAT_RESULT`、快照里放 `last_combat`：
  掉血是差值，赢没赢看我方玩家实体的 `BACON_WON_LAST_COMBAT`（或对手掉了血），没赢时掉了血是输、没掉是平；
  残影、淘汰单独标出（两份真实日志 29 场和复盘一致）。这是真实结果不是预测（不模拟战斗）。终端时间线打
  `⚔ 本场已定：…` 一行（`replay` 总是打）；画面高亮窗口在棋盘顶部正中画一条横幅（`overlay/layout.banner_box`，
  内容区顶部 1.2%、字号 2.6% H，不碰对手头像），留到下一回合招募真正开始（`RECRUIT_STARTED`：日志里阶段早就
  换成下一回合的招募了），对局结束那场留 20 秒；面板不显示。`[highlight] enabled = false` 时横幅照画，
  `--no-overlay` 时只有终端。关掉时时间线的"战斗结束"那一行也不写血量（那时已经是打完以后的）；面板的对手
  血量、名次本来就跟着日志走，不受这个开关管。`highlight-test` 总是画一条样例横幅，截图时一起核对位置。
- 站位建议（`advisor/positioning.py`，`[positioning]`）是纯规则，不占 AI 调用：规则方案里的买、卖都做完后，按卡牌数据
  （`CardInfo.mechanics` 和说明）把战斗型亡语、进击放最左，复仇放最右，下一个对手上次的阵容里有劈砍、我方恰好一个嘲讽时
  把嘲讽放最左；一回合最多 2 步，白板从不被点名。终端 `🧭`、面板规则区一行，方案做完后是"下一步"（高亮徽章"拖"）。
  拖动随从在日志里是 `MOVE_MINION` 块里的 `ZONE_POSITION` 变化，状态层发 `BOARD_CHANGED` 后重算，拖完自动消失。
  系统提示第 5 条告诉 AI 站位由程序提示、不要写站位；没有卡牌数据时不提示。
- 本版本统计（`stats/`，`[stats]`，**默认关闭**）：来自 Firestone 的公开静态 JSON（`static.zerotoheroes.com/api/bgs/`
  下的英雄、饰品、随从三个文件），非官方、没有正式授权，公开版默认不开，`enabled = true` 才会连 Firestone。
  `stats/fetch.py` 按卡牌数据的做法下载（带 `User-Agent`、处理 gzip、校验顶层键），缓存在卡牌数据的缓存目录、
  24 小时内不重下，失败时用旧缓存；`StatsStore` 在 `watch` 里后台准备（从不阻塞，没准备好之前的选英雄按护甲），
  `replay` / `review` 同步。只做相对比较：英雄按平均名次在样本够的英雄里排名，四个备选都有可靠数据时规则改按
  平均名次推荐；饰品在同一组里比，▲ 标最好的（不是推荐，"下一步"照旧跟 AI）；随从按"打出当回合"比较
  （回合修正，避开幸存者偏差），只给 AI（招募消息里卡池块之后的一块，按本局种族缓存），不显示。调度器第一次
  取到统计之后整局复用（选英雄时还没准备好的，之后的消息照样用上）。凡是出现数字的地方都署名"数据来自 Firestone"（面板写"Firestone"）。
- `thinking`（快速建议）请保持 `"disabled"`，思考交给深度分析。
- 攻略要点在 `strategy/current/guide.md`，会原样发给 AI。
- 英雄技能能不能用以日志里的选项列表（`GameState.DebugPrintOptions`）为准，选项比招募开始晚几十毫秒才到，
  会等它一起生成招募开始的局面。规则只安排 0 费、不需要目标的技能；其余的交给 AI，提示词里写明费用、状态和可选目标。
- 悬浮面板用 tkinter 实现，Windows 上通过 Win32 贴在炉石窗口上，并设置鼠标穿透、不抢焦点、不显示在任务栏；
  失败时退回成屏幕右上角的普通置顶窗口，无法启动时只用终端。`uv run lscs replay <录像> --overlay --speed 20`
  可以在任何平台预览面板。面板最上面是"下一步"（面板上显示的方案的第一步，或弹出选项时 AI 选的那个）。
- 面板默认贴在炉石客户区右下角（`position = "right"`，左下角 / 顶部可选），向上长，不挡"开始战斗"按钮。
  面板右上角的"≡ 拖动"标签是单独的不穿透、不抢焦点的窗口（`overlay/grip.py`，面板本身照样整块穿透），
  按住它拖动面板，位置按客户区比例存在项目文件夹的 `overlay-position.json`（不进仓库，不改 `config.toml`），
  换分辨率照样跟着走；双击标签或删掉文件回到预设。Mac 预览也能拖，只是不保存。
- 画面高亮（`[highlight]`）：Windows 上贴靠炉石时，再开一个和面板同一个 Tk 的置顶窗口盖住炉石客户区，
  背景用 `-transparentcolor` 色键（完全透明），再加上穿透、不抢焦点的扩展样式（不调 `SetLayeredWindowAttributes`，
  否则会盖掉色键），只给"下一步"画四角括号（浅金线 + 深色衬底，`style = "frame"` 改成整圈细线框）和右上角的
  步骤编号，换到新目标时用 `after` 定时器"呼吸"约 1.2 秒（`pulse`）；几何和呼吸的状态机在 `overlay/style.py`
  （纯函数）。面板隐藏时清空，出错只关高亮。位置由
  `overlay/layout.py` 的布局模型按客户区里居中的 16:9 内容区按比例算出；商店、场面和四个按钮按两张招募阶段截图
  （2560×1440、2559×1369）校准过，选项和选英雄还是估计的；按钮半径填 0 表示没校准、不画。用 `highlight-test`
  的截图校准后改默认值和 `config.example.toml`。

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
