"""Shared adapter controls for the ordinary and protocol station windows."""
import tkinter as tk
from tkinter import ttk

from dashboard import Action, label, BLUE, GREEN, RED, MUTED, MONO
from network_config import NetworkSettings, NetworkWorker, TEST_ADDRESS


class DemoNetwork:
    def __init__(self): self.configured = False
    def adapters(self):
        addresses = [{"address": "10.0.0.10", "prefix": 24, "state": "Preferred"}]
        if self.configured: addresses.append({"address": TEST_ADDRESS, "prefix": 24, "state": "Preferred"})
        return [{"guid": "00000000-0000-0000-0000-000000000001", "index": 1, "name": "DEMO Ethernet",
                 "description": "Simulated adapter", "status": "Up", "ipv4": addresses, "dhcp": "Enabled", "dns": [], "gateways": []}]
    def status(self, guid):
        return {"adapter": self.adapters()[0], "ready": self.configured, "owned": self.configured,
                "address": {"index": 1} if self.configured else None,
                "message": "DEMO: test address ready" if self.configured else "DEMO: test address not configured"}
    def change(self, action, guid):
        self.configured = action == "configure"
        return self.status(guid)
    def ready_index(self, guid): return 1


class NetworkPanel(tk.Frame):
    def __init__(self, parent, demo=False, can_change=lambda: True):
        super().__init__(parent, bg=parent.cget("bg"))
        self.root, self.demo, self.can_change = parent.winfo_toplevel(), demo, can_change
        self.backend = DemoNetwork() if demo else NetworkSettings()
        self.worker = NetworkWorker(self.backend)
        self.rows, self.labels, self.last_status = {}, {}, {}
        self.pending_test, self.closed = None, False
        self.selection = tk.StringVar()
        controls = tk.Frame(self, bg=self.cget("bg")); controls.pack(fill="x")
        label(controls, "PC Ethernet", size=9).pack(side="left", padx=(0, 8))
        self.combo = ttk.Combobox(controls, textvariable=self.selection, state="readonly", font=(MONO, 9), width=42, style="Station.TCombobox")
        self.combo.pack(side="left", fill="x", expand=True)
        self.combo.bind("<<ComboboxSelected>>", self.selected)
        Action(controls, "Refresh", self.refresh, width=78).pack(side="left", padx=(7, 4))
        Action(controls, "Configure IP", self.configure, width=112).pack(side="left", padx=4)
        Action(controls, "Restore", self.restore, width=80).pack(side="left", padx=(4, 0))
        self.status = tk.StringVar(value="Detecting physical Ethernet adapters…")
        self.status_label = label(self, variable=self.status, size=9, color=MUTED, anchor="w", justify="left")
        self.status_label.pack(fill="x", pady=(4, 0))
        self.status_label.bind("<Configure>", lambda event: self.status_label.configure(wraplength=max(260, event.width - 8)))
        self.after_id = self.root.after(100, self.poll)
        self.refresh()

    def guid(self): return self.labels.get(self.selection.get())

    @property
    def changing(self): return self.worker.busy and self.worker.action in ("configure", "restore")

    def display(self, message, error=False):
        self.status.set(message)
        self.status_label.configure(fg=RED if error else GREEN if self.last_status.get("ready") else BLUE)

    def update_rows(self, rows):
        selected = self.guid()
        self.rows = {row["guid"]: row for row in rows}
        self.labels = {f"{row['name']} · {row['status']} · {', '.join(ip['address'] for ip in row['ipv4']) or 'no IPv4'} [{row['index']}]": row["guid"] for row in rows}
        self.combo["values"] = list(self.labels)
        if selected not in self.rows:
            matches = [row["guid"] for row in rows if any(ip["address"] == TEST_ADDRESS for ip in row["ipv4"])]
            selected = matches[0] if len(matches) == 1 else next(iter(self.rows), None) if len(self.rows) == 1 else None
        self.selection.set(next((name for name, guid in self.labels.items() if guid == selected), ""))

    def refresh(self):
        if self.worker.request("refresh"): self.display("Reading Ethernet adapters…")

    def selected(self, _=None):
        self.pending_test, self.last_status = None, {}
        if self.guid() and self.worker.request("status", self.guid()): self.display("Checking selected adapter…")

    def change(self, action):
        if not self.can_change():
            self.cancel_test()
            self.display("Stop the active test before changing Ethernet settings", True)
            return
        guid = self.guid()
        if not guid: self.display("Select the PC Ethernet adapter connected to PixNova", True); return
        if self.worker.request(action, guid):
            self.combo.configure(state="disabled")
            self.display("Preparing adapter; Windows may request administrator permission…")
        else:
            self.display("Adapter operation is in progress")

    def configure(self): self.change("configure")

    def restore(self):
        self.cancel_test()
        self.change("restore")

    def prepare(self, callback):
        self.pending_test = (self.guid(), callback)
        if self.worker.busy:
            self.display("Waiting for adapter status before Ethernet test…")
        elif self.guid():
            self.worker.request("status", self.guid())
        else:
            self.cancel_test()
            self.display("Select the PC Ethernet adapter before testing", True)

    def cancel_test(self): self.pending_test = None

    def poll(self):
        if self.closed: return
        event = self.worker.poll()
        if event:
            self.combo.configure(state="readonly")
            if "error" in event:
                self.cancel_test(); self.last_status = {}
                self.display(event["error"], True)
            elif event["action"] == "refresh":
                self.update_rows(event["value"])
                if self.guid(): self.worker.request("status", self.guid())
                else: self.display("No physical Ethernet adapter found; connect a PC Ethernet adapter")
            else:
                if event["guid"] != self.guid():
                    if self.guid(): self.worker.request("status", self.guid())
                    self.after_id = self.root.after(100, self.poll)
                    return
                self.last_status = event["value"]
                adapter = self.last_status["adapter"]
                self.update_rows([adapter if row["guid"] == adapter["guid"] else row for row in self.rows.values()])
                self.display(f"PC link {adapter['status']} · {self.last_status['message']} · DHCP {adapter['dhcp']}")
                if self.pending_test:
                    guid, callback = self.pending_test
                    if guid is not None and guid != self.guid():
                        self.cancel_test()
                    elif self.last_status.get("ready"):
                        self.cancel_test()
                        try: callback(self.backend.ready_index(self.guid()))
                        except (ValueError, OSError, RuntimeError) as error: self.display(str(error), True)
                    elif event["action"] == "status":
                        self.change("configure")
                    else:
                        self.cancel_test()
        self.after_id = self.root.after(100, self.poll)

    def snapshot(self):
        return {"guid": self.guid(), "demo": self.demo, **self.last_status}

    def close(self):
        if self.closed: return
        self.closed = True
        self.cancel_test()
        self.root.after_cancel(self.after_id)
