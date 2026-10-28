"""Graphical connector and pin dashboard for the desktop test station."""

from collections import OrderedDict
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk
from peripherals import SENSORS, STORAGE, DEVICES, display_value


BG = "#0a101c"
SIDEBAR = "#0d1524"
PANEL = "#111d2f"
INSET = "#0c1626"
BORDER = "#23344b"
TEXT = "#e8f0fc"
MUTED = "#8194af"
BLUE = "#69aaff"
GREEN = "#3ed6a1"
RED = "#ff6b81"
AMBER = "#f4c56b"
FONT = "Segoe UI"
MONO = "Consolas"
STATE_COLORS = {
    "PASS": (GREEN, "#113329"),
    "FAIL": (RED, "#3d1c2b"),
    "VERIFYING": (AMBER, "#362e1d"),
    "WAITING": (MUTED, "#1a283c"),
    "NOT RUN": (MUTED, "#1a283c"),
}


def rounded(canvas, x1, y1, x2, y2, radius=12, **kwargs):
    return canvas.create_polygon(
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        smooth=True, splinesteps=24, **kwargs)


def label(parent, text=None, variable=None, size=10, color=TEXT, bold=False, bg=None, **kwargs):
    return tk.Label(parent, text=text, textvariable=variable, bg=bg or parent.cget("bg"),
                    fg=color, font=(FONT, size, "bold" if bold else "normal"), bd=0, **kwargs)


class Action(tk.Canvas):
    def __init__(self, parent, text, command, width=100, primary=False, danger=False):
        super().__init__(parent, width=width, height=36, bg=parent.cget("bg"),
                         highlightthickness=0, cursor="hand2", takefocus=True)
        self.text, self.command = text, command
        self.fill = "#357ee8" if primary else "#362032" if danger else "#1a2a41"
        self.hover = "#4990fa" if primary else "#4c263c" if danger else "#263b56"
        self.fg = "#ffffff" if primary else RED if danger else TEXT
        self.bind("<Configure>", lambda _: self.paint())
        self.bind("<Enter>", lambda _: self.paint(True))
        self.bind("<Leave>", lambda _: self.paint())
        self.bind("<Button-1>", lambda _: self.invoke())
        self.bind("<Return>", lambda _: self.invoke())
        self.bind("<space>", lambda _: self.invoke())
        self.bind("<FocusIn>", lambda _: self.paint())
        self.bind("<FocusOut>", lambda _: self.paint())

    def paint(self, hover=False):
        self.delete("all")
        width = self.winfo_width()
        rounded(self, 1, 1, width - 1, 35, radius=8, fill=self.hover if hover else self.fill,
                outline=BLUE if self.focus_get() == self else "")
        self.create_text(width / 2, 18, text=self.text, fill=self.fg, font=(FONT, 10, "bold"))

    def invoke(self):
        self.focus_set()
        self.command()


def load_connectors(profile):
    """Display-only pin positions; the firmware profile remains unchanged."""
    data = json.loads(Path(__file__).with_name("connectors.json").read_text(encoding="utf-8"))
    if data["schematic_sha256"] != profile["schematic_sha256"]:
        raise ValueError("Connector diagram and wiring profile use different schematics")
    connectors = {}
    for item in data["connectors"]:
        if item["name"] in connectors:
            raise ValueError("Duplicate connector diagram")
        pins = [pin["number"] for pin in item["pins"]]
        if len(pins) != len(set(pins)) or not all(type(pin) is int and 1 <= pin <= 32 for pin in pins):
            raise ValueError("Invalid connector diagram pin numbers")
        connectors[item["name"]] = {**item, "pins": sorted(item["pins"], key=lambda pin: pin["number"])}
    for row in profile["channels"]:
        positions = {pin["number"]: pin for pin in connectors.get(row["probe_connector"], {}).get("pins", [])}
        if row["probe_connector_pin"] not in positions:
            raise ValueError(f"Missing diagram position for {row['id']}")
        if row["probe_net"] not in positions[row["probe_connector_pin"]]["nets"]:
            raise ValueError(f"Diagram net does not match the wiring profile: {row['id']}")
    return connectors


def signal_name(row):
    name = row["id"]
    for prefix in ("UART4_I2C3_", "TELEM1_", "TELEM2_", "TELEM3_", "GPS1_", "GPS2_",
                   "SPI6_", "ADIO_", "POWER1_", "PPM_", "FMU_"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


class PortMap(tk.Canvas):
    ROW_HEIGHT = 30

    def __init__(self, parent, dashboard):
        super().__init__(parent, bg=BG, highlightthickness=0, yscrollincrement=24)
        self.dashboard = dashboard
        self.signature = None
        self.bind("<Configure>", lambda _: self.render(force=True))
        self.bind("<MouseWheel>", self.wheel)
        self.bind("<Button-4>", lambda _: self.yview_scroll(-3, "units"))
        self.bind("<Button-5>", lambda _: self.yview_scroll(3, "units"))

    def wheel(self, event):
        self.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def render(self, force=False):
        view, station = self.dashboard, self.dashboard.station
        results = station.assessment.results
        signature = (self.winfo_width(), view.filter_group, view.faults_only, view.selected,
                     tuple((r.status, r.frequency_hz) for r in results))
        if signature == self.signature and not force:
            return
        self.signature = signature
        self.delete("all")
        width = max(320, self.winfo_width() - 4)
        columns = max(1, min(3, int((width + 12) / 302)))
        card_width = (width - (columns - 1) * 12) / columns
        heights = [2] * columns
        visible = 0
        for group, indices in view.groups.items():
            if view.filter_group and group != view.filter_group:
                continue
            if view.faults_only and not any(results[i].status == "FAIL" for i in indices):
                continue
            column = min(range(columns), key=lambda col: heights[col])
            height = 132 + self.ROW_HEIGHT * len(indices)
            self.card(column * (card_width + 12), heights[column], card_width, height, group, indices)
            heights[column] += height + 12
            visible += 1
        if not visible:
            self.create_text(width / 2, 100, text="No failed pins" if view.faults_only else "No ports selected",
                             fill=GREEN if view.faults_only else MUTED, font=(FONT, 16, "bold"))
            self.create_text(width / 2, 133, text="Pin diagrams update with each measurement window.",
                             fill=MUTED, font=(FONT, 10))
        self.configure(scrollregion=(0, 0, width, max(heights) + 8))

    def select_pin(self, index):
        self.dashboard.select(index)

    def card(self, x, y, width, height, group, indices):
        view, station = self.dashboard, self.dashboard.station
        channels, results = station.profile["channels"], station.assessment.results
        failed = sum(results[i].status == "FAIL" for i in indices)
        passed = sum(results[i].status == "PASS" for i in indices)
        color = RED if failed else GREEN if passed == len(indices) else MUTED
        rounded(self, x, y, x + width, y + height, fill=PANEL, outline=RED if failed else BORDER)
        self.create_text(x + 14, y + 23, text=group, anchor="w", fill=TEXT, font=(FONT, 12, "bold"))
        self.create_oval(x + width - 80, y + 19, x + width - 74, y + 25, fill=color, outline="")
        self.create_text(x + width - 14, y + 23, text=f"{failed} FAIL" if failed else f"{passed}/{len(indices)}",
                         anchor="e", fill=color, font=(MONO, 9, "bold"))
        connector = channels[indices[0]]["probe_connector"]
        self.create_text(x + 14, y + 48, text=f"PROBE {connector}  ·  NUMBERED PINS", anchor="w", fill=MUTED, font=(FONT, 8))
        positions = view.connectors[connector]["pins"]
        by_pin = {channels[i]["probe_connector_pin"]: i for i in indices}
        gap = 5
        pin_width = min(29, (width - 28 - gap * (len(positions) - 1)) / len(positions))
        for offset, pin in enumerate(positions):
            px = x + 14 + offset * (pin_width + gap)
            index = by_pin.get(pin["number"])
            status = results[index].status if index is not None else None
            fg, fill = STATE_COLORS[status] if status else ("#52647e", INSET)
            tags = (f"pin:{index}",) if index is not None else ()
            rounded(self, px, y + 63, px + pin_width, y + 91, radius=5,
                    fill=fill, outline=fg if index == view.selected or status == "FAIL" else BORDER,
                    tags=tags + ((f"pin-fill:{index}",) if index is not None else ()))
            self.create_text(px + pin_width / 2, y + 77, text=str(pin["number"]), fill=fg,
                             font=(MONO, 9, "bold"), tags=tags)
            if index is not None:
                self.tag_bind(f"pin:{index}", "<Button-1>", lambda _, i=index: self.select_pin(i))
        self.create_text(x + 14, y + 112, text="SIGNAL", anchor="w", fill=MUTED, font=(FONT, 8))
        self.create_text(x + width * .49, y + 112, text="FMU → PROBE", fill=MUTED, font=(FONT, 8))
        self.create_text(x + width - 14, y + 112, text="Hz", anchor="e", fill=MUTED, font=(FONT, 8))
        for offset, index in enumerate(indices):
            row, result = channels[index], results[index]
            ry = y + 132 + offset * self.ROW_HEIGHT
            fg, fill = STATE_COLORS[result.status]
            tag = f"row:{index}"
            if result.status == "FAIL" or index == view.selected:
                rounded(self, x + 7, ry - 12, x + width - 7, ry + 13, radius=5,
                        fill=fill if result.status == "FAIL" else "#1b304d", outline="", tags=(tag,))
            self.create_text(x + 14, ry, text=signal_name(row), anchor="w", fill=fg, font=(FONT, 9), tags=(tag,))
            self.create_text(x + width * .49, ry, text=f"{row['source_pin']} → {row['probe_pin']}",
                             fill=fg, font=(MONO, 9), tags=(tag,))
            measured = f"{result.frequency_hz:.2f}" if result.frequency_hz is not None else "—"
            self.create_text(x + width - 14, ry, text=measured, anchor="e", fill=fg, font=(MONO, 9), tags=(tag,))
            self.tag_bind(tag, "<Button-1>", lambda _, i=index: self.select_pin(i))


class Dashboard:
    def __init__(self, station):
        self.station = station
        self.root = station.root
        self.connectors = load_connectors(station.profile)
        self.groups = OrderedDict()
        for index, row in enumerate(station.profile["channels"]):
            self.groups.setdefault(row["group"], []).append(index)
        self.selected, self.filter_group, self.faults_only = 0, None, False
        self.nav_items, self.metric_labels = {}, {}
        self._inspector_signature = None
        self.selected_sensor = None
        self.sensor_cards = {}
        self.peripheral_group = "Sensors"
        self.peripheral_groups, self.peripheral_buttons = {}, {}
        self._build()

    def _build(self):
        root = self.root
        root.title("PixNova · IO Test Station" + (" — DEMO" if self.station.demo else ""))
        root.configure(bg=BG)
        root.geometry("1540x960")
        root.minsize(1180, 760)
        self._theme()
        sidebar = tk.Frame(root, bg=SIDEBAR, width=172)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        brand = tk.Canvas(sidebar, height=95, width=172, bg=SIDEBAR, highlightthickness=0)
        brand.pack(fill="x")
        rounded(brand, 19, 24, 51, 56, radius=8, fill="#234c7a", outline="")
        brand.create_line(27, 47, 27, 33, 40, 33, 40, 40, 28, 40, fill=BLUE, width=2)
        brand.create_text(62, 33, text="PixNova", anchor="w", fill=TEXT, font=(FONT, 14, "bold"))
        brand.create_text(62, 53, text="TEST STATION", anchor="w", fill=MUTED, font=(FONT, 7, "bold"))
        label(sidebar, "WORKSPACE", size=8, color=MUTED).pack(anchor="w", padx=20, pady=(0, 10))
        self._nav(sidebar, "All ports", None)
        self._nav(sidebar, "Failed pins", "failed")
        label(sidebar, "CONNECTORS", size=8, color=MUTED).pack(anchor="w", padx=20, pady=(22, 8))
        for group in self.groups:
            self._nav(sidebar, group, group)
        protected = tk.Frame(sidebar, bg=SIDEBAR)
        protected.pack(side="bottom", fill="x", padx=20, pady=24)
        label(protected, "SENSOR BUSES", size=8, color=MUTED).pack(anchor="w")
        label(protected, "I2C2 / I2C3 / I2C4", size=9, color=BLUE).pack(anchor="w", pady=(6, 2))
        label(protected, "Protected · no waveforms", size=8, color=MUTED).pack(anchor="w")
        body = tk.Frame(root, bg=BG)
        body.pack(side="left", fill="both", expand=True, padx=24, pady=20)
        heading = tk.Frame(body, bg=BG)
        heading.pack(fill="x")
        titles = tk.Frame(heading, bg=BG)
        titles.pack(side="left")
        label(titles, "IO verification", size=24, bold=True).pack(anchor="w")
        label(titles, "Pin-level signals. Independent measurement.", color=MUTED, size=10).pack(anchor="w", pady=(4, 0))
        actions = tk.Frame(heading, bg=BG)
        actions.pack(side="right")
        if self.station.demo:
            label(actions, "DEMO / SIMULATED", color=AMBER, bold=True, size=9).pack(side="left", padx=14)
        Action(actions, "Export report", self.station.export, width=112).pack(side="left", padx=(0, 8))
        Action(actions, "■  Stop", self.station.stop, width=80, danger=True).pack(side="left", padx=(0, 8))
        Action(actions, "▶  Start test", self.station.start, width=122, primary=True).pack(side="left")
        self._connections(body)
        self._peripherals(body)
        metrics = tk.Frame(body, bg=BG)
        metrics.pack(fill="x", pady=(14, 16))
        for column, (name, caption, color) in enumerate([
                ("total", "TEST INPUTS", TEXT), ("pass", "PASSED", GREEN),
                ("fail", "FAILED", RED), ("pending", "PENDING", AMBER)]):
            metrics.columnconfigure(column, weight=1, uniform="metric")
            card = tk.Frame(metrics, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
            card.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 6, 0 if column == 3 else 6))
            label(card, caption, color=MUTED, size=8).pack(side="left", padx=14, pady=16)
            number = label(card, "0", color=color, size=23, bold=True)
            number.pack(side="right", padx=16)
            self.metric_labels[name] = number
        workspace = tk.Frame(body, bg=BG)
        workspace.pack(fill="both", expand=True)
        inspector = tk.Frame(workspace, bg=PANEL, width=268, highlightbackground=BORDER, highlightthickness=1)
        inspector.pack(side="right", fill="y", padx=(16, 0))
        inspector.pack_propagate(False)
        self._inspector(inspector)
        center = tk.Frame(workspace, bg=BG)
        center.pack(side="left", fill="both", expand=True)
        toolbar = tk.Frame(center, bg=BG)
        toolbar.pack(fill="x", pady=(0, 10))
        self.map_title = label(toolbar, "Port & pin map", size=13, bold=True)
        self.map_title.pack(side="left")
        for text, color in [("Pass", GREEN), ("Fail", RED), ("Checking", AMBER), ("Excluded", "#52647e")]:
            label(toolbar, "● " + text, color=color, size=8).pack(side="right", padx=(10, 0))
        maps = tk.Frame(center, bg=BG)
        maps.pack(fill="both", expand=True)
        self.map = PortMap(maps, self)
        scroll = ttk.Scrollbar(maps, orient="vertical", command=self.map.yview)
        self.map.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.map.pack(side="left", fill="both", expand=True)
        label(center, "Probe pin numbers follow the schematic; diagram is not a connector mating view.",
              size=8, color=MUTED).pack(anchor="w", pady=(8, 0))
        self._log(body)
        self.render()

    def _theme(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Station.TCombobox", fieldbackground=INSET, background=BORDER,
                        foreground=TEXT, arrowcolor=MUTED, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, padding=7, selectbackground=PANEL,
                        selectforeground=TEXT, insertcolor=TEXT)
        style.map("Station.TCombobox", fieldbackground=[("readonly", INSET)],
                  foreground=[("readonly", TEXT)], selectbackground=[("readonly", INSET)])
        style.configure("Vertical.TScrollbar", background=BORDER, troughcolor=BG,
                        bordercolor=BG, arrowcolor=MUTED, lightcolor=BORDER, darkcolor=BORDER,
                        width=9, arrowsize=10)
        self.root.option_add("*TCombobox*Listbox.background", INSET)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#284b78")

    def _nav(self, parent, text, key):
        row = tk.Frame(parent, bg=SIDEBAR, cursor="hand2")
        row.pack(fill="x", padx=10, pady=2)
        title = label(row, text, size=10, color=MUTED, padx=10, pady=7, anchor="w")
        title.pack(side="left", fill="x", expand=True)
        count = label(row, "", size=9, color=MUTED, padx=10)
        count.pack(side="right")
        for widget in (row, title, count):
            widget.bind("<Button-1>", lambda _, k=key: self.filter(k))
        self.nav_items[key] = (row, title, count)

    def _connections(self, parent):
        box = tk.Frame(parent, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        box.pack(fill="x", pady=(18, 0))
        inside = tk.Frame(box, bg=PANEL)
        inside.pack(fill="x", padx=16, pady=12)
        for role, title, variable in [("source", "PIXNOVA / SOURCE", self.station.source_port),
                                      ("probe", "TEST BOARD / PROBE (optional)", self.station.probe_port)]:
            endpoint = tk.Frame(inside, bg=PANEL)
            endpoint.pack(side="left", fill="x", expand=True, padx=(0, 14))
            caption = tk.Frame(endpoint, bg=PANEL)
            caption.pack(fill="x", pady=(0, 5))
            label(caption, title, size=8, color=MUTED, bold=True).pack(side="left")
            status = label(caption, "● Offline", size=8, color=MUTED)
            status.pack(side="right", padx=(10, 0))
            setattr(self, role + "_status", status)
            combo = ttk.Combobox(endpoint, textvariable=variable, width=20, style="Station.TCombobox", font=(MONO, 10))
            combo.pack(fill="x")
            setattr(self, role + "_combo", combo)
        Action(inside, "↻  Refresh", self.station.refresh_ports, width=94).pack(side="left", padx=(4, 8), pady=(17, 0))
        Action(inside, "Connect USB", self.station.connect, width=110).pack(side="left", padx=(0, 8), pady=(17, 0))
        Action(inside, "Self-check", self.station.self_check, width=100).pack(side="left", pady=(17, 0))
        status_row = tk.Frame(box, bg=INSET)
        status_row.pack(fill="x")
        self.state_label = label(status_row, variable=self.station.state, color=BLUE, size=9, anchor="w", padx=14, pady=8)
        self.state_label.pack(fill="x")

    def _inspector(self, parent):
        canvas = tk.Canvas(parent, bg=PANEL, highlightthickness=0)
        self.inspector_scroll = canvas
        scroll = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=PANEL)
        window = canvas.create_window(16, 16, window=inner, width=222, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=max(180, event.width - 32)))
        inner.bind("<Configure>", lambda _: canvas.configure(scrollregion=(0, 0, canvas.winfo_width(), inner.winfo_reqheight() + 32)))
        self.inspector_heading = label(inner, "PIN INSPECTOR", size=8, color=MUTED, bold=True)
        self.inspector_heading.pack(anchor="w")
        self.inspector_title = label(inner, "", size=16, bold=True, anchor="w")
        self.inspector_title.pack(fill="x", pady=(9, 0))
        self.inspector_signal = label(inner, "", size=10, color=MUTED, anchor="w")
        self.inspector_signal.pack(fill="x", pady=(4, 12))
        self.inspector_badge = label(inner, "", size=10, bold=True, padx=10, pady=7)
        self.inspector_badge.pack(anchor="w")
        self.path = tk.Canvas(inner, height=116, bg=PANEL, highlightthickness=0)
        self.path.pack(fill="x", pady=(8, 8))
        self.detail_values = {}
        self.detail_labels = {}
        for key, title in [("expected", "Expected"), ("measured", "Measured"), ("error", "Error"), ("window", "Valid windows")]:
            row = tk.Frame(inner, bg=PANEL)
            row.pack(fill="x", pady=3)
            caption = label(row, title, size=9, color=MUTED)
            caption.pack(side="left")
            self.detail_labels[key] = caption
            value = label(row, "—", size=11, bold=True)
            value.pack(side="right")
            self.detail_values[key] = value
        self.reason = label(inner, "", size=9, color=MUTED, wraplength=214, justify="left", anchor="nw")
        self.reason.pack(fill="x", pady=(14, 0))
        self.pin_note = label(inner, "", size=8, color=MUTED, wraplength=214, justify="left", anchor="nw")
        self.pin_note.pack(fill="x", pady=(8, 0))
        def scroll_inspector(event):
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"

        def bind_wheel(widget):
            widget.bind("<MouseWheel>", scroll_inspector)
            widget.bind("<Button-4>", lambda _: canvas.yview_scroll(-1, "units"))
            widget.bind("<Button-5>", lambda _: canvas.yview_scroll(1, "units"))
            for child in widget.winfo_children():
                bind_wheel(child)
        bind_wheel(canvas)

    def _peripherals(self, parent):
        section = tk.Frame(parent, bg=BG)
        section.pack(fill="x", pady=(14, 0))
        heading = tk.Frame(section, bg=BG)
        heading.pack(fill="x", pady=(0, 7))
        label(heading, "PIXNOVA · ONBOARD CHECKS", size=10, bold=True).pack(side="left")
        self.peripheral_summary = label(heading, "Waiting for PixNova", size=9, color=MUTED)
        self.peripheral_summary.pack(side="right")
        tabs = tk.Frame(section, bg=BG)
        tabs.pack(fill="x", pady=(0, 8))
        for group in ("Sensors", "Storage"):
            button = Action(tabs, group, lambda key=group: self.select_peripheral_group(key), width=114)
            button.pack(side="left", padx=(0, 8))
            self.peripheral_buttons[group] = button
        label(tabs, "Bus / CHIP ID · Read-only storage", color=MUTED, size=8).pack(side="left", padx=8)
        if self.station.demo:
            tk.Checkbutton(tabs, text="DEMO: ICM45686 #2 fault", variable=self.station.demo_peripheral_fault,
                           command=self.station.simulate_peripheral_fault, bg=BG, fg=AMBER,
                           activebackground=BG, activeforeground=AMBER, selectcolor=INSET,
                           highlightthickness=0, font=(FONT, 9)).pack(side="right")
        for group, devices in (("Sensors", SENSORS), ("Storage", STORAGE)):
            cards = tk.Frame(section, bg=BG)
            self.peripheral_groups[group] = cards
            self._device_cards(cards, devices)
        self.peripheral_groups["Sensors"].pack(fill="x")

    def _device_cards(self, cards, devices):
        for column, (name, spec) in enumerate(devices.items()):
            cards.columnconfigure(column, weight=1, uniform="peripheral")
            card = tk.Frame(cards, bg=PANEL, highlightbackground=BORDER, highlightthickness=1, cursor="hand2")
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 4, 0 if column == len(devices) - 1 else 4))
            title = label(card, spec["name"], size=11, bold=True, anchor="w")
            title.pack(fill="x", padx=10, pady=(9, 2))
            bus = label(card, spec["bus"], size=8, color=MUTED, anchor="w")
            bus.pack(fill="x", padx=10)
            state = label(card, "● WAITING", size=10, bold=True, color=MUTED, anchor="w")
            state.pack(fill="x", padx=10, pady=(5, 2))
            history = label(card, "Power-on: —", size=8, color=MUTED, anchor="w")
            history.pack(fill="x", padx=10, pady=(0, 8))
            for widget in (card, title, bus, state, history):
                widget.bind("<Button-1>", lambda _, key=name: self.select_sensor(key))
            self.sensor_cards[name] = {"frame": card, "title": title, "bus": bus, "state": state, "history": history}

    def select_peripheral_group(self, group):
        self.peripheral_groups[self.peripheral_group].pack_forget()
        self.peripheral_group = group
        self.peripheral_groups[group].pack(fill="x")
        self.render()

    def select_sensor(self, name):
        self.selected_sensor = name
        group = "Storage" if name in STORAGE else "Sensors"
        if self.peripheral_group != group:
            self.select_peripheral_group(group)
        self.inspector_scroll.yview_moveto(0)
        self.render()

    def _render_peripherals(self):
        model = self.station.peripherals
        online = "source" in self.station.hello
        passed = sum(row["status"] == "PASS" for row in model.latest.values())
        failed = any(row["status"] == "FAIL" for row in model.latest.values())
        boot_failed = "source" in model.summaries and not model.passed(("source",))
        text = f"POWER-ON {'PASS' if model.passed(('source',)) else 'FAIL' if boot_failed else 'PENDING'} · {passed}/{len(DEVICES)} latest PASS" if online else "Connect PixNova USB to read power-on checks"
        self.peripheral_summary.configure(text=text, fg=MUTED if not online else RED if failed or boot_failed else GREEN if passed == len(DEVICES) else AMBER)
        for group, devices in (("Sensors", SENSORS), ("Storage", STORAGE)):
            good = sum(model.latest[name]["status"] == "PASS" for name in devices) if online else 0
            bad = online and any(model.latest[name]["status"] == "FAIL" for name in devices)
            button = self.peripheral_buttons[group]
            button.text = f"{group} {good}/{len(devices)}"
            button.fg = MUTED if not online else RED if bad else GREEN if good == len(devices) else AMBER
            button.fill = "#1d3b5c" if self.peripheral_group == group else "#1a2a41"
            button.paint()
        for name, widgets in self.sensor_cards.items():
            result = model.latest[name]
            status = result["status"] if online else "WAITING"
            color, fill = STATE_COLORS[status]
            bg = fill if online and status in ("PASS", "FAIL") else PANEL
            widgets["frame"].configure(bg=bg, highlightbackground=color if status == "FAIL" else BLUE if self.selected_sensor == name else BORDER)
            for key in ("title", "bus", "state", "history"):
                widgets[key].configure(bg=bg)
            chip_id = " · " + display_value(DEVICES[name], result.get("detail")) if result.get("detail") is not None else ""
            widgets["state"].configure(text="● " + (status if online else "OFFLINE") + chip_id, fg=color)
            boot = model.power["source"].get(name, {}).get("status", "—")
            widgets["history"].configure(text=f"BOOT {boot} · {'PRE-TEST' if result['stage'] == 'PRE-TEST' else 'BOOT'}")

    def _log(self, parent):
        heading = tk.Frame(parent, bg=BG)
        heading.pack(fill="x", pady=(14, 7))
        label(heading, "EVENT LOG", size=8, color=MUTED, bold=True).pack(side="left")
        if self.station.demo:
            tk.Checkbutton(heading, text="DEMO: fault on TELEM1 pin 4 / PE8", variable=self.station.demo_fault,
                           command=lambda: self.select(0), bg=BG, fg=AMBER, activebackground=BG,
                           activeforeground=AMBER, selectcolor=INSET, font=(FONT, 9),
                           anchor="w", highlightthickness=0).pack(side="left", padx=18)
        label(heading, f"±{self.station.profile['tolerance_percent']:g}% tolerance · {self.station.profile['required_windows']} valid windows for pass",
              size=8, color=MUTED).pack(side="right")
        self.log = tk.Text(parent, height=3, wrap="word", bg=INSET, fg=MUTED, insertbackground=TEXT,
                           relief="flat", bd=0, padx=12, pady=8, font=(MONO, 9), state="disabled", highlightthickness=0)
        self.log.pack(fill="x")

    def filter(self, key):
        self.faults_only = key == "failed"
        self.filter_group = None if key in (None, "failed") else key
        candidates = [i for i, result in enumerate(self.station.assessment.results) if result.status == "FAIL"] if self.faults_only else self.groups.get(key, [])
        if candidates and self.selected not in candidates:
            self.selected = candidates[0]
        self.map.yview_moveto(0)
        self.render()

    def select(self, index):
        self.selected_sensor = None
        self.selected = index
        self.render()

    def render(self):
        results = self.station.assessment.results
        passed = sum(result.status == "PASS" for result in results)
        failed = sum(result.status == "FAIL" for result in results)
        for key, value in [("total", len(results)), ("pass", passed), ("fail", failed), ("pending", len(results) - passed - failed)]:
            self.metric_labels[key].configure(text=str(value).zfill(2))
        for key, (frame, title, count) in self.nav_items.items():
            selected = key == "failed" if self.faults_only else key == self.filter_group
            bg = "#1b2e49" if selected else SIDEBAR
            indices = self.groups.get(key, range(len(results)))
            bad = sum(results[i].status == "FAIL" for i in indices)
            count.configure(text=f"{bad}/{len(indices)}" if bad and key != "failed" else str(bad if key == "failed" else len(indices)),
                            fg=RED if bad else MUTED, bg=bg)
            title.configure(fg=RED if bad and key is not None else TEXT if selected else MUTED, bg=bg)
            frame.configure(bg=bg)
        self.map_title.configure(text="Failed port & pin map" if self.faults_only else self.filter_group or "Port & pin map")
        for role in ("source", "probe"):
            online = role in self.station.hello
            status = "Demo" if self.station.demo else "Verified" if online else "Offline"
            getattr(self, role + "_status").configure(text="● " + status, fg=AMBER if self.station.demo else GREEN if online else MUTED)
        self.map.render()
        self._render_peripherals()
        self._render_inspector()

    def _render_inspector(self):
        if self.selected_sensor is not None:
            self._render_sensor_inspector()
            return
        row = self.station.profile["channels"][self.selected]
        result = self.station.assessment.results[self.selected]
        signature = ("pin", self.selected, result.status, result.frequency_hz, result.error_percent, result.streak, result.reason)
        if self._inspector_signature == signature:
            return
        self._inspector_signature = signature
        color, fill = STATE_COLORS[result.status]
        self.inspector_heading.configure(text="PIN INSPECTOR")
        for key, text in [("expected", "Expected"), ("measured", "Measured"), ("error", "Error"), ("window", "Valid windows")]:
            self.detail_labels[key].configure(text=text)
        self.detail_values["expected"].configure(fg=TEXT)
        self.detail_values["window"].configure(fg=TEXT)
        self.inspector_title.configure(text=row["group"])
        self.inspector_signal.configure(text=f"{signal_name(row)}  /  {row['probe_connector']} pin {row['probe_connector_pin']}")
        self.inspector_badge.configure(text="● " + result.status, fg=color, bg=fill)
        self.detail_values["expected"].configure(text=f"{row['expected_hz']:.3f} Hz")
        self.detail_values["measured"].configure(text=f"{result.frequency_hz:.3f} Hz" if result.frequency_hz is not None else "—", fg=color)
        self.detail_values["error"].configure(text=f"{result.error_percent:+.2f}%" if result.error_percent is not None else "—", fg=color)
        self.detail_values["window"].configure(text=f"{min(result.streak, self.station.profile['required_windows'])} / {self.station.profile['required_windows']}")
        self.reason.configure(text=result.reason or "Waiting for startup checks and measurement.", fg=RED if result.status == "FAIL" else MUTED)
        self.pin_note.configure(text=("Signal path failed. Check the source, wiring and probe.\n\n" if result.status == "FAIL" else "") + row["note"])
        canvas = self.path
        canvas.delete("all")
        width = 216
        for y, caption, value, tag in [(2, "PIXNOVA / FMU", row["source_pin"], "source"),
                                       (72, f"PROBE {row['probe_connector']} · PIN {row['probe_connector_pin']}", row["probe_pin"], "probe")]:
            rounded(canvas, 1, y, width - 1, y + 42, fill=fill, outline=color, tags=(f"inspector:{tag}",))
            canvas.create_text(13, y + 12, text=caption, anchor="w", fill=MUTED, font=(FONT, 8))
            canvas.create_text(13, y + 29, text=value, anchor="w", fill=color, font=(MONO, 14, "bold"))
        canvas.create_line(108, 46, 108, 68, fill=color, width=2, arrow="last")
        canvas.create_text(120, 57, text="signal", anchor="w", fill=MUTED, font=(FONT, 8))

    def _render_sensor_inspector(self):
        name = self.selected_sensor
        spec, model = DEVICES[name], self.station.peripherals
        result = model.latest[name]
        boot = model.power["source"].get(name, {})
        online = "source" in self.station.hello
        signature = ("sensor", name, repr(result), repr(boot), online)
        if self._inspector_signature == signature:
            return
        self._inspector_signature = signature
        color, fill = STATE_COLORS[result["status"] if online else "WAITING"]
        self.inspector_heading.configure(text="PERIPHERAL INSPECTOR")
        self.inspector_title.configure(text=spec["name"])
        self.inspector_signal.configure(text=spec["bus"])
        self.inspector_badge.configure(text="● " + (result["status"] if online else "OFFLINE"), fg=color, bg=fill)
        storage = name in STORAGE
        for key, text in [("expected", "Check target" if storage else "Expected ID"), ("measured", "Observed" if storage else "Read ID"), ("error", "Power-on"), ("window", "Latest check")]:
            self.detail_labels[key].configure(text=text)
        self.detail_values["expected"].configure(text=spec.get("target", f"0x{spec['id']:02X}"), fg=TEXT)
        self.detail_values["measured"].configure(text=display_value(spec, result.get("detail")), fg=color)
        self.detail_values["error"].configure(text=boot.get("status", "—"), fg=GREEN if boot.get("pass") else RED if boot else MUTED)
        self.detail_values["window"].configure(text=result["stage"], fg=MUTED)
        reason = ("Read-only storage checks passed." if storage else "CHIP ID matches the expected sensor.") if result["status"] == "PASS" else ("Device missing, unreadable or unexpected geometry." if storage else "No response or unexpected CHIP ID.") if result["status"] == "FAIL" else "Waiting for the peripheral check."
        self.reason.configure(text=reason if online else "Flight controller disconnected; showing previous check details.", fg=color)
        summary = model.summaries.get("source", {})
        timing = f"Power-on check at {summary['checked_us'] / 1e6:.3f} s after boot.\n\n" if "checked_us" in summary else ""
        self.pin_note.configure(text=timing + spec.get("note", "Bus and identity checks only. MEMS self-test, calibration and measurement accuracy are not checked."))
        self.path.delete("all")
        for y, title, value, tag in [(2, "PIXNOVA INTERNAL BUS", spec["bus"], "bus"), (72, "ONBOARD STORAGE" if storage else "ONBOARD SENSOR", spec["name"], "sensor")]:
            rounded(self.path, 1, y, 215, y + 42, fill=fill, outline=color, tags=("inspector:" + tag,))
            self.path.create_text(13, y + 12, text=title, anchor="w", fill=MUTED, font=(FONT, 8))
            self.path.create_text(13, y + 29, text=value, anchor="w", fill=color, font=(FONT, 11, "bold"))
        self.path.create_line(108, 46, 108, 68, fill=color, width=2, arrow="last")
