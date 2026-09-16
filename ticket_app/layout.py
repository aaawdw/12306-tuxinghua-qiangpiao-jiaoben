"""Desktop layout; browser and booking behavior live outside this module."""
import tkinter as tk
from tkinter import ttk

from .config import SEATS
from .theme import C, FONT


class ScrollPane(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, bg=C["bg"], highlightthickness=0, bd=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.content = ttk.Frame(self.canvas, padding=(16, 16, 16, 8))
        window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", self.resize)
        self.canvas.bind("<Configure>", lambda e: (self.canvas.itemconfigure(window, width=e.width), self.resize()))

    def resize(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        if self.content.winfo_reqheight() > self.canvas.winfo_height() + 2:
            self.bar.grid(row=0, column=1, sticky="ns")
        else:
            self.bar.grid_remove()
            self.canvas.yview_moveto(0)


class Layout:
    def _build(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(3, weight=1)
        top = ttk.Frame(self.root, padding=(24, 16, 24, 16))
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="归程", style="Title.TLabel").pack(side="left")
        ttk.Label(top, text="12306 购票助手", style="Muted.TLabel", padding=(12, 4)).pack(side="left")
        self.edit(ttk.Button(top, text="保存配置", command=self.save)).pack(side="right")
        self.edit(ttk.Button(top, text="导入配置", command=self.import_config)).pack(side="right", padx=(0, 8))

        summary = ttk.Frame(self.root, style="Side.TFrame", padding=(16, 12))
        summary.grid(row=1, column=0, sticky="ew", padx=24, pady=(0, 16))
        summary.columnconfigure(0, weight=1)
        self.route_summary = tk.StringVar(self.root, value="出发站  →  到达站")
        self.date_summary = tk.StringVar(self.root)
        self.count_summary = tk.StringVar(self.root)
        self.route_label = ttk.Label(summary, textvariable=self.route_summary, style="Route.TLabel")
        self.route_label.grid(row=0, column=0, sticky="w")
        self.date_label = ttk.Label(summary, textvariable=self.date_summary, style="SideMuted.TLabel")
        self.date_label.grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(summary, textvariable=self.count_summary, style="Side.TLabel").grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0))

        self.error_label = ttk.Label(self.root, textvariable=self.banner, style="Error.TLabel", padding=(24, 0, 24, 12), wraplength=1000)
        self.error_label.grid(row=2, column=0, sticky="ew")
        self.banner.trace_add("write", lambda *_: self.show_banner())
        self.show_banner()
        self.root.bind("<Configure>", self.resize_layout, add="+")

        body = ttk.Frame(self.root, padding=(24, 0, 24, 0))
        body.grid(row=3, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1, minsize=500)
        body.columnconfigure(1, weight=0, minsize=316)
        body.rowconfigure(0, weight=1)
        self.tabs = ttk.Notebook(body)
        self.tabs.grid(row=0, column=0, sticky="nsew", padx=(0, 20))
        journey = ScrollPane(self.tabs)
        people = ttk.Frame(self.tabs, padding=16)
        preference = ttk.Frame(self.tabs, padding=16)
        self.tabs.add(journey, text="行程设置")
        self.tabs.add(people, text="乘车人")
        self.tabs.add(preference, text="车次偏好")
        self.scroll_panes = [journey]
        self.root.bind_all("<MouseWheel>", self._scroll, add="+")
        self._journey(journey.content)
        self._people(people)
        self._preferences(preference)
        self._run_panel(body)

        footer = ttk.Frame(self.root, padding=(24, 8, 24, 8))
        footer.grid(row=4, column=0, sticky="ew")
        ttk.Label(footer, textvariable=self.saved, style="Muted.TLabel").pack(side="left")
        ttk.Button(footer, text="使用说明", style="Quiet.TButton", command=self.open_help).pack(side="right")
        ttk.Label(footer, text="本机运行 · 手动登录与付款", style="Muted.TLabel", padding=(0, 0, 16, 0)).pack(side="right")

    def show_banner(self):
        if self.banner.get():
            self.error_label.grid()
        else:
            self.error_label.grid_remove()

    def resize_layout(self, event):
        if event.widget is self.root:
            self.error_label.configure(wraplength=max(300, event.width - 48))
            self.route_label.configure(wraplength=max(280, event.width - 340))
            self.date_label.configure(wraplength=max(280, event.width - 340))

    def field(self, parent, label, key, row, col, *, values=None, readonly=False):
        box = ttk.Frame(parent)
        box.grid(row=row, column=col, sticky="ew", padx=(0, 12 if col == 0 else 0), pady=(0, 12))
        ttk.Label(box, text=label).pack(anchor="w", pady=(0, 4))
        if values is not None:
            widget = ttk.Combobox(box, textvariable=self.vars[key], values=values, state="readonly" if readonly else "normal", width=14)
        else:
            widget = ttk.Entry(box, textvariable=self.vars[key], width=16)
        widget.pack(fill="x")
        self.edit(widget, "readonly" if readonly else "normal")
        return widget

    def _journey(self, parent):
        parent.columnconfigure((0, 1), weight=1, uniform="journey")
        ttk.Label(parent, text="设置回家的行程", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        for col, key, label in ((0, "from", "出发站"), (1, "to", "到达站")):
            widget = self.field(parent, label, key, 1, col, values=self.station_names)
            widget.bind("<KeyRelease>", lambda _e, w=widget, k=key: w.configure(values=[s for s in self.station_names if self.vars[k].get().strip() in s][:100]))
        self.field(parent, "乘车日期（YYYY-MM-DD）", "travelDate", 2, 0)
        self.field(parent, "浏览器", "browser", 2, 1, values=["chrome", "msedge", "chromium"], readonly=True)
        self.field(parent, "开售日期（YYYY-MM-DD）", "saleDate", 3, 0)
        self.field(parent, "开售时间（北京时间）", "saleTime", 3, 1)
        ttk.Separator(parent).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(4, 16))
        ttk.Label(parent, text="查询设置", style="Section.TLabel").grid(row=5, column=0, columnspan=2, sticky="w", pady=(0, 12))
        self.field(parent, "常规查询间隔（秒，0.1–60）", "interval", 6, 0)
        self.field(parent, "最长查询时间（分钟）", "duration", 6, 1)
        self.edit(ttk.Checkbutton(parent, text="开售后 30 秒快速查询（间隔最多 1 秒）", variable=self.fast_start)).grid(row=7, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(parent, text="定时运行时生效；小于 1 秒的设置会保留。间隔从上一轮完成后计算，实际周期另加网站响应时间。", style="Muted.TLabel", wraplength=460).grid(row=8, column=0, columnspan=2, sticky="w", pady=(0, 4))

    def make_table(self, parent, columns, *, height=4, priority=False):
        holder = ttk.Frame(parent)
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        tree = ttk.Treeview(holder, columns=tuple(key for key, _ in columns), show="tree headings" if priority else "headings", height=height, selectmode="browse")
        for key, title in columns:
            tree.heading(key, text=title, anchor="w")
            tree.column(key, width=180, minwidth=96, anchor="w")
        if priority:
            tree.heading("#0", text="优先级", anchor="center")
            tree.column("#0", width=68, minwidth=68, stretch=False, anchor="center")
        tree.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(holder, command=tree.yview)
        bar.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=bar.set)
        tree.bind("<<TreeviewSelect>>", lambda _e: self.refresh_lists())
        tree.bind("<Delete>", lambda _e: self.remove(tree))
        return holder, tree

    def _people(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)
        ttk.Label(parent, text="添加乘车人", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 12))
        add = ttk.Frame(parent)
        add.grid(row=1, column=0, sticky="ew", pady=(0, 16))
        add.columnconfigure(0, weight=1)
        self.person_name = tk.StringVar(self.root)
        self.person_ticket = tk.StringVar(self.root, value="成人票")
        ttk.Label(add, text="姓名（与 12306 保持一致）").grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(add, text="票种").grid(row=0, column=1, sticky="w", padx=8, pady=(0, 4))
        entry = self.edit(ttk.Entry(add, textvariable=self.person_name, width=14))
        entry.grid(row=1, column=0, sticky="ew")
        entry.bind("<Return>", lambda _e: self.add_person())
        self.edit(ttk.Combobox(add, textvariable=self.person_ticket, values=["成人票", "学生票"], state="readonly", width=8), "readonly").grid(row=1, column=1, padx=8)
        self.edit(ttk.Button(add, text="添加乘客", command=self.add_person)).grid(row=1, column=2)
        holder, self.people = self.make_table(parent, [("name", "姓名"), ("ticket", "票种")])
        holder.grid(row=2, column=0, sticky="nsew")
        self.people_empty = ttk.Label(holder, text="还没有乘车人\n在上方填写姓名，点击“添加乘客”", style="Empty.TLabel", justify="center")
        self.people_empty.place(relx=.5, rely=.5, y=18, anchor="center")
        tools = ttk.Frame(parent, padding=(0, 12, 0, 0))
        tools.grid(row=3, column=0, sticky="ew")
        self.people_remove = self.edit(ttk.Button(tools, text="移除选中", command=lambda: self.remove(self.people)))
        self.people_remove.pack(side="left")
        ttk.Label(tools, text="最多 9 人 · 无需填写证件号码", style="Muted.TLabel").pack(side="right")

    def _preferences(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1, minsize=92)
        heading = ttk.Frame(parent)
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(heading, text="优先车次与席别", style="Section.TLabel").pack(side="left")
        ttk.Label(heading, text="从上到下优先匹配", style="Muted.TLabel").pack(side="right")
        add = ttk.Frame(parent)
        add.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        add.columnconfigure(0, weight=1)
        self.train = tk.StringVar(self.root)
        self.seat = tk.StringVar(self.root, value="二等座")
        ttk.Label(add, text="车次编号，例如 G87").grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(add, text="席别").grid(row=0, column=1, sticky="w", padx=8, pady=(0, 4))
        entry = self.edit(ttk.Entry(add, textvariable=self.train, width=12))
        entry.grid(row=1, column=0, sticky="ew")
        entry.bind("<Return>", lambda _e: self.add_preference())
        self.edit(ttk.Combobox(add, textvariable=self.seat, values=list(SEATS), state="readonly", width=10), "readonly").grid(row=1, column=1, padx=8)
        self.edit(ttk.Button(add, text="添加组合", command=self.add_preference)).grid(row=1, column=2)
        holder, self.preferences = self.make_table(parent, [("train", "车次"), ("seat", "席别")], priority=True)
        holder.grid(row=2, column=0, sticky="nsew")
        self.preference_empty = ttk.Label(holder, text="还没有优先车次\n添加车次和席别，再调整优先顺序", style="Empty.TLabel", justify="center")
        self.preference_empty.place(relx=.5, rely=.5, y=18, anchor="center")
        tools = ttk.Frame(parent, padding=(0, 8, 0, 12))
        tools.grid(row=3, column=0, sticky="ew")
        self.up_button = self.edit(ttk.Button(tools, text="上移", command=lambda: self.move_preference(-1)))
        self.up_button.pack(side="left")
        self.down_button = self.edit(ttk.Button(tools, text="下移", command=lambda: self.move_preference(1)))
        self.down_button.pack(side="left", padx=8)
        self.preference_remove = self.edit(ttk.Button(tools, text="移除选中", command=lambda: self.remove(self.preferences)))
        self.preference_remove.pack(side="right")
        ttk.Separator(parent).grid(row=4, column=0, sticky="ew")
        options = ttk.Frame(parent, padding=(0, 12, 0, 0))
        options.grid(row=5, column=0, sticky="ew")
        options.columnconfigure((1, 3), weight=1)
        ttk.Label(options, text="卧铺").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.edit(ttk.Combobox(options, textvariable=self.vars["berth"], values=["不限", "中铺优先", "下铺优先", "必须下铺"], state="readonly", width=9), "readonly").grid(row=0, column=1, sticky="ew", padx=(0, 16))
        ttk.Label(options, text="座位").grid(row=0, column=2, sticky="w", padx=(0, 8))
        self.edit(ttk.Combobox(options, textvariable=self.vars["seatPosition"], values=["自动分配", "靠窗优先", "靠过道优先"], state="readonly", width=10), "readonly").grid(row=0, column=3, sticky="ew")
        self.edit(ttk.Checkbutton(options, text="接受无座", variable=self.no_seat)).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.edit(ttk.Checkbutton(options, text="提醒候补", variable=self.waitlist)).grid(row=1, column=2, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(parent, text="座位及中铺偏好无法满足时自动分配；下铺要求、候补需人工。", style="Muted.TLabel", wraplength=440).grid(row=6, column=0, sticky="w", pady=(8, 0))

    def _run_panel(self, parent):
        side = ttk.Frame(parent, style="Side.TFrame", padding=16)
        side.grid(row=0, column=1, sticky="nsew")
        side.columnconfigure(0, weight=1)
        side.rowconfigure(12, weight=1)
        ttk.Label(side, text="运行状态", style="SideMuted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(side, textvariable=self.status, style="State.TLabel", wraplength=280).grid(row=1, column=0, sticky="w", pady=(8, 8))
        ttk.Label(side, textvariable=self.status_detail, style="Side.TLabel", wraplength=280).grid(row=2, column=0, sticky="ew", pady=(0, 12))
        self.countdown_label = ttk.Label(side, textvariable=self.countdown, style="Countdown.TLabel")
        self.countdown_label.grid(row=3, column=0, sticky="w", pady=(0, 12))
        self.mode_box = self.edit(ttk.Combobox(side, textvariable=self.mode, values=list(self.mode_options), state="readonly", width=28), "readonly")
        self.mode_box.grid(row=4, column=0, sticky="ew")
        self.mode_box.bind("<<ComboboxSelected>>", lambda _e: self.sync_controls())
        self.mode_label = ttk.Label(side, textvariable=self.mode, style="SideMuted.TLabel", wraplength=280)
        self.mode_label.grid(row=4, column=0, sticky="w")
        self.now_check = self.edit(ttk.Checkbutton(side, text="立即开始（忽略定时）", variable=self.now, style="Side.TCheckbutton"))
        self.now_check.grid(row=5, column=0, sticky="w", pady=(4, 0))
        self.start_button = ttk.Button(side, text="打开浏览器并开始", style="Primary.TButton", command=self.start)
        self.start_button.grid(row=6, column=0, sticky="ew", pady=(12, 0))
        self.login_button = ttk.Button(side, text="已登录，继续", style="Primary.TButton", command=self.login, state="disabled")
        self.login_button.grid(row=7, column=0, sticky="ew", pady=(12, 0))
        self.run_controls = ttk.Frame(side, style="Side.TFrame")
        self.run_controls.grid(row=8, column=0, sticky="ew", pady=(8, 0))
        self.run_controls.columnconfigure((0, 1), weight=1)
        self.pause_button = ttk.Button(self.run_controls, text="暂停", command=self.pause, state="disabled")
        self.pause_button.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.stop_button = ttk.Button(self.run_controls, text="停止", command=self.stop, state="disabled")
        self.stop_button.grid(row=0, column=1, sticky="ew")
        self.close_button = ttk.Button(side, text="关闭浏览器会话", command=self.close_session, state="disabled")
        self.close_button.grid(row=9, column=0, sticky="ew", pady=(12, 0))
        ttk.Separator(side).grid(row=10, column=0, sticky="ew", pady=(16, 0))
        ttk.Label(side, text="运行日志", style="SideMuted.TLabel").grid(row=11, column=0, sticky="w", pady=(12, 8))
        logs = ttk.Frame(side, style="Side.TFrame")
        logs.grid(row=12, column=0, sticky="nsew")
        self.logs = tk.Text(logs, width=29, height=4, wrap="word", font=(FONT, 10), background=C["surface"], foreground=C["ink"], relief="flat", highlightthickness=0, state="disabled", spacing3=8)
        self.logs.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(logs, command=self.logs.yview)
        scrollbar.pack(side="right", fill="y")
        self.logs.configure(yscrollcommand=scrollbar.set)

    def refresh_summary(self):
        start, end = self.vars["from"].get().strip(), self.vars["to"].get().strip()
        self.route_summary.set(f'{start or "出发站"}  →  {end or "到达站"}')
        self.date_summary.set(f'乘车 {self.vars["travelDate"].get()}    ·    开售 {self.vars["saleDate"].get()} {self.vars["saleTime"].get()}')
        self.count_summary.set(f'{len(self.people.get_children())} 位乘客  ·  {len(self.preferences.get_children())} 个车次组合')

    def config_changed(self, *_):
        self.refresh_summary()
        if not self.busy and not getattr(self, "_loading", False):
            self.saved.set("有未保存的修改")

    def refresh_lists(self):
        if not hasattr(self, "preference_remove"):
            return
        for tree, label in ((self.people, self.people_empty), (self.preferences, self.preference_empty)):
            if tree.get_children(): label.place_forget()
            else: label.place(relx=.5, rely=.5, y=18, anchor="center")
        selected = self.preferences.selection()
        index = self.preferences.index(selected[0]) if selected else -1
        count = len(self.preferences.get_children())
        for n, item in enumerate(self.preferences.get_children(), 1):
            self.preferences.item(item, text=str(n))
        for button, allowed in (
            (self.people_remove, bool(self.people.selection())),
            (self.preference_remove, bool(selected)),
            (self.up_button, index > 0),
            (self.down_button, 0 <= index < count - 1),
        ):
            button.configure(state="normal" if allowed and not self.busy else "disabled")
        self.refresh_summary()
