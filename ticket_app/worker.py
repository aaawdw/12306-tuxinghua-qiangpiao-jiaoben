from __future__ import annotations

import asyncio
import copy
import tempfile
import threading
from pathlib import Path

from .browser import BrowserAdapter
from .config import ROOT, query_url
from .demo import DEMO_CONFIG, DemoServer
from .engine import Control, Handoff, Journal, Stopped, run


class Worker(threading.Thread):
    """All Playwright calls stay in this thread; Tk receives queue messages only."""
    def __init__(self, config, mode, events, *, now=False, local=None, headless=False):
        super().__init__(daemon=True)
        self.config = copy.deepcopy(config)
        self.mode, self.events, self.now = mode, events, now
        self.local = Path(local) if local else ROOT / ".local"
        self.control = Control()
        self.headless = headless

    def emit(self, kind, value):
        self.events.put((kind, value))

    async def recover_previous_attempt(self, guard, page):
        if not guard.path.exists():
            return
        previous = guard.path.read_bytes()
        self.control.recover.clear()
        self.emit("state", "recovery")
        self.emit("log", "发现本地提交记录，尚未开始查询。它不代表订单已成功；浏览器已保留供你核对。")
        self.emit("log", "请在 12306 检查未完成订单、已支付订单及排队状态。确认本行程无需付款、未出票且没有排队后，点击“已核对无订单，继续”。")
        while not self.control.recover.is_set():
            if page.is_closed():
                raise Stopped()
            await self.control.sleep(0.1)
        await self.control.checkpoint()
        guard.archive_after_review(previous)
        self.emit("log", "已按你的核对结果归档本行程旧记录，准备重新查询。其他行程记录保留。")

    def run(self):
        try:
            asyncio.run(self.work())
        except Exception:
            self.emit("log", "浏览器会话意外结束，请检查未完成订单。")
            self.emit("result", "handoff")
        finally:
            self.emit("closed", None)

    async def work(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.emit("log", "缺少 Playwright。在 PyCharm 终端运行：python -m pip install -r requirements.txt")
            self.emit("result", "handoff")
            return
        self.local.mkdir(parents=True, exist_ok=True)
        lock_path = self.local / "run.lock"
        try:
            lock = lock_path.open("x", encoding="utf-8")
        except FileExistsError:
            self.emit("log", "已有运行实例或残留锁文件。先关闭旧程序并检查未完成订单，再按 README 的恢复说明处理 .local/run.lock。")
            self.emit("result", "handoff")
            return
        context = None
        demo = None
        temp = None
        try:
            import os
            lock.write(str(os.getpid()))
            lock.flush()
            config = copy.deepcopy(DEMO_CONFIG) if self.mode == "demo" else self.config
            if self.mode == "demo":
                config["browser"] = self.config.get("browser", "chrome")
                config["seatPosition"] = self.config.get("seatPosition", "自动分配")
            guard = Journal(self.local / "attempts", config)
            async with async_playwright() as playwright:
                self.emit("state", "opening")
                if self.mode == "demo":
                    demo = DemoServer()
                    profiles = self.local / "demo-profiles"
                    profiles.mkdir(exist_ok=True)
                    temp = tempfile.TemporaryDirectory(prefix="session-", dir=profiles, ignore_cleanup_errors=True)
                profile = temp.name if temp else str(self.local / "browser-profile")
                try:
                    context = await playwright.chromium.launch_persistent_context(
                        profile, channel=None if config["browser"] == "chromium" else config["browser"],
                        headless=self.headless, no_viewport=True, locale="zh-CN", timezone_id="Asia/Shanghai")
                except Exception:
                    raise Handoff("无法启动浏览器。请检查是否安装所选 Chrome / Edge、是否有旧窗口占用会话。选择 Chromium 时需先运行 python -m playwright install chromium。") from None
                page = context.pages[0] if context.pages else await context.new_page()
                adapter = BrowserAdapter(page, config, self.control, demo=bool(demo), emit=self.emit)
                try:
                    if demo:
                        async def restrict(route):
                            if route.request.url.startswith(demo.url + "/"):
                                await route.continue_()
                            else:
                                await route.abort()
                        await context.route("**/*", restrict)
                        await page.goto(demo.url)
                        self.emit("log", "本地模拟：全部使用虚构数据，不连接 12306，不产生真实订单。")
                    else:
                        await page.goto("https://kyfw.12306.cn/otn/resources/login.html", wait_until="domcontentloaded")
                        self.emit("state", "login")
                        self.emit("log", "请在浏览器手动登录并检查未完成订单，完成后点击“已登录，继续”。")
                        while not self.control.login.is_set():
                            if page.is_closed():
                                raise Stopped()
                            await self.control.sleep(0.1)
                        await self.control.checkpoint()
                        if self.mode == "live":
                            await self.recover_previous_attempt(guard, page)
                        await page.goto(query_url(config), wait_until="domcontentloaded")
                        await page.locator("input#fromStationText").wait_for()
                        await adapter.verify_query()
                    class DemoGuard:
                        def assert_clear(self): pass
                        def arm(self, _offer): pass
                    result = await run(config, adapter, DemoGuard() if demo else guard, self.control, self.emit,
                                       dry_run=self.mode == "dry_run", now=bool(demo) or self.now)
                    if demo:
                        self.emit("log", f'模拟记录：提交 {demo.records["submits"]} 次，支付点击 {demo.records["payments"]} 次。')
                    self.emit("result", result)
                except Stopped:
                    self.emit("log", "已停止自动操作，浏览器保留。")
                    self.emit("result", "stopped")
                except Exception as exc:
                    self.emit("log", str(exc) if isinstance(exc, Handoff) else "页面加载或操作失败，请检查网络与浏览器。自动操作已停止。")
                    self.emit("result", "handoff")
                # Retain Playwright and the browser for manual payment until explicitly closed.
                while not self.control.close.is_set() and not page.is_closed():
                    await asyncio.sleep(0.2)
                await context.close()
                context = None
        except Handoff as exc:
            self.emit("log", str(exc))
            self.emit("result", "handoff")
        finally:
            if context:
                try:
                    await context.close()
                except Exception:
                    pass
            if demo:
                await asyncio.to_thread(demo.close)
            if temp:
                temp.cleanup()
            lock.close()
            lock_path.unlink(missing_ok=True)
