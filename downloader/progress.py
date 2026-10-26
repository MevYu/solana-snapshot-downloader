"""Small, dependency-free progress display for terminals and redirected logs."""

import math
import os
import shutil
import sys
import threading
import time
import unicodedata


def _text(value):
    return " ".join("".join(char if char.isprintable() else " " for char in str(value)).split())


def _duration(seconds):
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return "{}:{:02d}:{:02d}".format(hours, minutes, seconds)
    return "{}:{:02d}".format(minutes, seconds)


def _bytes(value, total=None):
    value = max(0, value)
    largest = max(value, total or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if largest < 1024 or unit == "PiB":
            number = "{:.0f}" if unit == "B" else "{:.1f}"
            count = number.format(value)
            if total is not None:
                count += "/" + number.format(total)
            return count + " " + unit
        largest /= 1024
        value /= 1024
        if total is not None:
            total /= 1024


def _truncate(text, columns):
    """Leave the last terminal column unused, including for wide characters."""
    result = []
    available = max(0, columns - 1)
    for char in text:
        width = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in "WF" else 1)
        if width > available:
            break
        result.append(char)
        available -= width
    return "".join(result)


class ProgressDisplay:
    """Render one active stage on stderr, refreshing while network calls wait.

    ``speed`` is bytes per second. Completing a byte count does not mark a stage
    successful: callers must call ``finish`` after their operation returns.
    """

    def __init__(self, stream=None, interval=0.2, log_interval=5.0):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("interval must be positive and finite")
        if not math.isfinite(log_interval) or log_interval <= 0:
            raise ValueError("log_interval must be positive and finite")
        self.stream = sys.stderr if stream is None else stream
        self.interval = interval
        self.log_interval = log_interval
        self._tty = self.stream.isatty() and os.environ.get("TERM") != "dumb"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._closed = False
        self._active = False
        self._visible = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def start(self, label, total=None, unit="nodes", detail=""):
        with self._lock:
            if self._closed:
                raise RuntimeError("progress display is closed")
            if self._active:
                self._render(force=True, final=True)
            self._label = _text(label)
            self._total = None if total is None else max(0, total)
            self._unit = _text(unit)
            self._detail = _text(detail)
            self._completed = 0
            self._speed = None
            self._started = time.monotonic()
            self._last_render = float("-inf")
            self._active = True
            self._render(force=True)
            if self._thread is None:
                self._thread = threading.Thread(target=self._refresh, name="snapshot-progress", daemon=True)
                self._thread.start()

    def update(self, completed, total=None, detail="", speed=None):
        with self._lock:
            if self._closed or not self._active:
                return
            total_changed = total is not None and total != self._total
            detail = _text(detail)
            detail_changed = detail != self._detail
            self._completed = max(0, completed)
            if total is not None:
                self._total = max(0, total)
            self._detail = detail
            self._speed = speed
            self._render(force=total_changed or (self._tty and detail_changed))

    def finish(self, message=None):
        """Finish the line without inferring success or changing the count."""
        with self._lock:
            if self._closed or not self._active:
                return
            self._render(force=True, final=True)
            self._active = False
            if message is not None:
                self.stream.write(_text(message) + "\n")
                self.stream.flush()

    def message(self, text):
        with self._lock:
            if self._closed:
                return
            if self._visible:
                self.stream.write("\r\033[2K")
                self._visible = False
            self.stream.write(_text(text) + "\n")
            if self._tty and self._active:
                self._render(force=True)
            else:
                self.stream.flush()

    def close(self):
        self._stop.set()
        try:
            with self._lock:
                if not self._closed:
                    self._closed = True
                    active, self._active = self._active, False
                    if active:
                        self._render(force=True, final=True)
        finally:
            if self._thread is not None and self._thread is not threading.current_thread():
                self._thread.join()

    def _refresh(self):
        while not self._stop.wait(self.interval):
            with self._lock:
                if self._active and not self._closed:
                    self._render()

    def _line(self, now):
        elapsed = max(0, now - self._started)
        if self._total is None:
            indicator = "|/-\\"[int(elapsed / self.interval) % 4]
        else:
            fraction = min(1, self._completed / self._total) if self._total else 0
            filled = int(fraction * 10)
            indicator = "[" + "#" * filled + "-" * (10 - filled) + "]"
        if self._unit == "bytes":
            count = _bytes(self._completed, self._total)
            if self._total is not None:
                count = "{:3.0f}% ".format(fraction * 100) + count
            speed = self._speed
            if speed is None:
                speed = self._completed / elapsed if elapsed else 0
            speed = speed if math.isfinite(speed) and speed > 0 else 0
            eta = "--:--"
            if self._total is not None and speed:
                eta = _duration(math.ceil(max(0, self._total - self._completed) / speed))
            count += " {:.1f} MiB/s ETA {}".format(speed / (1024 * 1024), eta)
        else:
            count = "{:,}".format(int(self._completed))
            if self._total is not None:
                count += "/{:,}".format(int(self._total))
            count += " " + self._unit
        line = "{} {} {}".format(self._label, indicator, count)
        if self._unit != "bytes":
            line += " elapsed " + _duration(elapsed)
        if self._detail:
            line += " | " + self._detail
        return line

    def _render(self, force=False, final=False):
        now = time.monotonic()
        refresh_interval = self.interval if self._tty else self.log_interval
        if not force and now - self._last_render < refresh_interval:
            return
        line = self._line(now)
        if self._tty:
            try:
                columns = os.get_terminal_size(self.stream.fileno()).columns
            except (AttributeError, OSError, ValueError):
                columns = shutil.get_terminal_size((80, 24)).columns
            self.stream.write("\r\033[2K" + _truncate(line, columns) + ("\n" if final else ""))
            self._visible = not final
        else:
            self.stream.write(line + "\n")
        self.stream.flush()
        self._last_render = now
