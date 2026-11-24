"""Keep the USB poll running while Tk's native serial-port dropdown has focus."""
from pathlib import Path
import sys
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Station


root = tk.Tk()
station = Station(root, Path(sys.path[0]) / "profile.json")
root.title("PixNova serial-port dropdown regression")
failures = []
finished = False


def callback_error(_kind, error, _traceback):
    failures.append(error)


def exercise_dropdown():
    combo = station.probe_combo
    combo["values"] = ("COM9", "COM145")
    root.tk.call("ttk::combobox::Post", str(combo))
    popdown = root.tk.call("ttk::combobox::PopdownWindow", str(combo))
    listbox = f"{popdown}.f.l"

    def test_focus():
        root.tk.call("focus", "-force", listbox)
        assert str(root.tk.call("focus")) == listbox
        previous_poll = station.last_pump_at

        def check():
            global finished
            try:
                assert str(root.tk.call("focus")) == listbox
                assert station.last_pump_at > previous_poll
                assert not failures, failures
                finished = True
            finally:
                root.tk.call("ttk::combobox::Unpost", str(combo))
                station.close()

        root.after(350, check)

    root.after(100, test_focus)


root.report_callback_exception = callback_error
root.after(300, exercise_dropdown)
root.after(5000, station.close)
root.mainloop()
if failures:
    raise failures[0]
assert finished, "Dropdown check did not complete"
print("GUI: USB polling continued while the native serial-port dropdown had focus")
