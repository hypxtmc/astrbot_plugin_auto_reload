# 更新记录

## 1.1.0（2026-09-19）

MRO 旧血自检。修的是一个骗了我们一整天的假绿。

病形是这样：`@llm_tool` 的壳函数挂在 `main.py` 上，函数体就一句 `super().parallel_handoff(...)`，真正执行的方法体在 `dispatch.py` 的 `DispatchMixin` 里。三条绑定判据（`_belongs` / `_is_orphan` / `_stale`）全都只认壳函数所在的 `main.py`——壳永远是新鲜的，判据必然全绿。

这不是漏检，是结构性的。回执那句「无孤儿」是四个列表全空时的固定串。

那天下午给复调的 `_emit_results` 补了个形参，热重载回执报「✅ 完全生效」，调工具仍然报 `name 'enable_segmented_forward' is not defined`。快路径、全量都试过，`.pyc` 也确认重编译了（17:35:14 晚于源文件 17:34:59）。重启才正常。

两道换血探针同时瞎了：

- `_tag_freshness` 认的是类属性 `RUNTIME_BUILD_TAG`，而插件给的是模块级常量，逐 MRO 类取到 0 个，直接返回 `ok=None` 跳过
- `_runtime_self_stale` 只比方法首行号，而那次改动是给签名加参数，不位移任何行号，零 stale，判「完全生效」

改动四处：

- 新增 `_audit_mro_stale()`：扫 MRO 上 `__module__` 以 `data.plugins.` 开头的基类，断言 `sys.modules[那个模块]` 里的同名属性 `is` 这个基类。不等即旧血；模块已不在 `sys.modules` 时同样判旧血
- `_audit_and_rebind` 调它：检出直接返回「⚠️ 假绿 + 必须重启」，不再让「无孤儿」骗人
- `_grade_reload_effect` 调它：两条探针没判死时补一刀，检出即降级为「⚠️ 部分生效」并点名是哪个 Mixin
- `_verify_quick_swap` 补两个洞：P2 类血同源从单类扩到整个 MRO；P4 原来「旧模块已 purge 出 `sys.modules` 就 `continue`」，改成模块消失也算 stale

负向测试过了：热重载 MRO 正常的插件仍报「完全生效」，零误报。

正向场景（热重载后 MRO 真混入旧 Mixin）正常换血构造不出来，等真实复现时再验。

## 1.0.5（2026-09-17）

快路径探针纠错版。1.0.4 落地的「定向快路径」实测从未真正放行过。

三判据里的 P2「类血同源」写成 `getattr(cls, "__globals__", None)`，而 `__globals__` 是**函数**的属性，类根本没有它（CPython 实测 `hasattr(SomeClass, "__globals__") is False`）。取到默认值 `None` 之后 `None is not mod.__dict__` 恒为真，判据对任何插件、任何时刻都判不过——每次重载白跑 0.16s 再升级 18s 全量。

本版把 P2 改成双判据：一是在线模块里按名字找到的必须是同一个类对象；二是类体里的函数 `__globals__` 必须就是该模块 `__dict__`（取得到才查，类体无函数不误伤）。

顺带修掉「插件改不动自己」——自载自身此前同样被这杆假秤判死、升级全量，而全量不做 purge，自身模块永不重执行，改动根本不被读。探针修好后自载自身 0.17s 直通过。

另有三笔：修复子代理人格热更诊断里 f-string 误用海象运算符导致的 `except` 分支 `NameError`（`f"ERR {_type_err := ...}"` 里的 `:` 会被解析成格式说明符起点）；`star_map` 摘表改按值同一性删，不再依赖 key 形态；版本号对齐（`@register` 此前停在 `1.0.3`，与 metadata 的 `1.0.4` 不一致）。

实测：`skill_cache_guard` 0.13s / 0.06s、`auto_reload` 自载 0.17s，三判据全绿加绑定自检零孤儿。

## 1.0.4（2026-09-16）

假绿防护上线。

动机很直白：热重载如果只凭「模块缓存清理 + 绑定自检」判定成功，可能在 `sys.modules` / `star_map` 里留下旧类对象——日志全绿，跑的还是旧代码。这类情况此前会谎报「✅ 完全生效」。对公开插件来说，这是在害人。

本版把判定升级为五层：

1. 子模块可选打 `RUNTIME_BUILD_TAG` 换血指纹（类属性与磁盘硬比对）
2. 运行时类方法 `co_firstlineno` 与磁盘 def 首行比对（采样上限 60）
3. stale 命中时回执降级为「⚠️ 部分生效——运行时类方法仍是旧版本」
4. 点名 stale 方法清单，内存与磁盘行号都给
5. 明确「🤖 请通知用户：需手动重启 AstrBot 后再重载一次」

真机验证两轮 reload 均当场咬中（auto_reload 与 parallel_handoff 各 5 处 stale 被点名）。

另有 reload 锁死全量路径（`reload(None)`）、`__self__` 一致性 rebind 等修复。

## 1.0.3（2026-09-15）

中间修复版。此前 metadata 里已升，没在 README 补录，这里一并补记账。

## 1.0.2（2026-09-13）

新增 `enable_subagent_tools` 配置项，子代理四工具可整体关闭。README 增加兼容性声明。

## 1.0.1（2026-09-13）

子代理三件套：`subagent_health` 四层体检、`subagent_snapshot` 快照与回滚、`subagent_test` 试音台。修复 `update_subagent` 的 list 动作误要求 name。

## 1.0.0（2026-09-13）

热重载完全体：生效度评分、必须重启清单、平台实例换手（真机 0.99s 验证）、陈旧任务回收（零误杀验证）。新增 2 项热重载配置。

## 0.1.1

插件管理基础功能。
