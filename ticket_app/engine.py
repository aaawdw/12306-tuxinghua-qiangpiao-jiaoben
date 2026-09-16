from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from uuid import uuid4
from datetime import datetime, timezone
from pathlib import Path

from .config import choose_offer, journey_key


class Handoff(Exception):
    """A readable, sanitized reason to leave the browser with the user."""


class Stopped(Exception):
    pass


class Control:
    def __init__(self):
        self.stop = threading.Event()
        self.paused = threading.Event()
        self.login = threading.Event()
        self.recover = threading.Event()
        self.close = threading.Event()

    async def checkpoint(self):
        while self.paused.is_set() and not self.stop.is_set() and not self.close.is_set():
            await asyncio.sleep(0.1)
        if self.stop.is_set() or self.close.is_set():
            raise Stopped()

    async def sleep(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            await self.checkpoint()
            await asyncio.sleep(min(0.1, max(0, end - time.monotonic())))
        await self.checkpoint()

    async def wait_until(self, timestamp):
        # Re-read wall time so changes to the system clock don't leave a stale
        # deadline. Keep the final two seconds responsive without busy-waiting.
        while True:
            await self.checkpoint()
            remaining = timestamp - time.time()
            if remaining <= 0:
                return
            await self.sleep(min(0.5 if remaining > 2 else 0.02, remaining))


class Journal:
    def __init__(self, folder: Path, config: dict):
        self.path = folder / (journey_key(config) + ".json")

    def assert_clear(self):
        if self.path.exists():
            raise Handoff("这个行程有本地提交记录，不代表已出票。请重新开始，在浏览器核对订单后使用“已核对无订单，继续”恢复。")

    def archive_after_review(self, expected: bytes):
        """Called under the worker's run lock after explicit order review."""
        if not self.path.exists() or self.path.read_bytes() != expected:
            raise Handoff("核对期间提交记录已变化，已停止恢复，请重新检查订单。")
        archive = self.path.parent / "archive"
        archive.mkdir(exist_ok=True)
        target = archive / f"{self.path.stem}-{uuid4().hex}.json"
        self.path.rename(target)
        return target

    def arm(self, offer):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("x", encoding="utf-8") as f:
                json.dump({"status": "submission-started", "at": datetime.now(timezone.utc).isoformat(),
                           "train": offer["train"], "seat": offer["seat"]}, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
        except FileExistsError:
            raise Handoff("检测到已有提交记录，已阻止重复下单。") from None


async def run(c, adapter, guard, control: Control, emit, *, dry_run=False, now=False):
    submitted = False
    try:
        if not dry_run:
            guard.assert_clear()
        start = time.time() if dry_run or now else datetime.fromisoformat(c["saleAt"]).timestamp()
        if not dry_run and not now and start < time.time() - 60:
            raise Handoff("开售时间已过去，请修改时间，或勾选“立即开始”再运行。")
        if start > time.time():
            emit("state", "waiting")
            emit("log", "登录和行程已核对，等待开售。")
            if start - time.time() > 5:
                await control.wait_until(start - 5)
                await adapter.prepare_for_sale()
                emit("log", "开售前检查通过，保持当前页面，等待准点首查。")
            await control.wait_until(start)
        sprint_end = start + 30 if not dry_run and not now and c.get('fastStart', True) else 0
        interval = c['pollIntervalMs'] / 1000
        fast_interval = min(1, interval)
        if sprint_end > time.time():
            emit("log", f"到达开售时间，立即首查；开售后 30 秒内每轮完成后等待 {fast_interval:g} 秒，实际周期另加网站响应时间。")
        end = time.monotonic() + c["maxRunMinutes"] * 60
        failures = 0
        first_query = True
        sprint_finished = False
        while time.monotonic() < end:
            await control.checkpoint()
            if first_query and not dry_run and not now:
                emit("log", f"首轮查询开始：距设定开售时间 {max(0, (time.time() - start) * 1000):.0f} 毫秒（本机计时）。")
            first_query = False
            emit("state", "querying")
            try:
                offers = await adapter.query()
                failures = 0
            except (Handoff, Stopped):
                raise
            except Exception as exc:
                failures += 1
                if failures >= 3:
                    raise Handoff("连续三次查询失败，已停止自动查询，请检查浏览器。") from None
                reason = "等待网站响应超时" if type(exc).__name__ == "TimeoutError" else "网站响应或结果读取失败"
                emit("log", f"{reason}（{type(exc).__name__}），{failures * 10} 秒后重试查询。")
                await control.sleep(max(c["pollIntervalMs"] / 1000, failures * 10))
                continue
            offer = choose_offer(c, offers)
            if dry_run:
                emit("log", f'演练匹配：{offer["train"]} / {offer["seat"]}。没有点击预订。' if offer else "查询演练完成，目前没有符合条件的余票。没有点击预订。")
                return "dry_run"
            if offer:
                emit("state", "ordering")
                emit("log", f'匹配 {offer["train"]} / {offer["seat"]}，正在核对订单。')
                await control.checkpoint()
                await adapter.book(offer)
                emit("log", "已识别乘客确认页，正在选择乘车人并核对行程、票种和席别。")
                await control.checkpoint()
                await adapter.fill_order(offer)
                emit("log", "乘车人、行程和席别核对通过，准备提交订单。")
                await control.checkpoint()
                guard.arm(offer)
                submitted = True
                await adapter.submit()
                emit("state", "queueing")
                await adapter.wait_for_payment()
                emit("log", "已到待支付页，自动操作停止。请在网站规定时间内核对并付款。")
                return "payment"
            emit("log", "本轮没有符合条件的余票。")
            if time.time() < sprint_end:
                # Keep one outstanding query at a time. A slower response never
                # triggers catch-up requests, and faster user settings survive.
                delay = fast_interval
            else:
                delay = interval
                if sprint_end and not sprint_finished:
                    emit("log", f"开售快速查询阶段结束，恢复每轮完成后等待 {delay:g} 秒。")
                    sprint_finished = True
            await control.sleep(min(delay, max(0, end - time.monotonic())))
        if c["waitlist"] == "提醒":
            emit("log", "查询时限已到。可在网站选择候补并支付预付款；候补需要你人工提交。")
        return "no_ticket"
    except Stopped:
        emit("log", "已停止自动操作，浏览器保留供你处理。")
        return "stopped"
    except Exception as exc:
        emit("log", str(exc) if isinstance(exc, Handoff) else f"{getattr(adapter, 'stage', '购票流程')}失败（{type(exc).__name__}），已停止自动操作，请检查页面。")
        if submitted:
            emit("log", "本次可能已生成订单。请查看排队状态或未完成订单，程序不会重新提交或取消订单。")
        return "handoff"
