"""Read-only temperature graph. Never imports or connects to Phidgets."""

import csv
import math
from collections import deque
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"
COLUMNS = ("TI_0_C", "TI_1_C", "TI_2_C", "TI_3_C", "avg_C")
COLORS = ("#2563eb", "#dc2626", "#15803d", "#9333ea", "#111827")
MAX_POINTS = 360  # About 30 minutes at the existing 5-second log interval.


def read_samples(path):
    """Ignore unfinished or invalid rows while the controller writes its CSV."""
    samples = deque(maxlen=MAX_POINTS)
    with path.open(newline="") as log:
        lines = log.readlines()
    if lines and not lines[-1].endswith("\n"):
        lines.pop()
    for row in csv.DictReader(lines):
        try:
            timestamp = datetime.fromisoformat(row["timestamp"])
            values = tuple(float(row[column]) for column in COLUMNS)
            if timestamp.tzinfo is not None and all(math.isfinite(v) for v in values):
                samples.append((timestamp, values))
        except (KeyError, TypeError, ValueError):
            continue
    return samples


class TemperatureGraph:
    def __init__(self, root, tk):
        self.root = root
        self.signature = None
        self.samples = deque(maxlen=MAX_POINTS)
        root.title("Heater temperatures - read-only log viewer")
        root.geometry("960x580")
        root.minsize(640, 400)
        tk.Label(root, text="Temperatures (C) | latest 360 logged readings",
                 font=("Segoe UI", 13, "bold")).pack(pady=(12, 4))
        legend = tk.Frame(root)
        legend.pack()
        for name, color in zip(COLUMNS, COLORS):
            tk.Label(legend, text=name, fg=color, font=("Segoe UI", 11, "bold")).pack(
                side="left", padx=12)
        self.canvas = tk.Canvas(root, bg="white", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=10, pady=10)
        self.status = tk.Label(root, text="Waiting for logs...", anchor="w", wraplength=900)
        self.status.pack(fill="x", padx=12, pady=(0, 10))
        self.canvas.bind("<Configure>", lambda event: self.draw())
        self.poll()

    def poll(self):
        try:
            paths = sorted(LOG_DIR.glob("*/data.csv"))
            if not paths:
                self.status.config(text="Waiting for a controller run. Start heater.py separately.")
            else:
                path = paths[-1]
                stat = path.stat()
                signature = (path, stat.st_mtime_ns, stat.st_size)
                if signature != self.signature:
                    self.samples = read_samples(path)
                    self.signature = signature
                    self.draw()
                if self.samples:
                    timestamp, values = self.samples[-1]
                    age = max(0, (datetime.now().astimezone() - timestamp).total_seconds())
                    readings = " | ".join(f"{name}: {value:.2f}" for name, value in zip(COLUMNS, values))
                    note = " | No new readings; controller may be stopped." if age > 15 else ""
                    self.status.config(text=f"Run {path.parent.name}\n{readings}\n"
                                       f"Last sample {timestamp:%H:%M:%S} ({age:.0f}s ago){note}")
                else:
                    self.status.config(text=f"Run {path.parent.name}: waiting for the first complete CSV row.")
        except (OSError, csv.Error, UnicodeError) as error:
            self.status.config(text=f"Cannot read logs: {error}. Retrying...")
        self.root.after(1000, self.poll)

    def draw(self):
        canvas = self.canvas
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 200 or height < 150:
            return
        left, top, right, bottom = 65, 20, width - 25, height - 50
        if not self.samples:
            canvas.create_text(width / 2, height / 2, text="Waiting for temperature data...")
            return
        values = [value for _, row in self.samples for value in row]
        low, high = min(values), max(values)
        padding = max(1.0, (high - low) * 0.1)
        low, high = low - padding, high + padding
        start = self.samples[0][0]
        elapsed = [(timestamp - start).total_seconds() for timestamp, _ in self.samples]
        duration = max(5.0, max(elapsed))
        for step in range(6):
            fraction = step / 5
            y = bottom - fraction * (bottom - top)
            canvas.create_line(left, y, right, y, fill="#e5e7eb")
            canvas.create_text(left - 8, y, anchor="e", text=f"{low + fraction * (high - low):.1f}")
            x = left + fraction * (right - left)
            canvas.create_text(x, bottom + 18, text=f"{fraction * duration / 60:.1f}")
        canvas.create_line(left, top, left, bottom, right, bottom, fill="#6b7280")
        canvas.create_text((left + right) / 2, height - 10,
                           text=f"Minutes since {start:%H:%M:%S} (first visible sample)")
        for index, color in enumerate(COLORS):
            points = []
            for seconds, (_, row) in zip(elapsed, self.samples):
                x = left + seconds / duration * (right - left)
                y = bottom - (row[index] - low) / (high - low) * (bottom - top)
                points.extend((x, y))
            if len(points) > 2:
                canvas.create_line(*points, fill=color, width=3 if index == 4 else 2)
            x, y = points[-2:]
            canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill=color, outline=color)


def main():
    try:
        import tkinter as tk
    except ImportError:
        raise SystemExit("Graphing requires tkinter. Modify the Python 3.11 installation "
                         "and enable 'tcl/tk and IDLE', then run graph.py again.")
    root = tk.Tk()
    TemperatureGraph(root, tk)
    root.mainloop()


if __name__ == "__main__":
    main()
