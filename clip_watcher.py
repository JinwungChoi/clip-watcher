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


class TrayApp:
    def __init__(self, watcher: ClipWatcher, logger: Logger):
        self.watcher = watcher
        self.logger = logger
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
            MenuItem(
                lambda item: "재개" if not self.watcher.enabled else "일시정지",
                self._toggle,
                default=True,
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

    def _toggle(self, icon, item) -> None:
        self.watcher.enabled = not self.watcher.enabled
        state = "ON" if self.watcher.enabled else "OFF"
        self.icon.title = f"Clip Watcher ({state})"
        if self.watcher.enabled:
            self.logger.log("감시 재개", "OK")
        else:
            self.logger.log("감시 일시정지", "WARN")
        self.icon.update_menu()

    def _make_set_mode(self, mode: str):
        def _set(icon, item):
            self.watcher.path_mode = mode
            save_path_mode(mode)
            self.logger.log(f"경로 모드 변경: {mode} (저장됨)", "OK")
            self.icon.update_menu()
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
        self.logger.log(f"종료 (총 {self.watcher.saved_count} 개 저장됨)", "OK")
        self.watcher.stop()
        self.icon.stop()

    def on_save(self, filename: str, clip_value: str) -> None:
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
    app = TrayApp(watcher, logger)
    watcher.on_save = app.on_save

    t = threading.Thread(target=watcher.run, daemon=True)
    t.start()

    def _shutdown():
        logger.log(f"종료 신호 수신 (총 {watcher.saved_count} 개 저장됨)", "WARN")
        watcher.stop()
        try:
            app.icon.stop()
        except Exception:
            pass

    _install_console_ctrl_handler(_shutdown)

    app.run()


if __name__ == "__main__":
    main()
