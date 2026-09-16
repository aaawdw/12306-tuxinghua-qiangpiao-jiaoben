from __future__ import annotations

import json
import os
import queue
import re
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

from .config import CHINA, ROOT, SEATS, defaults, load_config, save_config, stations, validate
from .theme import apply
from .layout import Layout
from .worker import Worker

STATES = {
    "idle": ("准备行程", "填写左侧表单，先用查询演练核对流程。"),
    "opening": ("正在打开浏览器", "请稍候，浏览器将在独立窗口打开。"),
    "login": ("等待你登录", "在浏览器中登录 12306，并检查未完成订单，然后点击下方按钮。"),
    "recovery": ("请核对上次订单", "本地记录不代表已出票。请在浏览器核对未完成、已支付订单和排队状态。"),
    "waiting": ("等待开售", "已就绪。请保持电脑唤醒、联网。"),
    "querying": ("正在查询", "按车次与席别优先级匹配余票。"),
    "ordering": ("正在核对订单", "正在选择乘客和席别。"),
    "confirming": ("正在选座与确认", "按座位偏好选择位置，等待网站确认按钮可用。"),
    "queueing": ("等待订票结果", "请求已提交，等待网站返回结果。"),
    "payment": ("请前往浏览器付款", "自动操作已停止。核对订单，并在网站提示期限内完成付款。"),
    "dry_run": ("查询演练完成", "没有点击预订或提交订单。结果见下方日志。"),
    "no_ticket": ("查询时限已到", "没有匹配到车票，自动查询已停止。"),
    "handoff": ("需要你处理", "自动操作已停止。查看日志和浏览器，确认当前状态。"),
    "stopped": ("已停止", "浏览器保留，方便你继续处理。"),
}
MODES = {"网站查询演练 · 不下单": "dry_run", "正式购票 · 到待支付停止": "live", "本地模拟演练 · 无需登录": "demo"}


class App(Layout):
    mode_options = MODES
    def __init__(self, root, config_path=None, *, local=None):
        self.root = root
        self.config_path = Path(config_path) if config_path else ROOT / "config.json"
        self.local = Path(local) if local else ROOT / ".local"
        self.events = queue.Queue()
        self.worker = None
        self.busy = False
        self.finished = False
        self.phase = "idle"
        self.active_config = None
        self.editable = []
        self.closing = False
        apply(root)
        root.title("归程 · 12306 购票助手")
        width = min(1180, root.winfo_screenwidth() - 80)
        height = min(820, root.winfo_screenheight() - 90)
        root.geometry(f"{width}x{height}")
        root.minsize(min(960, width), min(680, height))
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.vars = {k: tk.StringVar(root) for k in ("from", "to", "travelDate", "saleDate", "saleTime", "browser", "berth", "interval", "duration", "seatPosition")}
        self.no_seat = tk.BooleanVar(root)
        self.fast_start = tk.BooleanVar(root, value=True)
        self.waitlist = tk.BooleanVar(root, value=True)
        self.now = tk.BooleanVar(root)
        self.mode = tk.StringVar(root, value=next(iter(MODES)))
        self.banner = tk.StringVar(root)
        self.status = tk.StringVar(root)
        self.status_detail = tk.StringVar(root)
        self.countdown = tk.StringVar(root, value="")
        self.saved = tk.StringVar(root, value="配置仅保存在本机")
        self.station_names = list(stations())
        self._build()
        self.populate(defaults())
        if self.config_path.exists():
            try:
                self.populate(load_config(self.config_path))
                self.saved.set("已载入本机配置")
            except (ValueError, OSError, json.JSONDecodeError):
                self.banner.set("已有配置无法读取，原文件未修改。请检查或导入有效配置。")
        self.set_phase("idle")
        for variable in (*self.vars.values(), self.no_seat, self.waitlist, self.fast_start):
            variable.trace_add("write", self.config_changed)
        self.refresh_lists()
        self.append_log("欢迎。先填写行程、添加乘客和车次；也可以直接运行本地模拟演练。")
        root.after(100, self.pump)
        root.after(500, self.tick)

    def edit(self, widget, state="normal"):
        self.editable.append((widget, state))
        return widget

    def _scroll(self, event):
        if event.widget.winfo_class() in ("TCombobox", "TEntry", "Treeview", "Text"):
            return
        for pane in self.scroll_panes:
            if str(event.widget).startswith(str(pane)) and pane.content.winfo_reqheight() > pane.canvas.winfo_height():
                pane.canvas.yview_scroll(-int(event.delta / 120), "units")
                return

    def populate(self, c):
        self._loading = True
        self.vars["seatPosition"].set(c.get("seatPosition", "自动分配"))
        for key in ("from", "to", "travelDate", "browser", "berth"):
            self.vars[key].set(c[key])
        sale = datetime.fromisoformat(c["saleAt"]).astimezone(CHINA)
        self.vars["saleDate"].set(sale.strftime("%Y-%m-%d"))
        self.vars["saleTime"].set(sale.strftime("%H:%M:%S"))
        self.vars["interval"].set(f'{c["pollIntervalMs"] / 1000:g}')
        self.vars["duration"].set(str(c["maxRunMinutes"]))
        self.no_seat.set(c["allowNoSeat"])
        self.fast_start.set(c.get('fastStart', True))
        self.waitlist.set(c["waitlist"] == "提醒")
        for tree in (self.people, self.preferences):
            tree.delete(*tree.get_children())
        for p in c["passengers"]:
            self.people.insert("", "end", values=(p["name"], p["ticket"]))
        for p in c["preferences"]:
            self.preferences.insert("", "end", values=(p["train"], p["seat"]))
        self._loading = False
        self.refresh_lists()

    def collect(self):
        v = {k: value.get().strip() for k, value in self.vars.items()}
        time = v["saleTime"]
        if re.fullmatch(r"\d{2}:\d{2}", time):
            time += ":00"
        try:
            interval, duration = float(v["interval"]) * 1000, float(v["duration"])
        except ValueError:
            raise ValueError("查询间隔和查询时长需要填写数字。") from None
        return validate({"from": v["from"], "to": v["to"], "travelDate": v["travelDate"],
                         "saleAt": f'{v["saleDate"]}T{time}+08:00', "browser": v["browser"], "berth": v["berth"],
                         "pollIntervalMs": interval, "maxRunMinutes": duration,
                         "seatPosition": v["seatPosition"],
                         "fastStart": self.fast_start.get(),
                         "allowNoSeat": self.no_seat.get(), "waitlist": "提醒" if self.waitlist.get() else "关闭",
                         "passengers": [{"name": self.people.set(i, "name"), "ticket": self.people.set(i, "ticket")} for i in self.people.get_children()],
                         "preferences": [{"train": self.preferences.set(i, "train"), "seat": self.preferences.set(i, "seat")} for i in self.preferences.get_children()]})

    def add_person(self):
        if self.busy: return
        name = self.person_name.get().strip()
        if not name or len(name) > 60:
            self.banner.set("请填写乘客姓名（不需要身份证号）。")
            return
        if name in [self.people.set(i, "name") for i in self.people.get_children()] or len(self.people.get_children()) >= 9:
            self.banner.set("不能重复添加乘客，每次最多 9 人。")
            return
        self.people.insert("", "end", values=(name, self.person_ticket.get()))
        self.person_name.set("")
        self.banner.set("")
        self.refresh_lists()
        self.config_changed()

    def add_preference(self):
        if self.busy: return
        train, seat = self.train.get().strip().upper(), self.seat.get()
        if not re.fullmatch(r"[GDCZTKYS]?\d{1,5}", train):
            self.banner.set("请填写有效车次，例如 G87、K157。")
            return
        if (train, seat) in [(self.preferences.set(i, "train"), self.preferences.set(i, "seat")) for i in self.preferences.get_children()]:
            self.banner.set("这个车次与席别已添加。")
            return
        if len(self.preferences.get_children()) >= 30:
            self.banner.set("最多添加 30 个组合。")
            return
        self.preferences.insert("", "end", values=(train, seat))
        self.train.set("")
        self.banner.set("")
        self.refresh_lists()
        self.config_changed()

    def remove(self, tree):
        if not self.busy:
            tree.delete(*tree.selection())
            self.refresh_lists()
            self.config_changed()

    def move_preference(self, delta):
        if self.busy: return
        selected = self.preferences.selection()
        if selected:
            self.preferences.move(selected[0], "", max(0, self.preferences.index(selected[0]) + delta))
            self.refresh_lists()
            self.config_changed()

    def save(self):
        if self.busy: return
        try:
            save_config(self.config_path, self.collect())
            self.banner.set("")
            self.saved.set("配置已保存")
        except (ValueError, OSError) as exc:
            self.banner.set(str(exc))

    def import_config(self):
        if self.busy: return
        name = filedialog.askopenfilename(parent=self.root, title="导入购票配置", initialdir=ROOT, filetypes=[("JSON 配置", "*.json")])
        if name:
            try:
                self.populate(load_config(Path(name)))
                self.saved.set("已导入，点击保存后写入本机")
                self.banner.set("")
            except (ValueError, OSError):
                self.banner.set("导入失败，请检查配置格式、车站和日期。原配置未修改。")

    def lock_form(self, locked):
        self.busy = locked
        for widget, normal in self.editable:
            widget.configure(state="disabled" if locked else normal)
        self.start_button.configure(state="disabled" if locked else "normal")
        self.sync_controls()
        self.refresh_lists()

    def sync_controls(self):
        for widget, show in (
            (self.mode_box, not self.busy),
            (self.mode_label, self.busy),
            (self.start_button, not self.busy),
            (self.login_button, self.busy and self.phase in ("login", "recovery") and not self.finished),
            (self.run_controls, self.busy and not self.finished),
            (self.close_button, self.busy and self.finished),
            (self.now_check, not self.busy and MODES[self.mode.get()] == "live"),
            (self.countdown_label, self.phase == "waiting"),
        ):
            if show: widget.grid()
            else: widget.grid_remove()

    def start(self):
        if self.busy: return
        mode = MODES[self.mode.get()]
        try:
            c = defaults() | {"browser": self.vars["browser"].get(), "seatPosition": self.vars["seatPosition"].get()} if mode == "demo" else self.collect()
            if mode != "demo":
                if c["travelDate"] < datetime.now(CHINA).date().isoformat():
                    raise ValueError("乘车日期已过去，请更新行程。")
                if mode == "live" and not self.now.get() and datetime.fromisoformat(c["saleAt"]).timestamp() < datetime.now().timestamp() - 60:
                    raise ValueError("开售时间已过去，请修改时间，或勾选“立即开始”。")
                save_config(self.config_path, c)
                self.saved.set("配置已自动保存")
            self.banner.set("")
        except (ValueError, OSError) as exc:
            self.banner.set(str(exc))
            return
        self.active_config = c
        self.finished = False
        self.lock_form(True)
        self.close_button.configure(state="normal")
        self.stop_button.configure(state="normal")
        self.set_phase("opening")
        self.append_log("开始本地模拟演练。" if mode == "demo" else f'准备 {c["from"]} → {c["to"]}，{len(c["passengers"])} 位乘客。')
        if mode != 'demo':
            self.append_log(f'本次查询间隔：每轮完成后 {c["pollIntervalMs"] / 1000:g} 秒；卧铺偏好：{c["berth"]}。')
        self.worker = Worker(c, mode, self.events, now=self.now.get(), local=self.local)
        self.worker.start()

    def set_phase(self, phase):
        self.phase = phase
        title, detail = STATES[phase]
        self.status.set(title)
        self.status_detail.set(detail)
        self.login_button.configure(state="normal" if phase in ("login", "recovery") else "disabled",
                                    text="已核对无订单，继续" if phase == "recovery" else "已登录，继续")
        self.pause_button.configure(state="normal" if phase in ("waiting", "querying", "ordering", "confirming", "queueing") else "disabled")
        self.sync_controls()

    def login(self):
        if self.worker and self.phase == "recovery" and not self.finished:
            if not messagebox.askyesno(
                "确认重新尝试本行程",
                "请先在 12306 核对本行程：没有待支付订单、没有已支付或已出票的车票，也没有仍在排队的请求。\n\n"
                "确认后会归档本行程的旧记录并重新查询，可能再次创建订单。不会取消网站订单。\n\n"
                "你已核对，并确认需要重新尝试吗？",
                default="no", parent=self.root):
                return
            self.worker.control.recover.set()
            self.login_button.configure(state="disabled")
            self.status_detail.set("正在归档旧记录并恢复查询…")
            return
        if self.worker and self.phase == "login":
            self.worker.control.login.set()
            self.login_button.configure(state="disabled")
            self.status_detail.set("正在检查登录状态和查询条件。")

    def pause(self):
        if not self.worker or self.finished: return
        flag = self.worker.control.paused
        if flag.is_set():
            flag.clear()
            self.pause_button.configure(text="暂停")
            self.status.set(STATES[self.phase][0])
            self.append_log("继续执行。")
        else:
            flag.set()
            self.pause_button.configure(text="继续")
            self.status.set("已请求暂停")
            self.append_log("已请求暂停；已发送的网站请求仍可能完成。")

    def stop(self):
        if self.worker:
            self.worker.control.stop.set()
            self.worker.control.paused.clear()
            self.stop_button.configure(state="disabled")
            self.pause_button.configure(state="disabled")
            self.login_button.configure(state="disabled")
            self.status_detail.set("正在停止，浏览器会继续保留。")

    def close_session(self):
        if self.worker:
            if self.phase == "payment" and MODES[self.mode.get()] != "demo" and not messagebox.askyesno("关闭浏览器", "请先完成付款。现在要关闭浏览器会话吗？", parent=self.root):
                return
            self.worker.control.close.set()
            self.worker.control.stop.set()
            self.worker.control.paused.clear()
            self.close_button.configure(state="disabled")
            self.status_detail.set("正在关闭浏览器会话…")

    def append_log(self, text):
        self.logs.configure(state="normal")
        self.logs.insert("end", datetime.now().strftime("%H:%M:%S") + "  " + text + "\n")
        if int(self.logs.index("end-1c").split(".")[0]) > 350:
            self.logs.delete("1.0", "80.0")
        self.logs.see("end")
        self.logs.configure(state="disabled")

    def pump(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "state":
                    self.set_phase(value)
                elif kind == "log":
                    self.append_log(value)
                elif kind == "result":
                    self.finished = True
                    self.set_phase(value)
                    if value == "payment" and MODES[self.mode.get()] == "demo":
                        self.status.set("模拟流程已完成")
                        self.status_detail.set("已验证至待支付页。全部为虚构数据，无需付款。")
                    self.stop_button.configure(state="disabled")
                    self.pause_button.configure(state="disabled", text="暂停")
                    if value in ("payment", "handoff"):
                        self.root.bell()
                elif kind == "closed":
                    self.worker = None
                    self.lock_form(False)
                    for button in (self.close_button, self.pause_button, self.stop_button, self.login_button):
                        button.configure(state="disabled")
                    self.append_log("浏览器会话已关闭，可以修改配置并重新开始。")
                    self.status_detail.set("浏览器会话已关闭。可以修改配置并重新开始。")
                    if self.closing:
                        self.root.destroy()
                        return
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def tick(self):
        text = ""
        if self.busy and self.phase == "waiting" and self.active_config:
            seconds = max(0, int(datetime.fromisoformat(self.active_config["saleAt"]).timestamp() - datetime.now().timestamp()))
            text = f"距开售 {seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
        self.countdown.set(text)
        self.root.after(500, self.tick)

    def open_help(self):
        if os.name == "nt":
            os.startfile(str(ROOT / "README.md"))

    def on_close(self):
        if self.worker:
            if not messagebox.askyesno("退出购票助手", "退出会停止自动操作并关闭浏览器。请确认已完成付款或人工处理。是否退出？", parent=self.root):
                return
            self.closing = True
            self.worker.control.close.set()
            self.worker.control.stop.set()
            self.worker.control.paused.clear()
            self.status_detail.set("正在停止并关闭会话，请稍候…")
        else:
            self.root.destroy()
