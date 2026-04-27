"""Clipboard image watcher with tray icon and optional R2 upload."""

import hashlib
import os
import sys
import threading
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageGrab

import pystray
from pystray import Menu, MenuItem

import customtkinter as ctk

try:
    import pyperclip
except ImportError:
    pyperclip = None

try:
    import boto3
    _BOTO_AVAILABLE = True
except ImportError:
    _BOTO_AVAILABLE = False

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass


# ===== CONFIG =====
SAVE_DIR = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Pictures" / "ClipShots"
LOG_FILE = SAVE_DIR / "watcher.log"
MODE_FILE = SAVE_DIR / ".mode"
POLL_INTERVAL = 0.5
DEFAULT_PATH_MODE = "windows"
PATH_MODES = ("windows", "wsl", "forward", "r2")
PATH_MODE_LABELS = {
    "windows": "Windows (C:\\...)",
    "wsl": "WSL (/mnt/c/...)",
    "forward": "Forward slash (C:/...)",
    "r2": "R2 업로드 (퍼블릭 URL)",
}


def load_path_mode() -> str:
    try:
        value = MODE_FILE.read_text(encoding="utf-8").strip()
        if value in PATH_MODES:
            return value
    except OSError:
        pass
    return DEFAULT_PATH_MODE


def save_path_mode(mode: str) -> None:
    try:
        MODE_FILE.write_text(mode, encoding="utf-8")
    except OSError:
        pass


# ===== Logger =====
_COLORS = {"INFO": "\033[37m", "OK": "\033[32m", "WARN": "\033[33m", "ERROR": "\033[31m"}
_RESET = "\033[0m"


class Logger:
    def __init__(self, log_file: Path):
        self.log_file = log_file
        self._lock = threading.Lock()
        self._subscribers = []

    def subscribe(self, fn) -> None:
        self._subscribers.append(fn)

    def log(self, msg: str, level: str = "INFO") -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{level}] {msg}"
        with self._lock:
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
            if sys.stdout is not None:
                color = _COLORS.get(level, "")
                try:
                    print(f"{color}{line}{_RESET}" if color else line, flush=True)
                except (OSError, ValueError):
                    pass
        for sub in self._subscribers:
            try:
                sub(line, level)
            except Exception:
                pass


# ===== R2 Uploader =====
class R2Uploader:
    def __init__(self, logger: Logger):
        self.logger = logger
        self.access_key = os.environ.get("R2_ACCESS_KEY")
        self.secret_key = os.environ.get("R2_SECRET_KEY")
        self.endpoint = os.environ.get("R2_ENDPOINT")
        self.bucket = os.environ.get("R2_BUCKET")
        self.public_url = (os.environ.get("R2_PUBLIC_URL") or "").rstrip("/")
        self.client = None
        self.available = False
        self._init_client()

    def _init_client(self) -> None:
        if not _BOTO_AVAILABLE:
            self.logger.log("boto3 미설치 — R2 업로드 비활성", "WARN")
            return
        missing = [k for k, v in {
            "R2_ACCESS_KEY": self.access_key,
            "R2_SECRET_KEY": self.secret_key,
            "R2_ENDPOINT": self.endpoint,
            "R2_BUCKET": self.bucket,
            "R2_PUBLIC_URL": self.public_url,
        }.items() if not v]
        if missing:
            self.logger.log(f"R2 환경변수 누락: {', '.join(missing)}", "WARN")
            return
        try:
            self.client = boto3.client(
                "s3",
                endpoint_url=self.endpoint,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name="auto",
            )
            self.available = True
            self.logger.log(f"R2 준비됨 (bucket={self.bucket})", "OK")
        except Exception as e:
            self.logger.log(f"R2 클라이언트 초기화 실패: {e}", "ERROR")

    def upload(self, key: str, data: bytes) -> str | None:
        if not self.available:
            return None
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType="image/png",
            )
            return f"{self.public_url}/{key}"
        except Exception as e:
            self.logger.log(f"R2 업로드 에러: {e}", "ERROR")
            return None


# ===== Clipboard Watcher =====
class ClipWatcher:
    def __init__(self, logger: Logger, uploader: R2Uploader, save_dir: Path, initial_mode: str):
        self.logger = logger
        self.uploader = uploader
        self.save_dir = save_dir
        self.enabled = True
        self.path_mode = initial_mode
        self.last_hash = ""
        self.saved_count = 0
        self.on_save = None  # callable(filename, clip_value)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            if self.enabled:
                try:
                    self._tick()
                except Exception as e:
                    self.logger.log(f"에러: {e}", "ERROR")
            self._stop.wait(POLL_INTERVAL)

    def _tick(self) -> None:
        try:
            img = ImageGrab.grabclipboard()
        except Exception:
            return
        if not isinstance(img, Image.Image):
            return

        buf = BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        h = hashlib.sha1(data).hexdigest()
        if h == self.last_hash:
            return
        self.last_hash = h

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"clip_{ts}.png"
        full_path = self.save_dir / filename
        try:
            with open(full_path, "wb") as f:
                f.write(data)
        except OSError as e:
            self.logger.log(f"파일 저장 실패: {e}", "ERROR")
            return

        clip_value = self._resolve_clip_value(str(full_path), filename, data)

        if pyperclip is not None:
            try:
                pyperclip.copy(clip_value)
            except Exception as e:
                self.logger.log(f"클립보드 쓰기 실패: {e}", "WARN")

        self.saved_count += 1
        self.logger.log(
            f"저장됨 [{self.saved_count}] ({self.path_mode}): {filename} -> {clip_value}",
            "OK",
        )

        if self.on_save:
            try:
                self.on_save(filename, clip_value)
            except Exception:
                pass

    def _resolve_clip_value(self, win_path: str, filename: str, data: bytes) -> str:
        mode = self.path_mode
        if mode == "wsl":
            drive = win_path[0].lower()
            return f"/mnt/{drive}" + win_path[2:].replace("\\", "/")
        if mode == "forward":
            return win_path.replace("\\", "/")
        if mode == "r2":
            if self.uploader.available:
                url = self.uploader.upload(filename, data)
                if url:
                    return url
                self.logger.log("R2 업로드 실패 — 로컬 경로 fallback", "WARN")
            else:
                self.logger.log("R2 설정 없음 — 로컬 경로 fallback", "ERROR")
            return win_path
        return win_path


# ===== Tray =====
def _make_icon_image() -> Image.Image:
    img = Image.new("RGB", (64, 64), (30, 30, 30))
    draw = ImageDraw.Draw(img)
    draw.rectangle((6, 6, 57, 57), outline=(0, 200, 120), width=3)
    draw.rectangle((18, 22, 46, 46), fill=(0, 200, 120))
    return img


class MainWindow:
    """Main UI window (CustomTkinter). Hides to tray on close."""

    def __init__(self, watcher: ClipWatcher, logger: Logger):
        self.watcher = watcher
        self.logger = logger
        self.on_exit = lambda: None  # set by main()
        self.on_state_change = lambda: None  # set by main() to refresh tray menu

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("green")

        self.root = ctk.CTk()
        self.root.title("Clip Watcher")
        self.root.geometry("440x540")
        self.root.minsize(380, 460)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)

        self.status_label = ctk.CTkLabel(
            self.root, text="● ON", font=("Segoe UI", 28, "bold"), text_color="#4ade80"
        )
        self.status_label.pack(pady=(20, 6))

        self.toggle_btn = ctk.CTkButton(
            self.root, text="일시정지", command=self._toggle, width=220, height=42,
            font=("Segoe UI", 13, "bold"),
        )
        self.toggle_btn.pack(pady=(0, 18))

        ctk.CTkLabel(self.root, text="경로 모드", anchor="w").pack(fill="x", padx=24)
        self.mode_var = ctk.StringVar(value=watcher.path_mode)
        self.mode_menu = ctk.CTkOptionMenu(
            self.root,
            values=self._available_modes(),
            variable=self.mode_var,
            command=self._on_mode_change,
            width=220,
        )
        self.mode_menu.pack(pady=(2, 16), padx=24, anchor="w")

        self.stats_label = ctk.CTkLabel(self.root, text="저장됨: 0개", anchor="w")
        self.stats_label.pack(fill="x", padx=24)
        self.last_label = ctk.CTkLabel(
            self.root, text="마지막: -", anchor="w", wraplength=380, justify="left"
        )
        self.last_label.pack(fill="x", padx=24, pady=(0, 14))

        ctk.CTkLabel(self.root, text="최근 로그", anchor="w").pack(fill="x", padx=24)
        self.log_box = ctk.CTkTextbox(
            self.root, height=160, font=("Consolas", 10), wrap="none"
        )
        self.log_box.pack(fill="both", expand=True, padx=24, pady=(4, 12))
        self.log_box.configure(state="disabled")

        btn_frame = ctk.CTkFrame(self.root, fg_color="transparent")
        btn_frame.pack(fill="x", padx=24, pady=(0, 16))
        ctk.CTkButton(btn_frame, text="폴더 열기", command=self._open_folder, width=90).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btn_frame, text="로그 열기", command=self._open_log, width=90).pack(side="left", padx=6)
        ctk.CTkButton(
            btn_frame, text="종료", command=self._exit, width=80,
            fg_color="#a33", hover_color="#822",
        ).pack(side="right")

    def _available_modes(self):
        return [m for m in PATH_MODES if m != "r2" or self.watcher.uploader.available]

    def _toggle(self):
        self.watcher.enabled = not self.watcher.enabled
        if self.watcher.enabled:
            self.logger.log("감시 재개", "OK")
        else:
            self.logger.log("감시 일시정지", "WARN")
        self.refresh_state()
        self.on_state_change()

    def _on_mode_change(self, value: str):
        if value == self.watcher.path_mode:
            return
        self.watcher.path_mode = value
        save_path_mode(value)
        self.logger.log(f"경로 모드 변경: {value} (저장됨)", "OK")
        self.on_state_change()

    def _open_folder(self):
        try:
            os.startfile(str(SAVE_DIR))
        except OSError as e:
            self.logger.log(f"폴더 열기 실패: {e}", "ERROR")

    def _open_log(self):
        try:
            os.startfile(str(LOG_FILE))
        except OSError as e:
            self.logger.log(f"로그 열기 실패: {e}", "ERROR")

    def _exit(self):
        self.on_exit()

    def show(self):
        self.root.after(0, self._do_show)

    def _do_show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self):
        self.root.withdraw()

    def refresh_state(self):
        self.root.after(0, self._do_refresh_state)

    def _do_refresh_state(self):
        on = self.watcher.enabled
        self.status_label.configure(
            text=f"{'●' if on else '○'} {'ON' if on else 'OFF'}",
            text_color="#4ade80" if on else "#f87171",
        )
        self.toggle_btn.configure(text="일시정지" if on else "재개")
        self.stats_label.configure(text=f"저장됨: {self.watcher.saved_count}개")
        if self.mode_var.get() != self.watcher.path_mode:
            self.mode_var.set(self.watcher.path_mode)

    def update_last_save(self, filename: str, clip_value: str):
        def _do():
            self.last_label.configure(text=f"마지막: {filename}\n→ {clip_value}")
            self._do_refresh_state()
        self.root.after(0, _do)

    def append_log(self, line: str, level: str):
        def _do():
            try:
                self.log_box.configure(state="normal")
                self.log_box.insert("end", line + "\n")
                lines = int(self.log_box.index("end-1c").split(".")[0])
                if lines > 200:
                    self.log_box.delete("1.0", f"{lines - 200}.0")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
            except Exception:
                pass
        try:
            self.root.after(0, _do)
        except Exception:
            pass

    def destroy(self):
        try:
            self.root.after(0, self.root.destroy)
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


class TrayApp:
    def __init__(self, watcher: ClipWatcher, logger: Logger, window: MainWindow):
        self.watcher = watcher
        self.logger = logger
        self.window = window
        self.on_exit = lambda: None  # set by main()
        self.icon = pystray.Icon(
            "clip_watcher",
            _make_icon_image(),
            "Clip Watcher (ON)",
            menu=self._build_menu(),
        )

    def _build_menu(self) -> Menu:
        available_modes = [
            m for m in PATH_MODES
            if m != "r2" or self.watcher.uploader.available
        ]
        path_items = [
            MenuItem(
                PATH_MODE_LABELS[mode],
                self._make_set_mode(mode),
                checked=lambda item, m=mode: self.watcher.path_mode == m,
                radio=True,
            )
            for mode in available_modes
        ]

        return Menu(
            MenuItem("창 열기", self._show_window, default=True),
            MenuItem(
                lambda item: "재개" if not self.watcher.enabled else "일시정지",
                self._toggle,
            ),
            MenuItem("저장 폴더 열기", self._open_folder),
            MenuItem(
                lambda item: f"경로 모드: {self.watcher.path_mode}",
                Menu(*path_items),
            ),
            MenuItem("로그 파일 열기", self._open_log),
            Menu.SEPARATOR,
            MenuItem("종료", self._exit),
        )

    def _show_window(self, icon, item) -> None:
        self.window.show()

    def _toggle(self, icon, item) -> None:
        self.watcher.enabled = not self.watcher.enabled
        state = "ON" if self.watcher.enabled else "OFF"
        self.icon.title = f"Clip Watcher ({state})"
        if self.watcher.enabled:
            self.logger.log("감시 재개", "OK")
        else:
            self.logger.log("감시 일시정지", "WARN")
        self.icon.update_menu()
        self.window.refresh_state()

    def refresh(self) -> None:
        """Called by window when it changes state — keep tray in sync."""
        state = "ON" if self.watcher.enabled else "OFF"
        try:
            self.icon.title = f"Clip Watcher ({state})"
            self.icon.update_menu()
        except Exception:
            pass

    def _make_set_mode(self, mode: str):
        def _set(icon, item):
            self.watcher.path_mode = mode
            save_path_mode(mode)
            self.logger.log(f"경로 모드 변경: {mode} (저장됨)", "OK")
            self.icon.update_menu()
            self.window.refresh_state()
        return _set

    def _open_folder(self, icon, item) -> None:
        try:
            os.startfile(str(SAVE_DIR))
        except OSError as e:
            self.logger.log(f"폴더 열기 실패: {e}", "ERROR")

    def _open_log(self, icon, item) -> None:
        try:
            os.startfile(str(LOG_FILE))
        except OSError as e:
            self.logger.log(f"로그 열기 실패: {e}", "ERROR")

    def _exit(self, icon, item) -> None:
        self.on_exit()

    def notify_save(self, filename: str, clip_value: str) -> None:
        try:
            self.icon.notify(f"{filename}\n{clip_value}", "스샷 저장됨")
        except Exception:
            pass

    def run(self) -> None:
        self.icon.run()


# ===== Main =====
def _enable_ansi_on_windows() -> None:
    if sys.platform != "win32" or sys.stdout is None:
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        kernel32.SetConsoleMode(handle, 7)
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


_CTRL_HANDLER_REF = None  # keep callback alive


def _install_console_ctrl_handler(on_signal) -> None:
    """Catch Ctrl+C / Ctrl+Break / console close on Windows and trigger shutdown.

    pystray's message pump blocks the Python interpreter so signal handlers
    never fire; SetConsoleCtrlHandler runs the callback in its own thread.
    """
    if sys.platform != "win32":
        import signal
        try:
            signal.signal(signal.SIGINT, lambda s, f: on_signal())
            signal.signal(signal.SIGTERM, lambda s, f: on_signal())
        except (ValueError, OSError):
            pass
        return

    import ctypes
    HANDLER_TYPE = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

    def _handler(ctrl_type):
        # CTRL_C=0, CTRL_BREAK=1, CTRL_CLOSE=2, CTRL_LOGOFF=5, CTRL_SHUTDOWN=6
        if ctrl_type in (0, 1, 2, 5, 6):
            on_signal()
            return 1
        return 0

    global _CTRL_HANDLER_REF
    _CTRL_HANDLER_REF = HANDLER_TYPE(_handler)
    try:
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_CTRL_HANDLER_REF, True)
    except Exception:
        pass


def main() -> None:
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    _enable_ansi_on_windows()

    logger = Logger(LOG_FILE)
    initial_mode = load_path_mode()
    logger.log(f"Clip Watcher 시작 (경로 모드: {initial_mode})", "OK")
    logger.log(f"저장 경로: {SAVE_DIR}")
    logger.log(f"로그 파일: {LOG_FILE}")

    if pyperclip is None:
        logger.log("pyperclip 미설치 — 클립보드 텍스트 복사 비활성", "WARN")

    uploader = R2Uploader(logger)
    if initial_mode == "r2" and not uploader.available:
        logger.log(f"저장된 모드 'r2' 사용 불가 — '{DEFAULT_PATH_MODE}'로 fallback", "WARN")
        initial_mode = DEFAULT_PATH_MODE

    watcher = ClipWatcher(logger, uploader, SAVE_DIR, initial_mode)
    window = MainWindow(watcher, logger)
    app = TrayApp(watcher, logger, window)

    logger.subscribe(window.append_log)

    def _on_save(filename, clip_value):
        app.notify_save(filename, clip_value)
        window.update_last_save(filename, clip_value)
    watcher.on_save = _on_save

    window.on_state_change = app.refresh

    shutdown_done = threading.Event()

    def _shutdown():
        if shutdown_done.is_set():
            return
        shutdown_done.set()
        logger.log(f"종료 (총 {watcher.saved_count} 개 저장됨)", "OK")
        watcher.stop()
        try:
            app.icon.stop()
        except Exception:
            pass
        window.destroy()

    window.on_exit = _shutdown
    app.on_exit = _shutdown
    _install_console_ctrl_handler(_shutdown)

    threading.Thread(target=watcher.run, daemon=True).start()
    threading.Thread(target=app.run, daemon=True).start()

    window.run()


if __name__ == "__main__":
    main()
