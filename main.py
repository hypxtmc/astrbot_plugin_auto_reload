import time

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig



def _is_owner(event, owner_uids=None) -> bool:
    """所有者放行（即使非 ADMIN）：uid 列表由插件配置项 owner_uids 提供"""
    try:
        uid = str(event.get_sender_id() or "")
        return uid in (owner_uids or ())
    except Exception:
        return False


# 重载回执：命中即进回执的关键日志词（避免刷屏，只留能自证的行）
_RELOAD_LOG_HINTS = (
    "loading plugin", "removed handler", "reload", "已加载", "注册",
    "error", "traceback", "failed", "失败", "no module", "importerror", "syntaxerror",
)


class _ReloadLogCapture:
    """捕获一次热重载窗口内的日志。

    AstrBot 的 astrbot.* logger 全是 propagate=False（core/log.py GetLogger），
    挂 root handler 一条也收不到；所有记录经 intercept handler 汇入 loguru，
    所以改挂 loguru sink（实测：挂 root 时回执显示「日志 0 行」）。
    """

    def __init__(self):
        self.lines = []  # [(levelname, message)]
        self._sink_id = None

    def start(self):
        try:
            from loguru import logger as _loguru

            self._sink_id = _loguru.add(
                self._emit,
                level="INFO",
                format="{level}|{message}",
                colorize=False,
            )
        except Exception:
            self._sink_id = None

    def stop(self):
        if self._sink_id is None:
            return
        try:
            from loguru import logger as _loguru

            _loguru.remove(self._sink_id)
        except Exception:
            pass
        self._sink_id = None

    def _emit(self, message):
        try:
            level, _, msg = str(message).partition("|")
            self.lines.append((level.strip(), msg.strip()))
        except Exception:
            pass


def _normalize_plugin_name(s) -> str:
    """归一化插件名：去 astrbot_plugin_ 前缀、统一分隔符、忽略大小写"""
    s = str(s or "").strip().lower()
    for prefix in ("astrbot_plugin_", "astrbot_", "plugin_"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return s.replace("-", "_").replace(" ", "_")


def _plugin_name_hint(query, registry, reason: str = "") -> str:
    """解析失败时直接给出内部名候选 + 重载提示，免去二次试错"""
    cands = [m for m in registry if not getattr(m, "reserved", False)]
    norm = _normalize_plugin_name(query)
    near = [
        m for m in cands
        if norm and (
            norm in _normalize_plugin_name(m.name)
            or _normalize_plugin_name(m.name) in norm
        )
    ]
    picked = near or cands
    out = []
    if reason:
        out.append(reason)
    out.append("重载请用插件内部名（内部名 ≠ 目录名/展示名）：")
    out.append("最接近的候选：" if near else "当前可用插件：")
    for m in picked[:12]:
        out.append(f"  · {m.name}　（展示名：{m.display_name or '—'}）")
    if len(picked) > 12:
        out.append(f"  …共 {len(picked)} 个，完整列表调 plugin_list")
    return "\n".join(out)


@register(
    "astrbot_plugin_auto_reload",
    "hypxtmc",
    "在聊天中管理 AstrBot 插件：查看列表、启停、重载（含平台换手/陈旧任务回收/生效度评分）、安装、卸载、更新",
    "1.0.3-m3fix",
    "",
)
class PluginManager(Star):
    """自动重载 - 为 AI agent 设计的插件管理与自主热重载，人类也可用聊天指令操作"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    # ─────────────────────────────────────────
    # 工具方法
    # ─────────────────────────────────────────

    def _get_pm(self):
        """获取 PluginManager 实例"""
        pm = self.context._star_manager
        if pm is None:
            raise RuntimeError("PluginManager 未初始化，无法执行操作")
        return pm

    def _format_star(self, star) -> str:
        """格式化单个插件信息"""
        status = "✅ 启用" if star.activated else "❌ 禁用"
        reserved = " 🔒保留" if getattr(star, "reserved", False) else ""
        version = f" v{star.version}" if getattr(star, "version", None) else ""
        author = f" · {star.author}" if getattr(star, "author", None) else ""
        return f"{status}{reserved}  {star.name}{version}{author}"

    # ─────────────────────────────────────────
    # /plugin list
    # ─────────────────────────────────────────

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("plugin list")
    async def list_plugins(self, event: AstrMessageEvent):
        """查看所有已加载的插件"""
        stars = self.context.get_all_stars()
        if not stars:
            yield event.plain_result("📦 当前没有加载任何插件。")
            return

        activated = [s for s in stars if s.activated and not getattr(s, "reserved", False)]
        deactivated = [s for s in stars if not s.activated and not getattr(s, "reserved", False)]
        reserved = [s for s in stars if getattr(s, "reserved", False)]

        lines = ["📦 **插件列表**", f"共 {len(stars)} 个插件"]
        if activated:
            lines.append(f"\n**✅ 已启用 ({len(activated)})**")
            for s in activated:
                version = f" v{s.version}" if getattr(s, "version", None) else ""
                lines.append(f"  · {s.name}{version}")
        if deactivated:
            lines.append(f"\n**❌ 已禁用 ({len(deactivated)})**")
            for s in deactivated:
                version = f" v{s.version}" if getattr(s, "version", None) else ""
                lines.append(f"  · {s.name}{version}")
        if reserved:
            lines.append(f"\n**🔒 保留插件 ({len(reserved)})**")
            for s in reserved:
                version = f" v{s.version}" if getattr(s, "version", None) else ""
                lines.append(f"  · {s.name}{version}")

        yield event.plain_result("\n".join(lines))

    # ─────────────────────────────────────────
    # /plugin info
    # ─────────────────────────────────────────

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("plugin info")
    async def plugin_info(self, event: AstrMessageEvent, name: str):
        """查看插件详情  用法：/plugin info <插件名>"""
        star = self.context.get_registered_star(name)
        if not star:
            yield event.plain_result(f"❌ 找不到插件「{name}」")
            return

        lines = [
            f"📋 **插件详情：{star.name}**",
            f"显示名：{getattr(star, 'display_name', name)}",
            f"状态：{'✅ 启用' if star.activated else '❌ 禁用'}",
        ]
        if getattr(star, "version", None):
            lines.append(f"版本：{star.version}")
        if getattr(star, "author", None):
            lines.append(f"作者：{star.author}")
        if getattr(star, "repo", None):
            lines.append(f"仓库：{star.repo}")
        if getattr(star, "description", None):
            lines.append(f"描述：{star.description}")
        if getattr(star, "root_dir_name", None):
            lines.append(f"目录：{star.root_dir_name}")
        if getattr(star, "reserved", False):
            lines.append("类型：🔒 保留插件（不可卸载）")

        yield event.plain_result("\n".join(lines))

    # ─────────────────────────────────────────
    # /plugin disable
    # ─────────────────────────────────────────

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("plugin disable")
    async def disable_plugin(self, event: AstrMessageEvent, name: str):
        """禁用插件  用法：/plugin disable <插件名>"""
        star = self.context.get_registered_star(name)
        if not star:
            yield event.plain_result(f"❌ 找不到插件「{name}」")
            return
        if not star.activated:
            yield event.plain_result(f"⚠️ 插件「{name}」已经是禁用状态")
            return
        if getattr(star, "reserved", False):
            yield event.plain_result(f"❌ 「{name}」是保留插件，无法禁用")
            return

        try:
            pm = self._get_pm()
            await pm.turn_off_plugin(name)
            yield event.plain_result(f"✅ 已禁用插件「{name}」")
        except Exception as e:
            logger.error(f"[插件管理] 禁用失败: {e}")
            yield event.plain_result(f"❌ 禁用失败：{e}")

    # ─────────────────────────────────────────
    # /plugin enable
    # ─────────────────────────────────────────

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("plugin enable")
    async def enable_plugin(self, event: AstrMessageEvent, name: str):
        """启用插件  用法：/plugin enable <插件名>"""
        star = self.context.get_registered_star(name)
        if star and star.activated:
            yield event.plain_result(f"⚠️ 插件「{name}」已经是启用状态")
            return
        if star and getattr(star, "reserved", False):
            yield event.plain_result(f"❌ 「{name}」是保留插件，无需手动启用")
            return

        try:
            pm = self._get_pm()
            await pm.turn_on_plugin(name)
            yield event.plain_result(f"✅ 已启用插件「{name}」")
        except Exception as e:
            logger.error(f"[插件管理] 启用失败: {e}")
            yield event.plain_result(f"❌ 启用失败：{e}")

    # ─────────────────────────────────────────
    # /plugin reload
    # ─────────────────────────────────────────

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("plugin reload")
    async def reload_plugin(self, event: AstrMessageEvent, name: str):
        """重载插件  用法：/plugin reload <插件名>"""
        star = self.context.get_registered_star(name)
        if not star:
            yield event.plain_result(f"❌ 找不到插件「{name}」")
            return

        try:
            pm = self._get_pm()
            purged, _snapshot = self._purge_plugin_modules(star.name)
            await pm.reload(None)  # 2026-09-15 23:27 锁全量——指定分支在 r7 reload（4257）
            # 中漏 star_cls 换血（r7 探针不出现），全强制走 core load(None)
            # 全量 re-build Plugin class 才是真绿路径；代价 reload ~20s，真绿优先
            tip = f"（深度清理模块缓存 {len(purged)} 个）" if purged else ""
            yield event.plain_result(f"✅ 已重载插件「{name}」{tip}")
        except Exception as e:
            logger.error(f"[插件管理] 重载失败: {e}")
            yield event.plain_result(f"❌ 重载失败：{e}")

    # ─────────────────────────────────────────
    # /plugin install
    # ─────────────────────────────────────────

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("plugin install")
    async def install_plugin(self, event: AstrMessageEvent, repo_url: str):
        """从 GitHub 仓库安装插件  用法：/plugin install <仓库URL>"""
        if not self.config.get("allow_install", False):
            yield event.plain_result(
                "❌ 安装功能未开启。\n"
                "请在 AstrBot 插件配置中开启 allow_install 选项，或在配置文件中设置。"
            )
            return

        yield event.plain_result(f"⏳ 正在从 {repo_url} 安装插件，请稍候...")

        try:
            pm = self._get_pm()
            result = await pm.install_plugin(repo_url)
            if result:
                plugin_name = result.get("name", "未知")
                yield event.plain_result(f"✅ 插件安装成功！「{plugin_name}」")
            else:
                yield event.plain_result("✅ 插件安装完成（无法获取详细信息）")
        except Exception as e:
            logger.error(f"[插件管理] 安装失败: {e}")
            yield event.plain_result(f"❌ 安装失败：{e}")

    # ─────────────────────────────────────────
    # /plugin uninstall
    # ─────────────────────────────────────────

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("plugin uninstall")
    async def uninstall_plugin(self, event: AstrMessageEvent, name: str):
        """卸载插件  用法：/plugin uninstall <插件名>"""

        # 防止卸载自己
        if name == "astrbot_plugin_auto_reload":
            yield event.plain_result("❌ 不能卸载插件管理助手本身（需要先手动禁用本插件）")
            return

        if not self.config.get("allow_uninstall", False):
            yield event.plain_result(
                "❌ 卸载功能未开启。\n"
                "请在 AstrBot 插件配置中开启 allow_uninstall 选项，或在配置文件中设置。"
            )
            return

        star = self.context.get_registered_star(name)
        if not star:
            yield event.plain_result(f"❌ 找不到插件「{name}」")
            return
        if getattr(star, "reserved", False):
            yield event.plain_result(f"❌ 「{name}」是保留插件，无法卸载")
            return

        try:
            pm = self._get_pm()
            await pm.uninstall_plugin(name, delete_config=False, delete_data=False)
            yield event.plain_result(f"✅ 已卸载插件「{name}」")
        except Exception as e:
            logger.error(f"[插件管理] 卸载失败: {e}")
            yield event.plain_result(f"❌ 卸载失败：{e}")

    # ─────────────────────────────────────────
    # /plugin update
    # ─────────────────────────────────────────

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("plugin update")
    async def update_plugin(self, event: AstrMessageEvent, name: str):
        """更新插件（从原仓库拉取最新版本）  用法：/plugin update <插件名>"""
        star = self.context.get_registered_star(name)
        if not star:
            yield event.plain_result(f"❌ 找不到插件「{name}」")
            return
        if getattr(star, "reserved", False):
            yield event.plain_result(f"❌ 「{name}」是保留插件，无法更新")
            return
        if not getattr(star, "repo", None):
            yield event.plain_result(f"❌ 插件「{name}」没有配置仓库地址，无法自动更新")
            return

        yield event.plain_result(f"⏳ 正在更新插件「{name}」，请稍候...")

        try:
            pm = self._get_pm()
            await pm.update_plugin(name)
            yield event.plain_result(f"✅ 插件「{name}」更新完成，已自动重载")
        except Exception as e:
            logger.error(f"[插件管理] 更新失败: {e}")
            yield event.plain_result(f"❌ 更新失败：{e}")

    # ─────────────────────────────────────────
    # /plugin help
    # ─────────────────────────────────────────

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("plugin help")
    async def plugin_help(self, event: AstrMessageEvent):
        """查看插件管理助手帮助"""
        lines = [
            "🛠️ **插件管理助手 帮助**",
            "",
            "**查看**",
            "  /plugin list — 查看所有插件",
            "  /plugin info <名称> — 查看插件详情",
            "",
            "**启停**",
            "  /plugin enable <名称> — 启用插件",
            "  /plugin disable <名称> — 禁用插件",
            "  /plugin reload <名称> — 重载插件",
            "",
            "**安装/卸载**",
            "  /plugin install <GitHub URL> — 安装插件（需开启 allow_install）",
            "  /plugin uninstall <名称> — 卸载插件（需开启 allow_uninstall）",
            "  /plugin update <名称> — 更新插件",
            "",
            "**其他**",
            "  /plugin help — 显示本帮助",
            "",
            "💡 所有指令需要管理员权限",
        ]
        yield event.plain_result("\n".join(lines))

    # ─────────────────────────────────────────
    # LLM 工具注册（主代理直接调用）
    # ─────────────────────────────────────────

    async def _resolve_plugin_key(self, name: str) -> str:
        """把展示名/序数/内部名/目录名解析成 star 内部名"""
        from astrbot.core.star.star import star_registry as sr

        name = str(name).strip()
        if name.isdigit():
            visible = [m for m in sr if not m.reserved]
            idx = int(name) - 1
            if 0 <= idx < len(visible):
                return visible[idx].name
            raise ValueError(
                _plugin_name_hint(name, sr, f"序号超出范围（共 {len(visible)} 个）")
            )

        # ① 精确：内部名 / 展示名
        for m in sr:
            if name == str(m.name) or name == str(m.display_name):
                return m.name

        # ② 归一化：astrbot_plugin_parallel_handoff → parallel_handoff
        norm = _normalize_plugin_name(name)
        if norm:
            for m in sr:
                if norm in (
                    _normalize_plugin_name(m.name),
                    _normalize_plugin_name(m.display_name),
                ):
                    return m.name

        # ③ 子串兜底（保留原有宽松匹配）
        for m in sr:
            if name in str(m.display_name) or name in str(m.name):
                return m.name
        if norm:
            for m in sr:
                n = _normalize_plugin_name(m.name)
                if n and (norm in n or n in norm):
                    return m.name

        raise ValueError(_plugin_name_hint(name, sr))

    @filter.llm_tool(name="hot_reload_plugin")
    async def _llm_hot_reload(
        self,
        event: AstrMessageEvent,
        name: str,
        handover_platforms: bool = False,
        reclaim_tasks: bool = False,
        quick: bool = True,
    ) -> str:
        """热重载任意 AstrBot 插件（重载后插件代码立即生效，返回值自带验证回执）。

        Args:
            name (string): 插件名（可传 all 表示全部）。支持内部名/展示名/目录名/序号（从 plugin_list 输出取序）。传错时回执会直接给出内部名候选。
            handover_platforms (boolean): 二期能力，默认 False。开启后在该插件重载完成后，停掉它持有的旧平台适配器实例、按新代码重挂（治「平台适配器型」插件重载后仍走旧代码）。代价是通道断线重连约 1-3 秒，期间消息可能丢，仅在确实需要换血时开启。
            reclaim_tasks (boolean): 三期能力，默认 False。开启后取消该插件自己 create_task 起、且仍跑旧代码的后台任务（定时器/巡检/重试循环这类，不经平台注册、二期收不走）。只杀「持旧模块代码」的任务，新任务不受影响。适合插件自起后台循环的场合；不确定时可先只开 handover_platforms，看回执里 🧬 那行再定。
            quick (boolean): 定向快路径，默认 True（只对指定单个插件生效，传 all 时忽略）。开启后不再全量重载：只摘该插件的绑定/注册表 + 深度清它的模块缓存，然后只重导它一个（其余插件不终止、不重新 import，约 1-3 秒）。重导完立刻跑三判据探针（模块换新 / 类血同源 / 注册表唯一），探针不过就自动升级全量重载兜底，回执里会写明走了哪条路。传 False 强制直接全量。
        """
        try:
            allowed = event.is_admin() if hasattr(event, "is_admin") else False
        except Exception:
            allowed = False
        if not (allowed or _is_owner(event, self.config.get("owner_uids"))):
            return "权限不足：仅管理员或配置的所有者可调用"

        target_all = str(name).strip().lower() == "all"
        plugin_key = None
        if not target_all:
            try:
                plugin_key = await self._resolve_plugin_key(name)
            except Exception as e:
                return f"❌ 未找到插件「{name}」\n{e}"

        # 方案 B 总闸（2026-09-16 晚）：面板可一键禁用快路径，回到纯全量。
        quick = bool(quick) and bool(self.config.get("hot_reload_quick_default", True))
        before = self._snapshot_star(plugin_key) if plugin_key else {}
        # 深度清理模块缓存：AstrBot 的 reload 走 __import__，
        # sys.modules 里已缓存的子模块不会被清（核心的模块名前缀匹配与运行时
        # 模块名对不上），导致 router.py/dispatch.py/memory.py 这类拆分子模块的
        # 改动“重载成功但代码不生效”。这里按内部名/文件路径精确挖掉，强制重读盘。
        purged, module_snapshot = [], {}
        if plugin_key:
            purged, module_snapshot = self._purge_plugin_modules(plugin_key)
        # 先清尸再重载（2026-09-11 实证）：热重载真正的坑不在模块缓存，而在
        # llm_tools / star_handlers_registry 里残留着指向「已下线旧模块」的绑定——
        # core 的 _unbind_plugin 按模块路径前缀摘，跟运行时模块名对不上就漏摘，
        # 而重新注册的那份又被同名挤掉，于是新代码永远抢不到调用权（自检回执
        # 曾一次报出 12 处孤儿）。必须在 import 之前摘干净，给新注册腾位置。
        evicted = {}
        if plugin_key:
            evicted = self._evict_plugin_bindings(plugin_key)
        capture = _ReloadLogCapture()
        capture.start()
        t0 = time.monotonic()
        path_note = ""
        try:
            # 2026-09-15 23:27 锁全量逻辑同上：核心是「指定某插件分支」能走到的是
            # 损假绿路径—— — core 只 _unbind+load 指定 path，star_map 若已 fresh 会导致直接复用旧 star_cls_type；
            # 传 None 强制 reload 全插件走 load(None)，那边 star_map.clear()
            # + star_registry.clear() + __init_subclass 重新注册 → 全换血 → 真绿。
            # 2026-09-16 晚 方案 B：默认先走「定向快路径」——只重导目标插件，
            # 重导完由三判据探针判定是否真换血；不过就升级下面的全量兜底。
            quick_ok = False
            if plugin_key and quick:
                quick_ok, path_note = await self._quick_target_reload(
                    plugin_key, module_snapshot
                )
            if plugin_key and quick and quick_ok:
                success, error_message = True, None
            else:
                if plugin_key and quick:
                    path_note += (
                        "\n⤴️ 快路径未过 → 已自动升级全量重载（本次仍保证真换血）"
                    )
                success, error_message = await self._get_pm().reload(None)
                if not success:
                    self._restore_plugin_modules(module_snapshot)
        except Exception as e:
            success, error_message = False, f"{type(e).__name__}: {e}"
            self._restore_plugin_modules(module_snapshot)
        finally:
            capture.stop()
            elapsed = time.monotonic() - t0

        after = self._snapshot_star(plugin_key) if plugin_key else {}
        # ── 绑定自检 + 孤儿重绑（2026-09-11）──
        # 核心 reload 只保证「重新 import + 新实例注册」，但 llm_tools /
        # star_handlers_registry 里若还攥着指向「已下线的旧模块」的函数对象，
        # 运行时就是"日志说重载成功、代码一行没换"。下面逐一定比函数对象的
        # __globals__ 是否就是当前在线模块的 __dict__，孤儿就按 __qualname__
        # 从新模块取回同名对象替换。
        rebind_note = ""
        if plugin_key and success:
            try:
                rebind_note = self._audit_and_rebind(plugin_key)
            except Exception as _rb_e:
                rebind_note = f"\n⚠️ 绑定自检异常：{type(_rb_e).__name__}: {_rb_e}"
        # ── 二期：平台适配器实例换手 ──
        # 默认关闭（开关 handover_platforms），只在明确要换血时开。
        # 时序刻意放在「新代码已加载」之后：先重载拿到新类，再停旧实例、用新类
        # 重挂，把断线窗口压到只剩重连那一瞬 —— 而不是反过来让通道断整个重载期。
        # 放在长活对象自检之前，是为了让自检读到的是「换手后」的真实结果。
        handover_note = ""
        if plugin_key and success:
            # 配置项是面板级默认值；工具显式传参（True）可按次覆盖
            _ho_on = handover_platforms or bool(self.config.get("hot_reload_handover_default"))
            if _ho_on:
                try:
                    handover_note = await self._handover_plugin_platforms(plugin_key)
                except Exception as _ho_e:
                    handover_note = f"\n⚠️ 平台换手异常：{type(_ho_e).__name__}: {_ho_e}"
        # ── 三期：陈旧后台任务回收 ──
        # 二期按生命周期停掉的只有「适配器自带的」任务；插件在自己代码里
        # create_task 起的（定时器/巡检/重试循环）没有登记表，谁也停不掉，
        # 只能靠 ④ 的判据认出来再取消。默认关闭（reclaim_tasks）。
        # 同样放在长活自检之前，让自检读到的是「回收后」的真实结果。
        reclaim_note = ""
        if plugin_key and success:
            # 配置项是面板级默认值；工具显式传参（True）可按次覆盖
            _rc_on = reclaim_tasks or bool(self.config.get("hot_reload_reclaim_default"))
            if _rc_on:
                try:
                    reclaim_note = await self._reclaim_plugin_tasks(plugin_key)
                except Exception as _rc_e:
                    reclaim_note = f"\n⚠️ 任务回收异常：{type(_rc_e).__name__}: {_rc_e}"
        # ── 长活对象自检 ──
        # 绑定自检只能看“注册表里的新绑定”，看不了“已经在跑的老链路”。
        # 今天 qq_restapi 语音事件走的正是后者：模块缓存清了、绑定也无孤儿，
        # 但 WS 派发任务与平台适配器实例仍持着旧模块的 globals，事件进来
        # 还是执行旧代码。这里把这类持有者揪出来写进回执，把“假成功”
        # 从“需要人工发消息去试”变成“回执直接明说”。
        live_note = ""
        if plugin_key and success:
            try:
                live_note = self._audit_live_refs(plugin_key)
            except Exception as _lv_e:
                live_note = f"\n⚠️ 长活对象自检异常：{type(_lv_e).__name__}: {_lv_e}"
        receipt = self._format_reload_receipt(
            plugin_key, target_all, success, error_message,
            elapsed, before, after, capture.lines, len(purged),
        )
        # ── 一期：生效度评分 + 必须重启清单 ──
        # 两个自检只能回答“有没有陈旧引用”，回答不了“这次重载到底算不算成功”。
        # 这里再补两样，都是纯回执增强、不碰 core：
        #   ① astrbot/core 下有没有「源文件比 .pyc 新」的模块——有就说明那份改动
        #      是在导入之后落盘的，内存里根本没有它，任何插件重载都救不了，
        #      必须重启（今天提交的三处 core patch 就是这类东西）。
        #   ② 把 L0-L4 分层判定写成一行结论，让“生效到什么程度”一眼可读。
        core_stale = []
        try:
            core_stale = self._scan_unapplied_core_changes()
        except Exception:
            core_stale = []
        try:
            # 2026-09-16 00:10 ⑤ 层生效度：stale 检测插件类方法是否真换血（avoid false green）
            grade_note = self._grade_reload_effect(success, live_note, core_stale, plugin_key=plugin_key)
        except Exception as _g_e:
            grade_note = f"\n⚠️ 生效度评分异常：{type(_g_e).__name__}: {_g_e}"
        return (
            receipt + path_note + rebind_note + handover_note
            + reclaim_note + live_note + grade_note
        )

    # ─────────────────────────────────────────
    # 定向快路径（2026-09-16 晚 · 方案 B）
    # ─────────────────────────────────────────

    def _match_plugin_metas(self, plugin_key: str):
        """找出 star_map 里属于该插件的条目，返回 [(module_path, meta), ...]。

        匹配口径与 _purge_plugin_modules 对齐：先按「模块名分段相等」精确命中，
        全空时才退到子串兜底。子串优先会误伤同名插件，而误伤的后果是 P3
        「注册表唯一」判据误报，把快路径无畐钉死。
        """
        out = []
        try:
            from astrbot.core.star.star import star_map as _sm
        except Exception:
            return out
        key = str(plugin_key or "").strip().lower()
        if not key:
            return out

        def _seg_hit(s) -> bool:
            # star_map 的 key 是「main@data.plugins.xxx.main」形态：@ 把首段粘成
            # 「main@data」，任何内部名永远命中不了首段——必须额外给 key 造一个
            # 「模块名@前缀」形态去撞（2026-09-16 快路径两次报「无条目」的真因）
            star_mod = ""
            if "@" in str(s):
                star_mod = str(s).split("@", 1)[1]
            return any(
                seg.lower() == key
                for seg in str(s).split(".") + (star_mod.split(".") if star_mod else [])
            )

        for path, meta in list(_sm.items()):
            cls = getattr(meta, "star_cls_type", None)
            cm = str(getattr(cls, "__module__", "") or "")
            if _seg_hit(path) or _seg_hit(cm):
                out.append((path, meta))
        if not out:
            for path, meta in list(_sm.items()):
                cls = getattr(meta, "star_cls_type", None)
                cm = str(getattr(cls, "__module__", "") or "")
                if key in str(path).lower() or key in cm.lower():
                    out.append((path, meta))
        return out

    def _resolve_target_dir(self, plugin_key: str) -> str:
        """定向 load 要的是插件目录名（specified_dir_name），不是内部名。

        meta.root_dir_name 由 core load() 写入（= data/plugins 下的目录名）；
        metadata.yaml 的 name 与目录名不一致时，用内部名去 load 会一个都匹配不上。
        """
        for _path, meta in self._match_plugin_metas(plugin_key):
            d = str(getattr(meta, "root_dir_name", "") or "")
            if d:
                return d
        try:
            for m in self.context.get_all_stars():
                if getattr(m, "name", "") == plugin_key:
                    d = str(getattr(m, "root_dir_name", "") or "")
                    if d:
                        return d
        except Exception:
            pass
        return str(plugin_key or "")

    def _verify_quick_swap(self, plugin_key: str, module_snapshot: dict):
        """快路径三判据探针（零误报优先，任一不过即判「未真换血」→ 升级全量）。

        P1 模块换新：主模块必须回到 sys.modules 且是**新对象**。purge 前旧对象
                     的 id 存在 module_snapshot 里，id 相同即旧模块复活（Python
                     id 复用概率可忽略）。再补一刀：若被清模块无一换新，全判假绿。
        P2 类血同源：在线 star_cls_type 的 __globals__ 必须就是当前 sys.modules
                     里同名模块的 __dict__。旧类连着旧模块字典，一测就露——
                     这正是 2026-09-15「star_map 复用旧 star_cls_type」的病根。
        P3 注册表唯一：star_registry 里该插件只剩一条、实例类型与注册类一致。
        """
        import sys as _sys

        reasons = []
        metas = self._match_plugin_metas(plugin_key)
        if not metas:
            return {"ok": False, "reasons": ["注册表里找不到该插件条目（摘表后未重新注册）"]}

        path, meta = metas[0]
        old_ids = {n: id(m) for n, m in (module_snapshot or {}).items()}
        main_mod = str(getattr(meta, "module_path", "") or path)
        live = _sys.modules.get(main_mod)
        if live is None:
            reasons.append(f"主模块未回到 sys.modules：{main_mod.split('.')[-1]}")
        elif old_ids.get(main_mod) == id(live):
            reasons.append(f"主模块对象未换新（id 未变）：{main_mod.split('.')[-1]}")
        swapped = 0
        for n, oid in old_ids.items():
            cur = _sys.modules.get(n)
            if cur is not None and id(cur) != oid:
                swapped += 1
        if old_ids and swapped == 0:
            reasons.append(f"被清理的 {len(old_ids)} 个模块无一换新对象")

        cls = getattr(meta, "star_cls_type", None)
        if cls is None:
            reasons.append("star_cls_type 为空")
        else:
            mod = _sys.modules.get(str(getattr(cls, "__module__", "") or ""))
            if mod is None:
                reasons.append(f"类所属模块不在线：{getattr(cls, '__module__', '?')}")
            else:
                # 【2026-09-17 真凶】原判据 getattr(cls, "__globals__", None) 是恒 None：
                # __globals__ 是**函数**的属性，类根本没有它（CPython 实测
                # hasattr(SomeClass, "__globals__") is False，A.__init__ 若是继承来的
                # object.__init__ 更是 wrapper_descriptor）。取到 None 后
                # `None is not mod.__dict__` 恒真 → P2 对任何插件、任何时刻都判不过，
                # 快路径被这把假秤永久钉死：每次都白跑 ~0.16s 再升级 18s 全量
                # （01:13/01:15/01:16/01:21 四枪全挂同一句话，而 P1 每次都过 ——
                # 换血其实一直是好的，只死在探针自己身上）。
                # 正确取法两条，都要过：
                #   ① 在线模块里按名字找到的必须是同一个类对象（最正规、恒可用）；
                #   ② 类体里函数的 __globals__ 必须就是该模块的 __dict__（取得到时再查）；
                # 类体里一个函数都没有时 ② 自然跳过，不误伤。
                mod_cls = getattr(mod, str(getattr(cls, "__name__", "") or ""), None)
                cls_globals = None
                for _v in list(vars(cls).values()):
                    _fn = getattr(_v, "__func__", _v)  # classmethod/staticmethod/bound 拆包
                    _g = getattr(_fn, "__globals__", None)
                    if _g is not None:
                        cls_globals = _g
                        break
                _cname = getattr(cls, "__name__", "?")
                if mod_cls is not cls:
                    reasons.append(
                        f"类血不同源：{getattr(cls, '__module__', '?')} 在线模块里的 "
                        f"{_cname} 不是同一个类对象（旧类仍被注册表攥着）"
                    )
                elif cls_globals is not None and cls_globals is not mod.__dict__:
                    reasons.append(
                        f"类血不同源：{getattr(cls, '__module__', '?')} 的类连着别的模块字典"
                    )

        if len(metas) > 1:
            reasons.append(f"注册表里该插件有 {len(metas)} 条条目（重复注册）")
        star_cls = getattr(meta, "star_cls", None)
        if star_cls is None:
            reasons.append("实例未重建（star_cls 为空）")
        elif cls is not None and type(star_cls) is not cls:
            reasons.append("实例类型与注册类不一致（新旧类混用）")

        if reasons:
            return {"ok": False, "reasons": reasons[:4]}
        return {
            "ok": True,
            "detail": f"模块换新 {swapped}/{len(old_ids)}、类血同源、注册表唯一",
        }

    async def _quick_target_reload(self, plugin_key: str, module_snapshot: dict):
        """方案 B 定向快路径：只终止并重导目标插件，其余插件原地不动。

        走 core 的 `load(specified_dir_name=目录名)`，不是 `reload(名字)`：
        后者第一步要在 star_registry 里按名字找回 module_path，而我们的摘表刀
        已经把这条摘了 → specified_module_path 保持 None → 自动退化成全量
        （这就是「为什么每次都全量」的隐藏联动）。load(目录名) 只 import 那一个
        目录（star_manager.load 里的 specified_dir_name 分支），三张表一张不碰。

        终止旧实例得自己做 —— core 只在那条退化前的指定分支里 terminate，而我们
        绕开的正是它。`_terminate_plugin` 是 core 私有 API，这里与摘表/摘绑定
        同源使用，异常一律不抛出（终止失败也要继续重导）。

        Returns:
            (ok, note)：ok=False 时调用方必须升级全量，且本次快路径结果不采信。
        """
        pm = self._get_pm()
        metas = self._match_plugin_metas(plugin_key)
        if not metas:
            return False, "\n⇢ 定向快路径：注册表无该插件条目（疑似未安装或已禁用）"
        dir_name = self._resolve_target_dir(plugin_key)
        if not dir_name:
            return False, "\n⇢ 定向快路径：拿不到插件目录名"

        for _path, meta in metas:
            try:
                await pm._terminate_plugin(meta)
            except Exception as e:
                logger.warning(
                    f"[插件管理] 快路径终止旧实例异常（继续重导）：{type(e).__name__}: {e}"
                )

        # ── 快路径专用摘表（2026-09-16 从 _evict_plugin_bindings 挪入）──
        # core load() 的路径是「if path in star_map: 直接复用旧 star_cls_type」，
        # 不摘掉旧条目，新 import 的类注册不进去（2026-09-15 假绿根因）。
        # 全量兜底走 reload(None)，core 自己 star_map.clear()，不需要这里动手。
        # 必须在 terminate 之后、load 之前摘——查表(779)/terminate 都要靠旧条目活着。
        pkey = str(plugin_key or "").strip().lower()
        star_removed = 0
        star_keys = []
        try:
            from astrbot.core.star.star import star_map as _sm, star_registry as _sr

            for _path, _meta in list(_sm.items()):
                mp = str(getattr(_meta, "module_path", "") or "").lower()
                cls = getattr(_meta, "star_cls_type", None)
                if pkey not in mp and not (
                    cls is not None and pkey in str(getattr(cls, "__module__", "")).lower()
                ):
                    continue
                try:
                    # 按「值同一性」摘，不再靠 key 猜。
                    # 2026-09-17 复核更正：star_map 的真实 key 就是 module_path
                    # （core load() 里 path = "data.plugins." + 目录名 + "." + 模块名，
                    # 由 __init_subclass__ 写进去，实测日志 key=['data.plugins.
                    # astrbot_plugin_skill_cache_guard.main']）—— 老写法
                    # `if mpth in _sm: del _sm[mpth]` 其实是删得掉的，先前记的
                    # 「main@data.plugins.X.main」形态之说不成立，_match_plugin_metas
                    # 里那段 @ 拆解逻辑也是基于错误观察写的（待重审）。
                    # 这里改按值删只图一件事：即便 core 今后改命名口径，也不会静默漏摘表。
                    for _k, _v in list(_sm.items()):
                        if _v is _meta:
                            del _sm[_k]
                            star_keys.append(str(_k))
                    _sr.remove(_meta)
                    star_removed += 1
                except Exception:
                    pass
            if star_removed:
                logger.warning(
                    f"[插件管理] 快路径重导前摘除 star_map/star_registry "
                    f"{star_removed} 个（star_map key={star_keys}）"
                )
        except Exception as _sm_e:
            logger.warning(
                f"[插件管理] 快路径摘表异常（继续重导）：{type(_sm_e).__name__}: {_sm_e}"
            )

        t1 = time.monotonic()
        try:
            ok, err = await pm.load(specified_dir_name=dir_name)
        except Exception as e:
            ok, err = False, f"{type(e).__name__}: {e}"
        cost = time.monotonic() - t1
        if not ok:
            return False, f"\n⇢ 定向快路径：load({dir_name}) 失败：{err}（{cost:.2f}s）"

        chk = self._verify_quick_swap(plugin_key, module_snapshot)
        if not chk.get("ok"):
            why = "；".join(chk.get("reasons", [])[:3])
            return False, f"\n⇢ 定向快路径：三判据探针未过（{why}），耗时 {cost:.2f}s"
        logger.info(
            f"[插件管理] 定向快路径通过：{plugin_key} {chk.get('detail', '')}（{cost:.2f}s）"
        )
        return True, (
            f"\n⇢ 路径：定向快路径（只重导 {dir_name}，其余插件未动，{cost:.2f}s）"
            f"\n   探针：{chk.get('detail', '')}"
        )

    def _purge_plugin_modules(self, plugin_key: str):
        """把插件自身及其子模块从 sys.modules 里挖掉，强制下次 import 重读盘。

        Returns:
            (purged_names, snapshot)：snapshot 供重载失败时回滚用。
        """
        import sys as _sys
        import importlib as _importlib

        key = str(plugin_key or "").strip().lower()
        if not key:
            return [], {}
        names, snapshot = [], {}
        for mod_name, mod in list(_sys.modules.items()):
            if mod is None:
                continue
            hit = any(seg.lower() == key for seg in mod_name.split("."))
            if not hit:
                f = getattr(mod, "__file__", None)
                if f and key in str(f).replace("\\", "/").lower():
                    hit = True
            if hit:
                names.append(mod_name)
                snapshot[mod_name] = mod
        # 先删深的子模块，再删包本体
        for m in sorted(names, key=lambda s: s.count("."), reverse=True):
            _sys.modules.pop(m, None)
        if names:
            _importlib.invalidate_caches()
            logger.info(
                f"[插件管理] 深度清理模块缓存 {len(names)} 个：{sorted(names)[:8]}"
            )
        else:
            cands = [
                m for m in list(_sys.modules.keys())
                if "plugin" in m.lower() and not m.startswith("_")
            ]
            self._last_purge_diag = (
                f"未匹配 [{key}]，sys.modules 含 plugin 的模块：{cands[:10]}"
            )
        return names, snapshot

    def _restore_plugin_modules(self, snapshot: dict) -> None:
        """重载失败时把模块缓存还原回去，避免插件半死不活"""
        if not snapshot:
            return
        import sys as _sys
        restored = 0
        for name, mod in snapshot.items():
            if name not in _sys.modules:
                _sys.modules[name] = mod
                restored += 1
        if restored:
            logger.warning(f"[插件管理] 重载失败，已回滚 {restored} 个模块缓存")

    def _evict_plugin_bindings(self, plugin_key: str) -> dict:
        """摘除插件在 llm_tools / star_handlers_registry 里的全部绑定（重载前清尸）。

        匹配规则刻意放宽到两层兑底：模块路径含内部名 → 函数 __globals__['__file__']
        路径含内部名。core 的 _unbind_plugin 只做路径前缀匹配，运行时模块名一旦
        对不上就漏摘，留下"尸块绑定"把新代码堵在门外。

        Returns:
            {"tools": n, "handlers": n, "samples": [...]}
        """
        key = str(plugin_key or "").strip().lower()
        if not key:
            return {}

        llm_tools = None
        try:
            from astrbot.core.provider.register import llm_tools as _lt

            llm_tools = _lt
        except Exception:
            pass
        handlers_reg = None
        try:
            from astrbot.core.star.star_handler import star_handlers_registry as _hr

            handlers_reg = _hr
        except Exception:
            pass

        def _fn_of(obj):
            for attr in ("handler", "func", "callback", "func_obj"):
                f = getattr(obj, attr, None)
                if callable(f):
                    return f
            return None

        def _belongs(obj):
            mp = str(getattr(obj, "handler_module_path", "") or "").lower()
            if key in mp:
                return True
            fn = _fn_of(obj)
            g = getattr(fn, "__globals__", None) if fn is not None else None
            if isinstance(g, dict) and key in str(g.get("__file__", "")).lower():
                return True
            return False

        n_tools = n_handlers = 0
        samples = []

        if llm_tools is not None:
            fl = getattr(llm_tools, "func_list", None)
            if isinstance(fl, list):
                for t in list(fl):
                    if not _belongs(t):
                        continue
                    try:
                        fl.remove(t)
                        n_tools += 1
                        if len(samples) < 4:
                            samples.append(f"tool:{getattr(t, 'name', '?')}")
                    except Exception:
                        pass

        if handlers_reg is not None:
            try:
                hs = list(handlers_reg)
            except Exception:
                hs = []
            remover = getattr(handlers_reg, "remove_handler", None)
            for h in hs:
                if not _belongs(h):
                    continue
                try:
                    if callable(remover):
                        remover(h)
                    else:
                        inner = getattr(handlers_reg, "handlers", None)
                        if isinstance(inner, list):
                            inner.remove(h)
                    n_handlers += 1
                    if len(samples) < 6:
                        samples.append(f"handler:{getattr(h, 'handler_name', '?')}")
                except Exception:
                    pass

        if n_tools or n_handlers:
            logger.warning(
                f"[插件管理] 重载前摘除旧绑定：tool×{n_tools} handler×{n_handlers} {samples}"
            )

        # 第三张表（star_map/star_registry）的摘除已挪进 _quick_target_reload：
        # 它是快路径 load(specified_dir_name) 的前置条件，而 evict 在快路径查表
        # 之前跑——提前摘掉 star_map 会让快路径查表必然扑空（2026-09-16 两次
        # 「注册表无该插件条目」的真因之一）。全量兜底走 reload(None)，core 自己
        # star_map.clear()，无需这里动手。
        return {
            "tools": n_tools,
            "handlers": n_handlers,
            "samples": samples,
        }

    def _audit_and_rebind(self, plugin_key: str) -> str:
        """核对插件 handler/tool 的绑定是否指向「当前在线的模块」，孤儿则强制重绑。

        判定依据：函数对象的 __globals__ 必须**就是** sys.modules 里同名模块的
        __dict__（同一个字典对象）。热重载清掉旧模块后，如果 tool/handler 里
        拒着的还是旧模块的函数，两者就不是同一个 dict —— 这正是「重载成功、
        代码不生效」的物证。

        Returns:
            追加进重载回执的可读结论，空串表示不报告。
        """
        import sys as _sys

        key = str(plugin_key or "").strip().lower()
        if not key:
            return ""

        llm_tools = None
        try:
            from astrbot.core.provider.register import llm_tools as _lt

            llm_tools = _lt
        except Exception:
            pass
        handlers_reg = None
        try:
            from astrbot.core.star.star_handler import star_handlers_registry as _hr

            handlers_reg = _hr
        except Exception:
            pass

        def _fn_of(obj):
            """鸭子类型取可调用实体，兼容不同版本的字段名"""
            for attr in ("handler", "func", "callback", "func_obj"):
                f = getattr(obj, attr, None)
                if callable(f):
                    return attr, f
            return None, None

        def _is_orphan(fn, mod_name):
            """函数所属模块是否已下线。

            刻意**不按模块名**查 sys.modules（运行时模块名与表键形态常不一致，
            曾因此在回执里误报 12 处孤儿）：改用函数 __globals__ 里的 __file__
            去找同源模块——找得到、且那份 __dict__ 就是这份 globals，才算活绑定。
            """
            if fn is None:
                return False
            g = getattr(fn, "__globals__", None)
            if not isinstance(g, dict):
                return False
            fpath = g.get("__file__")
            if not fpath:
                return False
            for _m in list(_sys.modules.values()):
                if (
                    getattr(_m, "__file__", None) == fpath
                    and getattr(_m, "__dict__", None) is g
                ):
                    return False
            return True

        def _resolve_new(mod_name, fn):
            """按 __qualname__ 从新模块取回同名对象（支持类方法）"""
            if mod_name is None:
                return None
            mod = _sys.modules.get(str(mod_name))
            if mod is None:
                return None
            qual = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None)
            if not qual:
                return None
            obj = mod
            for part in qual.split("."):
                obj = getattr(obj, part, None)
                if obj is None:
                    return None
            return obj

        done_tools, done_handlers, done_self, failed = [], [], [], []

        # ── 2026-09-15 23:00 新增：bound method 的 __self__ 一致性检查 ────
        # 根因补充：_is_orphan 只查 __globals__（函数定义所在模块 dict），但
        # llm_tool 注册的是 **bound method**（Plugin 实例的方法）。类重定义后
        # __globals__ 跟着新模块 dict 一起翻新，_is_orphan 恰好看不出破绽，
        # 可 __self__ 攥着的仍是旧 Plugin 实例 —— audit 全绿、跑起来的方法体
        # 却是旧 MRO 上的旧 Mixin（2026-09-15 22:57 ·r6 探针不跑的实证）。
        # 所以这里再补一刀：__self__.__class__ 必须就是 star_map[path] 里的
        # star_cls_type；不然就把 bound method 从新实例上重新取一次。
        def _resolve_fresh_self():
            """取该插件当前 star_map 条目上的新 Plugin 实例"""
            try:
                _sm3 = _sys.modules.get("astrbot.core.star.star")
                sm = getattr(_sm3, "star_map", None)
                if sm is None:
                    return None
                for _p in sm:
                    if key in str(_p).lower():
                        return getattr(sm[_p], "star_cls", None)
            except Exception:
                pass
            return None

        def _rebind_self(t, attr_name, fn, log_line):
            """bound method 指向旧实例时，从 star_cls 重新取同名方法"""
            fresh = _resolve_fresh_self()
            if fresh is None:
                return None
            fname = getattr(fn, "__name__", None)
            if not fname:
                return None
            new_fn = getattr(fresh, fname, None)
            if new_fn is None or getattr(new_fn, "__self__", None) is not fresh:
                return None
            try:
                setattr(t, attr_name, new_fn)
                return new_fn
            except Exception:
                return None

        # ① llm_tools 里的工具绑定
        if llm_tools is not None:
            _fl = getattr(llm_tools, "func_list", None) or []
            for t in list(_fl):
                mp = str(getattr(t, "handler_module_path", "") or "")
                if key not in mp.lower():
                    continue
                attr_name, fn = _fn_of(t)
                if fn is None:
                    continue
                # ⚠️ 2026-09-15 23:05 破案新增：bound method 的 __self__ 检查
                # 旧实例掉在 bound method 上而 __globals__ 已经同源翻新时，
                # _is_orphan 判游哑火（这正是 22:57 ·r6 探针不跑的实证）——
                # 所以先看 __self__.__class__ 还是不是当前 star_cls_type。
                _inst = getattr(fn, "__self__", None)
                _fresh = _resolve_fresh_self() if _inst is not None else None
                _stale = (
                    _inst is not None
                    and _fresh is not None
                    and getattr(_inst, "__class__", None) is not getattr(_fresh, "__class__", None)
                )
                if _stale:
                    if _rebind_self(t, attr_name, fn, "self") is not None:
                        done_self.append(getattr(t, "name", "?"))
                    continue
                if not _is_orphan(fn, mp):
                    continue
                tname = getattr(t, "name", "?")
                try:
                    # 摘除而非重绑：旧模块已下线、没有可复活的来源，且同名的
                    # 新版绑定已经在表里（它才是实际被调用的那个）。留着旧对象
                    # 只会让回执背一串“孤儿”假警报。
                    _fl.remove(t)
                    done_tools.append(tname)
                except Exception:
                    failed.append(f"tool:{tname}")

        # ② star_handlers_registry 里的 handler 绑定
        if handlers_reg is not None:
            try:
                _hs = list(handlers_reg)
            except Exception:
                _hs = []
            remover = getattr(handlers_reg, "remove_handler", None)
            for h in _hs:
                mp = str(getattr(h, "handler_module_path", "") or "")
                if key not in mp.lower():
                    continue
                attr_name, fn = _fn_of(h)
                if fn is None:
                    continue
                # 同前：__self__ 旧实例时先尝试从新 star_cls 实例取同名方法重绑
                _inst = getattr(fn, "__self__", None)
                _fresh = _resolve_fresh_self() if _inst is not None else None
                _stale = (
                    _inst is not None
                    and _fresh is not None
                    and getattr(_inst, "__class__", None) is not getattr(_fresh, "__class__", None)
                )
                if _stale:
                    if _rebind_self(h, attr_name, fn, "self") is not None:
                        done_self.append(getattr(h, "handler_name", "?"))
                    continue
                if not _is_orphan(fn, mp):
                    continue
                hname = getattr(h, "handler_name", "?")
                try:
                    if callable(remover):
                        remover(h)
                    else:
                        _hs_list = getattr(handlers_reg, "handlers", None)
                        if isinstance(_hs_list, list):
                            _hs_list.remove(h)
                    done_handlers.append(hname)
                except Exception:
                    failed.append(f"handler:{hname}")

        if not (done_tools or done_handlers or done_self or failed):
            return "\n🔗 绑定自检：全部指向当前在线模块，无孤儿"

        parts = []
        if done_self:
            parts.append(f"__self__ 换血×{len(done_self)}（{', '.join(done_self[:5])}）")
            logger.warning(f"[插件管理] __self__ 重绑到新 star_cls 实例：{done_self}")
        if done_tools:
            parts.append(f"tool×{len(done_tools)}（{', '.join(done_tools[:5])}）")
            logger.warning(f"[插件管理] 已重绑孤儿 tool：{done_tools}")
        if done_handlers:
            parts.append(f"handler×{len(done_handlers)}（{', '.join(done_handlers[:5])}）")
            logger.warning(f"[插件管理] 已重绑孤儿 handler：{done_handlers}")
        if failed:
            parts.append(f"重绑失败×{len(failed)}（{', '.join(failed[:5])}）")
            logger.error(f"[插件管理] 孤儿重绑失败：{failed}")
        return "\n🧹 绑定自检：清理残留绑定 " + "、".join(parts)

    def _find_plugin_platforms(self, plugin_key: str) -> list:
        """找出归属该插件的平台适配器实例及其配置（只读，不动任何东西）。

        归属判据与 ④ 自检同源：类所在模块名 / 类 __init__ 的 __file__ 落在插件
        目录内。配置来源优先级：platform_manager.platforms_config 里同 id 的权威
        条目（带 enable），退回实例自带的 config（须含 id/type/enable 三键）。

        Returns:
            [{"id":..., "config": {...}或None, "cls": "类名"}]。
        """
        key = str(plugin_key or "").strip().lower()
        if not key:
            return []
        pm = getattr(self.context, "platform_manager", None)
        if pm is None:
            return []
        cfg_by_id = {}
        try:
            for _c in list(getattr(pm, "platforms_config", []) or []):
                if isinstance(_c, dict) and _c.get("id"):
                    cfg_by_id[str(_c["id"])] = _c
        except Exception:
            pass
        out = []
        try:
            for inst in list(getattr(pm, "platform_insts", []) or []):
                cls = type(inst)
                own = key in str(getattr(cls, "__module__", "") or "").lower()
                if not own:
                    _g = getattr(getattr(cls, "__init__", None), "__globals__", None)
                    if isinstance(_g, dict):
                        _f = str(_g.get("__file__") or "").replace("\\", "/").lower()
                        own = key in _f
                if not own:
                    continue
                pid, own_cfg = None, None
                try:
                    own_cfg = getattr(inst, "config", None)
                    if isinstance(own_cfg, dict) and own_cfg.get("id"):
                        pid = str(own_cfg["id"])
                except Exception:
                    own_cfg = None
                cfg = cfg_by_id.get(pid) if pid else None
                if cfg is None and isinstance(own_cfg, dict) and {"id", "type", "enable"} <= set(own_cfg):
                    cfg = own_cfg
                    pid = pid or str(own_cfg.get("id"))
                if pid is None:
                    # 退路：拿 client_self_id 反查 _inst_map
                    try:
                        _cid = getattr(inst, "client_self_id", None)
                        for _k, _v in (getattr(pm, "_inst_map", {}) or {}).items():
                            if _v.get("client_id") == _cid:
                                pid = str(_k)
                                cfg = cfg or cfg_by_id.get(pid)
                                break
                    except Exception:
                        pass
                out.append({"id": pid, "config": cfg, "cls": cls.__name__})
        except Exception:
            pass
        return out

    async def _handover_plugin_platforms(self, plugin_key: str) -> str:
        """二期：平台适配器实例换手——停旧实例、按新类重挂（零 core 改动）。

        为什么需要：插件重载只换「适配器类注册」和「模块缓存」，AstrBot 里跑着的
        平台实例是加载时创建的，仍持有旧模块的 globals。事件进来找实例、实例认旧
        代码，所以清缓存清不掉它。core 早有正规件：terminate_platform() 摘实例并
        停实例与其任务，load_platform() 按当前 platform_cls_map 重挂新类。

        为何不用 platform_manager.reload()：它在末尾会顺带扫一遍「配置里不存在的
        实例」并逐个 terminate，存在误伤面；这里只用两步最小副作用调用。

        为何必须先重载再换手：反过来会让通道在整个重载期断线，现在只剩重连那一瞬。

        安全：拿不到配置、或配置 enable=false 的，一律跳过——绝不在「停了旧的又挂
        不上新的」的情况下硬来。
        """
        found = self._find_plugin_platforms(plugin_key)
        if not found:
            return ""
        pm = getattr(self.context, "platform_manager", None)
        if pm is None:
            return ""
        done, skipped = [], []
        for item in found:
            pid, cfg, cls_name = item.get("id"), item.get("config"), item.get("cls")
            if not pid or not isinstance(cfg, dict):
                skipped.append(f"{cls_name}（无可用配置，未动手）")
                continue
            if not cfg.get("enable"):
                skipped.append(f"{pid}（配置 enable=false，跳过以免停了挂不回）")
                continue
            try:
                await pm.terminate_platform(str(pid))
                await pm.load_platform(cfg)
                new_cls = "?"
                try:
                    _info = (getattr(pm, "_inst_map", {}) or {}).get(str(pid))
                    if _info and _info.get("inst") is not None:
                        new_cls = type(_info["inst"]).__name__
                    else:
                        for _i in (getattr(pm, "platform_insts", []) or []):
                            _c = getattr(_i, "config", None)
                            if isinstance(_c, dict) and str(_c.get("id") or "") == str(pid):
                                new_cls = type(_i).__name__
                                break
                except Exception:
                    pass
                done.append(f"{pid}（{cls_name} → {new_cls}）")
            except Exception as e:
                skipped.append(f"{pid}（换手失败：{type(e).__name__}: {e}）")
        parts = []
        if done:
            parts.append(f"🔄 平台换手：{len(done)} 个实例已换血 —— {'; '.join(done)}")
            parts.append("   旧实例与其任务已停，新实例按当前代码重挂，通道重建连接中（约 1-3 秒）")
        if skipped:
            parts.append(f"   ⚠️ 跳过 {len(skipped)} 个：{'; '.join(skipped)}")
        return ("\n" + "\n".join(parts)) if parts else ""

    def _collect_stale_plugin_tasks(self, plugin_key: str, max_tasks: int = 200):
        """收集「仍持旧模块代码、且归属本插件」的运行中任务对象（三期回收的靶子）。

        判据与 ④ 自检完全同源，只挑**同时满足**两者的：
          - 归属：任务帧的 __globals__['__file__'] 落在目标插件目录内
          - 陈旧：该 globals 字典不在任何在线模块的 __dict__ 里（旧模块的残骸）
        新任务持当前模块代码，不在靶子内——这是三期不误杀的生命线。

        三重自保（三期是「动手」而不是「只读」，比 ④ 多两道闸）：
          - 绝不包含当前任务（那是我自己这一轮，取消了就把自己杀了）
          - 绝不包含已结束的任务
          - 绝不包含无插件文件痕帧的任务（本插件、core、别家插件自然排除）

        Returns:
            (tasks, labels)：等长列表，tasks 是 asyncio.Task 对象。
        """
        import asyncio as _aio
        import sys as _sys

        key = str(plugin_key or "").strip().lower()
        if not key:
            return [], []
        online = set()
        for _m in list(_sys.modules.values()):
            _f = getattr(_m, "__file__", None)
            _d = getattr(_m, "__dict__", None)
            if _f and _d is not None:
                online.add((_f, id(_d)))
        try:
            _cur = _aio.current_task()
        except Exception:
            _cur = None
        tasks, labels = [], []
        try:
            for _t in list(_aio.all_tasks())[:max_tasks]:
                if _t is _cur or _t.done():
                    continue
                try:
                    seen, node, depth = set(), _t.get_coro(), 0
                    while node is not None and depth < 25:
                        depth += 1
                        fr = getattr(node, "cr_frame", None)
                        while fr is not None:
                            g = fr.f_globals
                            if isinstance(g, dict):
                                _f = str(g.get("__file__") or "").replace("\\", "/")
                                if (
                                    key in _f.lower()
                                    and _f
                                    and (g.get("__file__"), id(g)) not in online
                                ):
                                    loc = f"{_f.split('/')[-1]}:{fr.f_code.co_name}"
                                    if loc not in seen:
                                        seen.add(loc)
                                        tasks.append(_t)
                                        labels.append(loc)
                            fr = fr.f_back
                        node = getattr(node, "cr_await", None)
                except Exception:
                    continue
        except Exception:
            return [], []
        return tasks, labels

    async def _reclaim_plugin_tasks(self, plugin_key: str) -> str:
        """三期：回收陈旧长活任务——对仍跑旧代码的任务发 cancel 并等它收敛。

        为何需要：二期按生命周期停的只有适配器自带任务；插件自己 create_task 起的
        定时器/巡检/重试循环没有登记表，谁也停不掉，旧代码就一直在那儿跑。

        为何安全：只取消「归属本插件 且 仍持旧模块代码」的任务（见
        _collect_stale_plugin_tasks 的三重自保）。cancel 后 await 带 3 秒超时——
        任务若吞掉 CancelledError 不肯退，只记录不纠缠（绝不反复 cancel 或强杀）。
        """
        import asyncio as _aio

        tasks, labels = self._collect_stale_plugin_tasks(plugin_key)
        if not tasks:
            return "\n🧹 任务回收：无需回收（没有仍跑旧代码的插件任务）"
        for _t in tasks:
            try:
                _t.cancel()
            except Exception:
                pass
        try:
            pending = [t for t in tasks if not t.done()]
            if pending:
                await _aio.wait(pending, timeout=3)
        except Exception:
            pass
        cancelled = [lab for t, lab in zip(tasks, labels) if t.done()]
        survivors = [lab for t, lab in zip(tasks, labels) if not t.done()]
        parts = []
        if cancelled:
            parts.append(f"🧹 任务回收：已取消 {len(cancelled)} 个陈旧任务 —— {'; '.join(cancelled)}")
        if survivors:
            parts.append(
                f"   ⚠️ {len(survivors)} 个 3 秒内未退出（可能吞了 CancelledError，只记录不纠缠）：{'; '.join(survivors)}"
            )
        return ("\n" + "\n".join(parts)) if parts else ""

    def _audit_live_refs(self, plugin_key: str) -> str:
        """长活对象自检：找出重载后仍被旧模块代码占据的运行中链路。

        热重载只换「注册表里的绑定」和「sys.modules 里的模块对象」，换不掉已经
        在跑的长活持有者：asyncio 后台任务、已注册的平台适配器实例。它们的函数
        __globals__ 仍指向旧模块的 __dict__，于是事件进来还是执行旧代码——这正是
        2026-09-13 语音事件「重载成功但解析结果不变」的物证。

        本方法只读不写，判据与绑定自检同源：一个 globals 字典若既不属于任何在线
        模块，其 __file__ 又落在目标插件目录内，就是陈旧引用。

        Returns:
            追加进重载回执的可读结论。
        """
        import sys as _sys

        key = str(plugin_key or "").strip().lower()
        if not key:
            return ""

        # 在线模块指纹表：(文件路径, globals字典id)——供陈旧判定
        online = set()
        for _m in list(_sys.modules.values()):
            _f = getattr(_m, "__file__", None)
            _d = getattr(_m, "__dict__", None)
            if _f and _d is not None:
                online.add((_f, id(_d)))

        def _file_of(g) -> str:
            try:
                return str(g.get("__file__") or "")
            except Exception:
                return ""

        def _is_stale(g) -> bool:
            if not isinstance(g, dict):
                return False
            f = _file_of(g)
            if not f:
                return False
            return (f, id(g)) not in online

        def _belongs(g) -> bool:
            return key in _file_of(g).replace("\\", "/").lower()

        def _short(g) -> str:
            return _file_of(g).replace("\\", "/").split("/")[-1] or "?"

        stale_tasks, stale_adapters = [], []

        # ① asyncio 长活任务：沿 cr_await 链 + 帧回溯链收集 f_globals
        try:
            import asyncio as _aio

            for _t in list(_aio.all_tasks())[:200]:
                try:
                    seen, node, depth = set(), _t.get_coro(), 0
                    while node is not None and depth < 25:
                        depth += 1
                        fr = getattr(node, "cr_frame", None)
                        while fr is not None:
                            g = fr.f_globals
                            if _belongs(g) and _is_stale(g):
                                loc = f"{_short(g)}:{fr.f_code.co_name}"
                                if loc not in seen:
                                    seen.add(loc)
                                    stale_tasks.append(loc)
                            fr = fr.f_back
                        node = getattr(node, "cr_await", None)
                except Exception:
                    continue
        except Exception:
            pass

        # ② 已注册的平台适配器实例（挂在平台管理器上，不随插件重载重建）
        try:
            insts = []
            _pm = getattr(self.context, "platform_manager", None)
            for _attr in ("platform_insts", "platforms", "insts"):
                _v = getattr(_pm, _attr, None)
                if isinstance(_v, (list, tuple)):
                    insts = list(_v)
                    break
            for _inst in insts[:30]:
                _cls = type(_inst)
                _hits = []
                for _name, _member in list(vars(_cls).items())[:80]:
                    _f = getattr(_member, "__func__", _member)
                    _g = getattr(_f, "__globals__", None)
                    if isinstance(_g, dict) and _belongs(_g) and _is_stale(_g):
                        _hits.append(f"{_name}@{_short(_g)}")
                if _hits:
                    stale_adapters.append(f"{_cls.__name__}（{', '.join(_hits[:3])}）")
        except Exception:
            pass

        stale_tasks = list(dict.fromkeys(stale_tasks))
        stale_adapters = list(dict.fromkeys(stale_adapters))

        if not stale_tasks and not stale_adapters:
            return "\n🧬 长活对象自检：无陈旧引用（后台任务/平台适配器均指向当前在线模块）"

        parts = []
        if stale_tasks:
            parts.append(f"后台任务×{len(stale_tasks)}（{'; '.join(stale_tasks[:3])}）")
        if stale_adapters:
            parts.append(f"平台适配器×{len(stale_adapters)}")
        logger.warning(f"[插件管理] 长活对象自检发现陈旧引用：{parts}")
        return (
            "\n🧬 长活对象自检：⚠️ 仍持有旧模块代码 " + "、".join(parts)
            + "\n   → 这些链路本次重载不会换血，行为可能仍是旧代码；功能没变就得重启 AstrBot"
        )

    def _snapshot_star(self, plugin_key: str) -> dict:
        """取插件当前快照（版本/是否启用），供重载前后对比"""
        try:
            for s in self.context.get_all_stars():
                if getattr(s, "name", "") == plugin_key:
                    return {
                        "version": getattr(s, "version", None),
                        "activated": bool(getattr(s, "activated", False)),
                    }
        except Exception:
            pass
        return {}

    def _scan_unapplied_core_changes(self, limit: int = 5) -> list:
        """扫 astrbot/core 下「源文件比 .pyc 新」的模块 = 内存里跑的不是这份代码。

        为何不用「文件 mtime vs 进程启动时刻」：本机 /proc/uptime 不可信（实测
        只有 124 秒，而主进程已跑 1 小时 07 分），据此算出的启动时刻偏早约 1.5
        小时，会把早就生效的改动误报成「未生效」—— 误报比不报更坏。

        改用 Python 自己的编译时间戳：模块导入时写 .pyc，源文件若晚于它，说明
        改动发生在导入之后，内存里仍是旧代码，只有重启才带得进去。

        Returns:
            相对 core 目录的路径列表（按源文件 mtime 倒序，最多 limit 条）。
            无 .pyc 可比对的模块跳过（宁可不报，不可误报）。
        """
        import importlib.util as _ilu
        import os as _os

        try:
            import astrbot as _astrbot

            core_dir = _os.path.join(_os.path.dirname(_astrbot.__file__), "core")
        except Exception:
            return []
        if not _os.path.isdir(core_dir):
            return []
        hits = []
        try:
            for root, dirs, files in _os.walk(core_dir):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for fn in files:
                    if not fn.endswith(".py"):
                        continue
                    src = _os.path.join(root, fn)
                    try:
                        pyc = _ilu.cache_from_source(src)
                        if not _os.path.exists(pyc):
                            continue
                        smt = _os.path.getmtime(src)
                        if smt > _os.path.getmtime(pyc):
                            hits.append((smt, _os.path.relpath(src, core_dir)))
                    except OSError:
                        continue
        except Exception:
            return []
        hits.sort(reverse=True)
        return [rel for _mt, rel in hits[:limit]]

    def _tag_freshness(self, plugin_key: str):
        """runtime build 指纹判据（2026-09-16 00:26 假绿防护·硬判据）。

        判据：子模块类必须带 RUNTIME_BUILD_TAG 常量（如 dispatch.py 开头:
        RUNTIME_BUILD_TAG = "build-2026-09-16-0026-r10"）。
        内存 star_cls_type 的 MRO 上类属性 tag 与磁盘同名源文件的 tag 比对，
        不一致即内存跑的是旧类（假绿），必须重启才能换血。
        保守：子模块未打指纹 → 跳过（ok=None），不误报。
        """
        try:
            import inspect as _insp
            import re as _re
            import sys as _sb
            _star_mod = _sb.modules.get("astrbot.core.star.star")
            _map = getattr(_star_mod, "star_map", None)
            if not _map:
                return {"ok": None}
            meta = None
            kl = (plugin_key or "").lower()
            for path, m in _map.items():
                if kl in str(path).lower():
                    meta = m
                    break
            if meta is None:
                return {"ok": None}
            cls = getattr(meta, "star_cls_type", None)
            if cls is None:
                return {"ok": None}
            for klass in _insp.getmro(cls):
                kmod = getattr(klass, "__module__", None)
                if not kmod or not kmod.startswith("data.plugins"):
                    continue
                mem_tag = getattr(klass, "RUNTIME_BUILD_TAG", None)
                if mem_tag is None:
                    continue
                fpath = _insp.getsourcefile(klass)
                if not fpath:
                    continue
                try:
                    with open(fpath, encoding="utf-8") as _fh:
                        disk_src = _fh.read()
                except Exception:
                    continue
                m2 = _re.search(
                    r"RUNTIME_BUILD_TAG\s*=\s*['\"]([^'\"]+)['\"]", disk_src
                )
                if not m2:
                    continue
                if m2.group(1) != mem_tag:
                    return {
                        "ok": False,
                        "stale": [
                            f"{kmod}: RUNTIME_BUILD_TAG 内存={mem_tag} / 磁盘={m2.group(1)}"
                        ],
                    }
            return {"ok": True}
        except Exception as _e:
            return {"ok": None, "err": f"{type(_e).__name__}: {_e}"}

    def _runtime_self_stale(self, plugin_key: str):
        """热重载后「类方法血」运行时验证（2026-09-16 00:02 产品化）。

        背景：2026-09-15 深夜实证三轮（·r6 绿 / ·r7、·r8 未绿），仅凭「模块缓
        存清理 + 绑定自检 + 长活自检全绿」不足以判定插件类方法真的换了血，
        Python 的 sys.modules + star_map 可能在某些 reload 分支留下旧类对象，
        日志全绿、跑的还是旧代码（假绿）。本函数把这些实证抽成**通用判据**，
        不依赖任何插件内置 ·rN 探针，对任意用户插件都适用。

        判据（保守、零误报优先）：对 star_map 中该插件 star_cls_type 的 MRO
        里每一个由插件源码定义的类（抽样前 10 个函数），比较
        内存中 co_firstlineno 与磁盘文件里同名 def 的行号；
        改动了其他位置导致方法行号位移时即可验出「内存仍是旧血」。
        不覆盖"行号不变但方法体内容变了"的极端情形（保守漏检，不误报），
        此类情况由四档生效度外的固定一行提醒兜底。

        返回 {ok: True} / {ok: False, stale: [...]} / {ok: None}（无法判定，不误报）。
        """
        try:
            import inspect as _insp
            import sys as _sb
            logger.info("[runtime_self_stale] 开始换血验证（%s）", plugin_key)
            _star_mod = _sb.modules.get("astrbot.core.star.star")
            _sm = getattr(_star_mod, "star_map", None)
            if not _sm:
                return {"ok": None}
            meta = None
            kl = plugin_key.lower()
            for path, m in _sm.items():
                if kl in str(path).lower():
                    meta = m
                    break
            if meta is None:
                return {"ok": None}
            cls = getattr(meta, "star_cls_type", None)
            if cls is None:
                return {"ok": None}
            stale, scanned = [], 0
            for klass in _insp.getmro(cls):
                if klass in (object, type(None)):
                    continue
                mod = getattr(klass, "__module__", None)
                if not mod or not mod.startswith("data.plugins"):
                    continue
                try:
                    fpath = _insp.getsourcefile(klass)
                    if not fpath:
                        continue
                    with open(fpath, encoding="utf-8") as _fh:
                        disk_lines = _fh.readlines()
                except Exception:
                    continue
                for name, obj in list(vars(klass).items()):
                    if scanned >= 60:
                        break
                    if not _insp.isfunction(obj) or obj.__module__ != mod:
                        continue
                    scanned += 1
                    try:
                        mem_no = obj.__code__.co_firstlineno
                    except Exception:
                        continue
                    disk_no = None
                    pat = f"def {name}("
                    for i, line in enumerate(disk_lines, start=1):
                        if pat in line:
                            disk_no = i
                            break
                    if disk_no is None or disk_no != mem_no:
                        stale.append(
                            f"{mod}: {name}（内存首行 {mem_no} / 磁盘 {disk_no}）"
                        )
                if len(stale) >= 2 or scanned >= 10:
                    break
            if scanned == 0:
                return {"ok": None}
            if stale:
                return {"ok": False, "stale": stale[:5]}
            logger.info(
                "[runtime_self_stale] 换血验证通过：%d 方法/磁盘对齐（%s）",
                scanned, plugin_key,
            )
            return {"ok": True, "scanned": scanned}
        except Exception as _rt_e:
            return {"ok": None, "err": f"{type(_rt_e).__name__}: {_rt_e}"}

    def _grade_reload_effect(self, success, live_note, core_stale, plugin_key=None) -> str:
        """把「这次重载到底生效到哪一层」写成一行结论（一期：生效度评分）。

        分层判据（与规划文档 L0-L4 对应）：
          L0/L1 模块与绑定：重载本身失败即未生效
          L2    长活对象：  有陈旧引用即部分生效（二期换手能力的靶子）
          L4    进程级：    core 有未进内存的改动，任何插件重载都救不了
        """
        stale_lines = bool(core_stale)
        live_dirty = bool(live_note) and "⚠️" in live_note
        if not success:
            head = "🧭 生效度：❌ 未生效（重载本身失败，见上方错误）"
        elif stale_lines:
            head = "🧭 生效度：⚠️ 插件层面已换血，但底层有改动没进内存"
        elif live_dirty:
            head = "🧭 生效度：◐ 部分生效——注册表已换血，长活链路仍持旧代码"
        else:
            head = "🧭 生效度：✅ 完全生效（模块/绑定/长活对象均指向当前代码）"
        out = [head]
        # ── 2026-09-16 第五层：运行时类方法换血验证（假绿防护） ──
        # 兼容缺省 plugin_key：只有显式指定插件重载时才做，全量重载不做（怕拖慢回执）。
        if plugin_key and success:
            _rt = self._tag_freshness(plugin_key)
            if _rt.get("ok") is None:
                _rt = self._runtime_self_stale(plugin_key)
            if _rt.get("ok") is False:
                head = (
                    "🧭 生效度：⚠️ 部分生效——模块/绑定已换血，但运行时类方法仍是旧版本"
                    "（即「假绿」，运行时验证未通过）"
                )
                out = [head]
                out.append("🚨 必须重启：本次热重载没能把这些类方法换血成功：")
                for _s in _rt["stale"]:
                    out.append(f"    · {_s}")
                out.append(
                    "🤖 请把这份结论原样通知用户/AI 操作者：插件已热重载但部分类方法"
                )
                out.append(
                    "    使用旧代码，需要手动重启 AstrBot 后再重载一次，才能真正生效。"
                )
                return "\n" + "\n".join(out)
        if stale_lines:
            out.append(
                "🚨 必须重启：astrbot/core 下有改动晚于本进程启动时刻，热重载带不进去——"
                f"{', '.join(core_stale[:3])}"
                + (f" 等 {len(core_stale)} 处" if len(core_stale) > 3 else "")
            )
        elif live_dirty:
            out.append("   长活链路要彻底换血，等二期换手能力上线（当前可暂不重启）")
        return "\n" + "\n".join(out)

    def _format_reload_receipt(self, plugin_key, target_all, success, error_message,
                               elapsed, before, after, log_lines, purged_count=0) -> str:
        """把一次热重载的结果整理成自证回执，省去事后翻日志"""
        label = "全部插件" if target_all else (plugin_key or "未知插件")
        out = [f"{'✅ 热重载成功' if success else '❌ 热重载失败'} · {label}"]
        meta = [f"耗时 {elapsed:.2f}s"]
        if not target_all:
            v0, v1 = before.get("version"), after.get("version")
            if v0 or v1:
                meta.append(f"版本 {v0 or '—'} → {v1 or '—'}")
            if after:
                meta.append("状态 🟢 已加载" if after.get("activated") else "状态 ⚪ 已禁用")
            else:
                meta.append("状态 ⚠️ 未在注册表中")
        if purged_count:
            meta.append(f"模块缓存清理 {purged_count} 个")
        else:
            out.append(f"模块缓存：{getattr(self, '_last_purge_diag', '') or '无匹配'}")
        meta.append(f"日志 {len(log_lines)} 行")
        out.append("｜".join(meta))
        if error_message:
            out.append(f"错误：{str(error_message)[:300]}")

        key_lines = []
        for level, msg in log_lines:
            low = msg.lower()
            if any(h in low for h in _RELOAD_LOG_HINTS):
                key_lines.append(f"[{level}] {msg.strip()[:160]}")
        out.append(f"关键日志 {len(key_lines)} 条：")
        if key_lines:
            for line in key_lines[-8:]:
                out.append("  · " + line)
        else:
            out.append("  · （无命中，建议人工复核）")
        return "\n".join(out)

    @filter.llm_tool(name="plugin_list")
    async def _llm_plugin_list(self, event: AstrMessageEvent) -> str:
        """列出 AstrBot 当前已加载的所有插件及其启用状态。

        Returns:
            str: 插件列表文本。
        """
        try:
            stars = self.context.get_all_stars()
        except Exception as e:
            return f"错误：{e}"
        if not stars:
            return "当前没有加载任何插件。"
        lines = [f"共 {len(stars)} 个插件："]
        for s in stars:
            if getattr(s, "reserved", False):
                continue
            status = "🟢" if s.activated else "⚪"
            ver = f" v{s.version}" if getattr(s, "version", None) else ""
            lines.append(f"{status} {s.name}{ver}")
        return "\n".join(lines)

# ─────────────────────────────────────────
    # 热更新子代理配置（orchestrator + persona 联动）
    # ─────────────────────────────────────────

    def _get_persona_mgr(self):
        m = getattr(self.context, "persona_manager", None)
        if m is None:
            raise RuntimeError("persona_manager 不可用")
        return m

    def _get_orch_config(self) -> dict:
        cfg = dict(self.context._config.get("subagent_orchestrator", {}) or {})
        cfg.setdefault("main_enable", False)
        cfg.setdefault("remove_main_duplicate_tools", False)
        cfg.setdefault("agents", [])
        return cfg

    async def _ensure_persona(self, persona_id, persona_prompt=None, description=None):
        """确保 persona 存在于内存/DB，不存在则创建（可带 prompt）。返回 (existed, persona)"""
        # 支持 file: 前缀从文件读取 prompt——大 prompt 走 LLM 工具参数会被截断，必须走文件
        if persona_prompt and persona_prompt.startswith("file:"):
            _fp = persona_prompt[5:].strip()
            try:
                with open(_fp, "r", encoding="utf-8") as _f:
                    persona_prompt = _f.read()
            except OSError as _e:
                raise ValueError(f"无法读取 prompt 文件 {_fp}: {_e}")
        mgr = self._get_persona_mgr()
        existing = mgr.get_persona_v3_by_id(persona_id) if persona_id else None
        # v3 可能查不到但 DB 里有，再兜底一次
        if existing is None and persona_id:
            try:
                allp = await mgr.get_all_personas()
                for p in allp:
                    if p.persona_id == persona_id:
                        existing = True
                        break
            except Exception:
                pass
        if existing is not None:
            # persona_prompt 提供且与现值不同 → 更新既有人格（否则改人格只能靠删了重建）
            if persona_prompt:
                cur_prompt = None
                _diag = []
                try:
                    allp = await mgr.get_all_personas()
                    _diag.append(f"allp={len(allp)}")
                    for p in allp:
                        if p.persona_id == persona_id:
                            cur_prompt = getattr(p, "system_prompt", None)
                            if cur_prompt is None and isinstance(p, dict):
                                cur_prompt = p.get("system_prompt")
                            _diag.append(f"matched len={len(cur_prompt) if cur_prompt else 0}")
                            break
                except Exception as _e:
                    _diag.append(f"ERR {_type_err := type(_e).__name__}:{_e}")
                logger.info(f"[子代理热更新] 更新人格诊断 {persona_id}: {'; '.join(_diag)}; new_len={len(persona_prompt)}; will_update={cur_prompt is not None and cur_prompt != persona_prompt}")
                if cur_prompt is not None and cur_prompt != persona_prompt:
                    await mgr.update_persona(persona_id, system_prompt=persona_prompt)
                    from astrbot.api import logger as _lg2
                    _lg2.info(f"[子代理热更新] 已更新人格 {persona_id} 的 system_prompt")
            return False, existing
        # 创建
        prompt = persona_prompt or description or f"你是{persona_id}。"
        from astrbot.api import logger as _lg
        try:
            p = await mgr.create_persona(
                persona_id, prompt, begin_dialogs=None, tools=None, skills=None
            )
            _lg.info(f"[子代理热更新] 已创建人格 {persona_id}")
            return True, p
        except ValueError:
            # 已存在（并发/DB有但内存缺），再查一次
            return False, mgr.get_persona_v3_by_id(persona_id)

    @filter.llm_tool(name="update_subagent")
    async def _llm_update_subagent(self, event, action: str, name: str = "",
                                   persona_id: str = "",
                                   public_description: str = "",
                                   provider_id: str = "",
                                   persona_prompt: str = "",
                                   config: dict = None) -> str:
        """热更新子代理（orchestrator + persona 联动）：
           action=upsert 新增/更新子代理；remove 移除；list 列出。
           成功后即时生效，无需重启。

        Args:
            action (string): upsert | remove | list
            name (string): 子代理英文 id，如 agent_a
            persona_id (string): 中文人格名（默认同 name）。upsert 时若该人格不存在会自动创建。
            public_description (string): 主代理路由描述（子代理用途/触发场景，给路由判断用）。
            provider_id (string): 该子代理用的模型提供商 id（如 opencode-go/mimo-v2.5）。留空用默认。
            persona_prompt (string): upsert 时写入系统人格 prompt。人格不存在则新建；已存在且内容有变化则更新（可借此修改人格设定）。
            config (dict): 可选，额外 orchestrator 字段（如 enabled=false 禁用、tools 列表等）。
        """
        if not self.config.get("enable_subagent_tools", True):
            return "子代理工具已在插件配置中关闭（enable_subagent_tools），可在插件配置页开启"
        try:
            allowed = event.is_admin() if hasattr(event, "is_admin") else False
        except Exception:
            allowed = False
        if not (allowed or _is_owner(event, self.config.get("owner_uids"))):
            return "权限不足：仅管理员或配置的所有者可调用"

        act = str(action).strip().lower()
        name = str(name).strip()
        orch = getattr(self.context, "subagent_orchestrator", None)
        if orch is None:
            return "错误：subagent_orchestrator 未初始化"

        if act == "list":
            cfg = self._get_orch_config()
            agents = cfg.get("agents", [])
            if not agents:
                return "当前没有配置任何子代理"
            lines = [f"已配置 {len(agents)} 个子代理："]
            for a in agents:
                if not isinstance(a, dict):
                    continue
                st = "🟢" if a.get("enabled", True) else "⚪"
                lines.append(f"{st} {a.get('name')} (persona={a.get('persona_id')}, provider={a.get('provider_id')})")
            return "\n".join(lines)

        if not name:
            return "错误：name 不能为空"

        cfg = self._get_orch_config()
        agents = cfg.get("agents", [])
        if not isinstance(agents, list):
            agents = []

        if act == "remove":
            before = len(agents)
            agents = [a for a in agents if not (isinstance(a, dict) and a.get("name") == name)]
            if len(agents) == before:
                return f"未找到子代理「{name}」，无需移除"
            cfg["agents"] = agents
            self.context._config["subagent_orchestrator"] = cfg
            self.context._config.save_config()
            await orch.reload_from_config(cfg)
            return f"✅ 已移除子代理「{name}」并即时生效"

        if act != "upsert":
            return f"错误：未知 action「{act}」（应为 upsert/remove/list）"

        pid = persona_id or name
        # 确保人格存在
        created, _p = await self._ensure_persona(
            pid, persona_prompt=persona_prompt or None,
            description=public_description or None,
        )

        entry = {
            "name": name,
            "persona_id": pid,
            "public_description": public_description or "",
            "provider_id": provider_id or "opencode-go/mimo-v2.5",
            "enabled": True,
        }
        if isinstance(config, dict):
            # 合并 extra 字段（enabled/tools/remove... 仅保留合法键，避免污染）
            for k in ("enabled", "tools", "remove_main_duplicate_tools"):
                if k in config:
                    entry[k] = config[k]

        # upsert：按 name 匹配更新，否则追加
        replaced = False
        for i, a in enumerate(agents):
            if isinstance(a, dict) and a.get("name") == name:
                merged = dict(a)
                merged.update(entry)
                # persona_prompt 无意义时不覆盖既有 public_description
                agents[i] = merged
                replaced = True
                break
        if not replaced:
            agents.append(entry)

        cfg["agents"] = agents
        self.context._config["subagent_orchestrator"] = cfg
        self.context._config.save_config()
        await orch.reload_from_config(cfg)

        who = "创建人格并" if created else ""
        act_word = "更新" if replaced else "新增"
        return f"✅ 已{act_word}子代理「{name}」（{who}persona={pid}），即时生效"
    # ── 子代理三件套：体检 / 快照 / 试音 ──

    def _snapshot_dir(self) -> str:
        import os as _os
        return _os.path.abspath(
            _os.path.join("data", "plugin_data", "astrbot_plugin_auto_reload", "subagent_snapshots")
        )

    def _rt_handoff_map(self) -> dict:
        """运行时 handoff 映射：{子代理名: HandoffTool}"""
        orch = getattr(self.context, "subagent_orchestrator", None)
        if orch is None:
            return {}
        out = {}
        for h in list(getattr(orch, "handoffs", []) or []):
            _n = getattr(h, "name", "") or ""
            if _n.startswith("transfer_to_"):
                out[_n[len("transfer_to_"):]] = h
        return out

    @filter.llm_tool(name="subagent_health")
    async def _llm_subagent_health(self, event, name: str = "") -> str:
        """子代理运行时体检：对照「编制表 / 运行时实例 / 人格源 / provider」四层，抓串台与旧代码。

        Args:
            name (string): 要体检的子代理名。留空则体检全部。
        """
        if not self.config.get("enable_subagent_tools", True):
            return "子代理工具已在插件配置中关闭（enable_subagent_tools），可在插件配置页开启"
        try:
            allowed = event.is_admin() if hasattr(event, "is_admin") else False
        except Exception:
            allowed = False
        if not (allowed or _is_owner(event, self.config.get("owner_uids"))):
            return "权限不足：仅管理员或配置的所有者可调用"

        pm = self._get_persona_mgr()
        cfg = self._get_orch_config()
        agents = [a for a in cfg.get("agents", []) if isinstance(a, dict)]
        rt = self._rt_handoff_map()
        if name:
            agents = [a for a in agents if a.get("name") == name]
            if not agents:
                return f"未找到子代理「{name}」"

        lines, bad = [], 0
        for a in agents:
            n = str(a.get("name", "")).strip()
            pid = str(a.get("persona_id", "") or "").strip()
            prov = str(a.get("provider_id", "") or "").strip()
            enabled = bool(a.get("enabled", True))
            issues = []

            h = rt.get(n)
            if enabled and h is None:
                issues.append("在编但未挂载到运行时（可能人格缺失或 reload 未执行）")
            persona = None
            if pid:
                try:
                    persona = pm.get_persona_v3_by_id(pid)
                except Exception:
                    persona = None
                if persona is None:
                    issues.append(f"人格「{pid}」不存在（运行时回落 inline prompt，串台高危）")
            if h is not None:
                # 运行时指令 vs 人格源：不一致 = 改了人格没 reload（旧代码在跑）
                if persona is not None:
                    src_prompt = str(persona.get("prompt", "") or "").strip()
                    rt_prompt = str(getattr(h.agent, "instructions", "") or "").strip()
                    if src_prompt and rt_prompt and src_prompt != rt_prompt:
                        issues.append("运行时 instructions 与人格源不一致（改人格后未重载，旧代码在跑）")
                _pv = str(getattr(h, "provider_id", "") or "").strip()
                if prov and _pv != prov:
                    issues.append(f"运行时 provider({_pv or '默认'}) 与编制表({prov}) 不一致")
            if prov:
                try:
                    _p = await self.context.provider_manager.get_provider_by_id(prov)
                except Exception:
                    _p = None
                if _p is None:
                    issues.append(f"provider「{prov}」不存在或不可用")

            n_tools = len(getattr(h.agent, "tools", None) or []) if h is not None else 0
            mark = "⚠️" if issues else "✅"
            if issues:
                bad += 1
            head = f"{mark} {n} (persona={pid or '—'}, provider={prov or '默认'}, tools={n_tools}{'' if enabled else '，已禁用'})"
            lines.append(head)
            for it in issues:
                lines.append(f"   └ {it}")
            if not issues:
                lines.append("   └ 编制/运行时/人格源/provider 四层一致")

        total = len(agents)
        summary = f"\n📊 体检完成：{total - bad}/{total} 健康" if total else "无在编子代理"
        return "\n".join(lines) + (f"\n{summary}" if total else "")

    @filter.llm_tool(name="subagent_snapshot")
    async def _llm_subagent_snapshot(self, event, action: str, snapshot_name: str = "") -> str:
        """子代理编制快照：备份/列出/回滚「orchestrator 配置 + 相关人格」全量状态。

        Args:
            action (string): save 保存当前状态快照 | list 列出历史快照 | rollback 回滚到指定快照
            snapshot_name (string): rollback 时指定快照名（从 list 输出取）。save 时留空自动按时间命名。
        """
        if not self.config.get("enable_subagent_tools", True):
            return "子代理工具已在插件配置中关闭（enable_subagent_tools），可在插件配置页开启"
        try:
            allowed = event.is_admin() if hasattr(event, "is_admin") else False
        except Exception:
            allowed = False
        if not (allowed or _is_owner(event, self.config.get("owner_uids"))):
            return "权限不足：仅管理员或配置的所有者可调用"

        import json as _json
        import os as _os
        from datetime import datetime as _dt

        act = str(action).strip().lower()
        d = self._snapshot_dir()
        _os.makedirs(d, exist_ok=True)

        if act == "list":
            files = sorted(f for f in _os.listdir(d) if f.endswith(".json"))
            if not files:
                return "暂无快照"
            lines = [f"共 {len(files)} 份快照（最新在上）："]
            for f in reversed(files[-15:]):
                p = _os.path.join(d, f)
                try:
                    data = _json.load(open(p, encoding="utf-8"))
                    lines.append(
                        f"  {f[:-5]}  agents={len(data.get('orch', {}).get('agents', []))}"
                        f"  personas={len(data.get('personas', {}))}"
                    )
                except Exception:
                    lines.append(f"  {f[:-5]}  （文件损坏，跳过）")
            return "\n".join(lines)

        if act == "save":
            cfg = self._get_orch_config()
            pm = self._get_persona_mgr()
            personas = {}
            for a in cfg.get("agents", []):
                if not isinstance(a, dict):
                    continue
                pid = str(a.get("persona_id", "") or "").strip()
                if not pid or pid in personas:
                    continue
                try:
                    p = pm.get_persona_v3_by_id(pid)
                except Exception:
                    p = None
                if p is not None:
                    personas[pid] = {
                        "prompt": p.get("prompt", ""),
                        "begin_dialogs": p.get("begin_dialogs"),
                        "tools": p.get("tools"),
                        "skills": p.get("skills"),
                        "custom_error_message": p.get("custom_error_message"),
                    }
            snap = {
                "created_at": _dt.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "orch": cfg,
                "personas": personas,
            }
            fname = snapshot_name or _dt.now().strftime("snapshot_%Y%m%d_%H%M%S")
            if not fname.endswith(".json"):
                fname += ".json"
            path = _os.path.join(d, fname)
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(snap, f, ensure_ascii=False, indent=2)
            return (
                f"✅ 快照已保存：{fname[:-5]}\n"
                f"   子代理 {len(cfg.get('agents', []))} 个、人格 {len(personas)} 个已入档"
            )

        if act == "rollback":
            if not snapshot_name:
                return "错误：rollback 需要指定 snapshot_name（从 list 输出取）"
            fname = snapshot_name if snapshot_name.endswith(".json") else snapshot_name + ".json"
            path = _os.path.join(d, fname)
            if not _os.path.isfile(path):
                return f"错误：快照「{snapshot_name}」不存在"
            try:
                snap = _json.load(open(path, encoding="utf-8"))
            except Exception as e:
                return f"错误：快照文件损坏（{e}）"
            orch_cfg = snap.get("orch") or {}
            personas = snap.get("personas") or {}
            pm = self._get_persona_mgr()
            restored, skipped = 0, []
            for pid, p in personas.items():
                try:
                    cur = pm.get_persona_v3_by_id(pid)
                except Exception:
                    cur = None
                if cur is None:
                    skipped.append(pid)
                    continue
                await pm.update_persona(
                    pid,
                    system_prompt=p.get("prompt") or None,
                    begin_dialogs=p.get("begin_dialogs"),
                    tools=p.get("tools") if p.get("tools") is not None else False,
                )
                restored += 1
            self.context._config["subagent_orchestrator"] = orch_cfg
            self.context._config.save_config()
            orch = getattr(self.context, "subagent_orchestrator", None)
            reloaded = "已重载运行时"
            if orch is not None:
                await orch.reload_from_config(orch_cfg)
            msg = (
                f"✅ 已回滚到「{snapshot_name}」：编制表 {len(orch_cfg.get('agents', []))} 个子代理，"
                f"人格恢复 {restored} 个，{reloaded}"
            )
            if skipped:
                msg += f"\n⚠️ {len(skipped)} 个人格当前不存在，已跳过：{'; '.join(skipped)}"
            return msg

        return f"错误：未知 action「{act}」（应为 save/list/rollback）"

    @filter.llm_tool(name="subagent_test")
    async def _llm_subagent_test(self, event, name: str, message: str) -> str:
        """子代理试音台：用该子代理的人格与 provider 直接发一条测试消息，拿回原始回复（不转发给用户）。调人格时改一段试一段。

        Args:
            name (string): 子代理名。
            message (string): 发给子代理的测试消息。
        """
        if not self.config.get("enable_subagent_tools", True):
            return "子代理工具已在插件配置中关闭（enable_subagent_tools），可在插件配置页开启"
        try:
            allowed = event.is_admin() if hasattr(event, "is_admin") else False
        except Exception:
            allowed = False
        if not (allowed or _is_owner(event, self.config.get("owner_uids"))):
            return "权限不足：仅管理员或配置的所有者可调用"

        import asyncio as _aio
        from datetime import datetime as _dt

        name = str(name).strip()
        if not name or not str(message).strip():
            return "错误：name 与 message 均不能为空"
        h = self._rt_handoff_map().get(name)
        if h is None:
            return f"错误：子代理「{name}」未挂载到运行时（先跑 subagent_health 看体检）"
        instructions = str(getattr(h.agent, "instructions", "") or "").strip()
        if not instructions:
            return f"错误：子代理「{name}」运行时 instructions 为空，无法试音"

        prov_id = str(getattr(h, "provider_id", "") or "").strip()
        try:
            if prov_id:
                provider = await self.context.provider_manager.get_provider_by_id(prov_id)
            else:
                provider = self.context.provider_manager.get_using_provider()
        except Exception as e:
            return f"错误：获取 provider 失败（{e}）"
        if provider is None:
            return f"错误：provider「{prov_id or '默认'}」不可用"

        session_id = f"subagent_test_{name}_{_dt.now().strftime('%Y%m%d%H%M%S')}"
        try:
            completion = await _aio.wait_for(
                provider.text_chat(
                    prompt=str(message),
                    session_id=session_id,
                    system_prompt=instructions,
                ),
                timeout=60,
            )
        except _aio.TimeoutError:
            return f"⏱ 试音超时（60s）：{name} 的 provider「{prov_id or '默认'}」响应过慢"
        except Exception as e:
            return f"错误：试音请求失败（{type(e).__name__}: {e}）"
        text = str(getattr(completion, "completion_text", "") or "").strip()
        if not text:
            text = "（空回复）"
        shown = text if len(text) <= 600 else text[:600] + f"…（共 {len(text)} 字）"
        return f"🎤 {name} 试音回复（provider={prov_id or '默认'}）：\n{shown}"

    async def terminate(self):
        logger.info("[自动重载] 插件已卸载")
