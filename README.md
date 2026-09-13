# 插件手术台

在聊天中管理 AstrBot 插件，无需登录管理后台。v1.0.0 起自带深度热重载能力：不只换代码，还能换掉在跑的平台实例、回收陈旧后台任务，并在回执里诚实告诉你"这次到底生效到哪一层"。

## 功能

| 指令 | 说明 |
|------|------|
| `/plugin help` | 查看帮助 |
| `/plugin list` | 查看所有插件（按启用/禁用/保留分组） |
| `/plugin info <名称>` | 查看插件详情 |
| `/plugin enable <名称>` | 启用插件 |
| `/plugin disable <名称>` | 禁用插件 |
| `/plugin reload <名称>` | 重载插件 |
| `/plugin install <GitHub URL>` | 从仓库安装插件（需开启配置） |
| `/plugin uninstall <名称>` | 卸载插件（需开启配置） |
| `/plugin update <名称>` | 从仓库更新插件 |

另有 LLM 工具：`hot_reload_plugin`、`plugin_list`、`update_subagent`（编制管理）、`subagent_health`（体检）、`subagent_snapshot`（快照）、`subagent_test`（试音）。后四个子代理工具受配置项 `enable_subagent_tools` 控制——不用子代理功能的实例可在配置页关闭，避免工具列表冗余。

## 子代理三件套（v1.0.1）

### 🩺 subagent_health 运行时体检

对照四层状态抓异常：编制表（config）↔ 运行时实例（handoffs）↔ 人格源（persona DB）↔ provider 可用性。专治：在编但未挂载、人格缺失导致的串台回落、改人格后没重载（运行时还跑旧 instructions）、provider 失效。

### 📸 subagent_snapshot 编制快照

`save` 一次性备份 orchestrator 配置段 + 全部相关人格（prompt/begin_dialogs/tools）；`list` 列历史；`rollback` 一键恢复（人格存在才恢复，不存在跳过并明示，不做半恢复）。存于 `data/plugin_data/astrbot_plugin_manager/subagent_snapshots/`，纯本地。

### 🎤 subagent_test 试音台

用子代理的人格与 provider 直接发一条测试消息，拿回原始回复（不转发给用户）。调人格时改一段试一段，session 隔离不留上下文，60s 超时。

## 热重载能力（v1.0.0）

`hot_reload_plugin` 每次重载都会返回**自证回执**，逐层报告生效情况：

```
✅ 热重载成功 · <插件名>
耗时 0.34s｜版本 2.10.0 → 2.10.0｜🟢 已加载｜模块缓存清理 15 个
🔗 绑定自检：全部指向当前在线模块，无孤儿
🧬 长活对象自检：无陈旧引用（后台任务/平台适配器均指向当前在线模块）
🧭 生效度：✓ 完全生效（模块/绑定/长活对象均指向当前代码）
```

### 🧭 生效度评分（一期）

四档结论：`✓ 完全生效` / `◐ 部分生效`（注册表已换血、长活链路仍持旧代码）/ `⚠️ 底层改动没进内存` / `❌ 未生效`。不再需要"重载完发条消息试试"。

### 🚨 必须重启清单（一期）

自动检测 `astrbot/core` 下"源文件比 .pyc 新"的模块——那说明改动在进程导入之后才落盘，内存里没有它，任何插件重载都救不了，回执会直接点名让你重启。判据不依赖系统时钟（实测本机 `/proc/uptime` 不可信，误报比不报更坏）。

### 🔄 平台实例换手（二期，默认关）

平台适配器型插件（QQ/Telegram 等）重载后，跑着的旧实例仍持旧模块代码——清缓存清不掉它。开启换手后：重载完成 → 停旧实例（连它的任务一起收走）→ 按新代码重挂 → 自检确认。实测断线重连约 **1 秒**。

三道安全闸：拿不到配置跳过、`enable=false` 跳过、别家实例零动作。

### 🧹 陈旧任务回收（三期，默认关）

插件自己 `create_task` 起的定时器/巡检/重试循环没有登记表，重载后旧代码会一直跑。开启回收后：只取消"归属本插件 **且** 仍持旧模块代码"的任务——新任务、别家插件、当前调用者自身一律不碰；吞掉 `CancelledError` 的顽固任务只记录不强杀。

### 必须重启的场景（边界声明）

- 改动 `astrbot/core/**`（回执会主动报 🚨）
- 平台端口绑定 / webhook 路径变更
- 全局 `config.yaml` 结构变更、数据库 schema 迁移

## 配置说明

在 AstrBot 插件配置页面找到「插件手术台」：

| 配置项 | 默认 | 说明 |
|--------|------|------|
| allow_install | 关 | 允许聊天指令安装插件 |
| allow_uninstall | 关 | 允许聊天指令卸载插件 |
| hot_reload_handover_default | 关 | 每次热重载自动执行平台实例换手（工具传参可按次覆盖） |
| hot_reload_reclaim_default | 关 | 每次热重载自动回收陈旧后台任务（工具传参可按次覆盖） |
| enable_subagent_tools | 开 | 启用 update_subagent / subagent_health / subagent_snapshot / subagent_test 四个子代理 LLM 工具 |
| owner_uids | 空 | 免 ADMIN 放行的用户 ID 列表（管理类 LLM 工具的调用白名单） |

> 设计原则：换手与回收默认关闭。先看回执 🧬 行报不报陈旧，确认需要换血再开——日常重载零代价。

## 安装

下载本插件文件夹，放入 AstrBot 的 `data/plugins/` 目录，重启 AstrBot。

或从 AstrBot 插件市场搜索「插件手术台」一键安装。

## 兼容性声明

本插件适配 AstrBot **4.16+**。指令类功能（`/plugin list` 等）仅依赖公开接口，兼容性最好。

深度热重载与子代理三件套依赖 AstrBot 内部模块（`astrbot.core.star.updater`、`astrbot.core.persona_mgr`、`astrbot.core.subagent_orchestrator` 等）——这些内部 API **未承诺稳定**，官方版本升级可能导致相应功能失效。遇到问题时请先核对你的 AstrBot 版本，并在 issue 中附上回执与日志。

## 注意事项

- 卸载插件会删除插件目录，请谨慎操作
- `install` 会执行 `pip install`，请确认仓库来源可信后再开启
- 本插件无法卸载自身（防止把自己搞没）

## 版本历史

- **1.0.2**（2026-09-13）：新增 enable_subagent_tools 配置项（子代理四工具可整体关闭）；README 增加兼容性声明
- **1.0.1**（2026-09-13）：子代理三件套——subagent_health 四层体检、subagent_snapshot 快照/回滚、subagent_test 试音台；修复 update_subagent list 动作误要求 name
- **1.0.0**（2026-09-13）：热重载完全体——生效度评分、必须重启清单、平台实例换手（真机 0.99s 验证）、陈旧任务回收（零误杀验证）、新增 2 项热重载配置
- 0.1.1：插件管理基础功能

## 作者

hypxtmc

基于早期开源插件的插件管理骨架发展而来，热重载深度能力（生效度评分、必须重启清单、平台实例换手、陈旧任务回收）为完全重新设计与实现。本插件为本地维护版本，不设远程更新源。

## 致谢

感谢 [Neko Ai](https://github.com/NekoAiDev) 编写的早期版本插件管理插件——本项目的插件管理基础骨架（列表/启停/安装/卸载/更新的指令框架）源自其工作。热重载深度能力在其骨架之上完全重新设计与实现。

正如 LICENSE（MIT）所载，原作者的版权声明予以保留。站在前人肩膀上，不忘来路。
