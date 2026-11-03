"""
LiteClip - a lightweight instant-replay clipper for NVIDIA GPUs.

Screen capture (Desktop Duplication) and encoding (NVENC) both run on the GPU,
so it uses very little CPU. Runs in the system tray, saves clips on a global hotkey.

Run from source:  pip install customtkinter pillow pystray   then double-click this file.
Build an .exe:    double-click build_exe.bat
"""

import ctypes
import datetime
import glob
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
import tkinter as tk
from tkinter import filedialog, messagebox

APP_NAME = "LiteClip"
APP_VERSION = "1.0.0"

try:
    import customtkinter as ctk
    from PIL import Image, ImageDraw
    import pystray
except ImportError:
    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror(APP_NAME, "Some parts are missing.\n\nOpen Command Prompt and run:\n\n"
                                   "pip install customtkinter pillow pystray")
    sys.exit(1)

# =================================================================== paths
APP_HOME = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
    else os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
THUMB_DIR = os.path.join(APP_DIR, "thumbs")
ICON_FILE = os.path.join(APP_DIR, "liteclip.ico")
BUF_DIR = os.path.join(tempfile.gettempdir(), "liteclip_buffer")
LOG_FILE = os.path.join(tempfile.gettempdir(), "liteclip_ffmpeg.log")
SEGMENT_LIST = os.path.join(BUF_DIR, "segments.csv")
PID_FILE = os.path.join(tempfile.gettempdir(), "liteclip_recorder.pid")

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOW_PRIORITY = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
SEGMENT_SECONDS = 2

# =================================================================== look
BG = "#0e1014"
SIDEBAR = "#13161b"
CARD = "#181b21"
CARD_2 = "#21252d"
BORDER = "#2b3039"
TEXT = "#eef0f4"
MUTED = "#8b92a0"
ACCENT = "#7c5cff"
ACCENT_H = "#9176ff"
RED = "#ef4444"
RED_H = "#f36464"
RED_BG = "#2a1517"
GREEN = "#22c55e"
AMBER = "#f5a524"
FONT = "Segoe UI"

# =================================================================== options
CLIP_LENGTHS = [15, 30, 45, 60, 90, 120]
FPS_OPTIONS = ["30", "60", "120", "144"]
QUALITY = {"Low": "10M", "Medium": "20M", "High": "35M", "Ultra": "50M"}
CODECS = {"H.264": "h264_nvenc", "HEVC": "hevc_nvenc"}

# Windows hotkey constants
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
WM_HOTKEY, WM_APP_SET, WM_APP_QUIT = 0x0312, 0x8001, 0x8002


def real_videos_dir():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            p = os.path.expandvars(winreg.QueryValueEx(k, "My Video")[0])
            if p:
                return p
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Videos")


OLD_DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "Videos", "LiteClips")
DEFAULTS = {
    "clip_seconds": 30,
    "fps": 60,
    "bitrate": "20M",
    "codec": "H.264",
    "monitor": 0,
    "audio_device": "None",
    "hotkey_mods": MOD_ALT,
    "hotkey_vk": 0x77,  # Alt + F8 (Alt+F10 clashes with NVIDIA's overlay)
    "clips_dir": os.path.join(real_videos_dir(), "LiteClips"),
    "ffmpeg": "ffmpeg",
    "autostart_recording": False,
    "run_at_startup": False,
    "tray_on_close": True,
}


# =================================================================== helpers
def parse_legacy_hotkey(text):
    mods, vk = 0, 0
    for part in str(text).lower().replace(" ", "").split("+"):
        if part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        elif re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", part):
            vk = 0x6F + int(part[1:])
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
    return (mods, vk) if vk else (DEFAULTS["hotkey_mods"], DEFAULTS["hotkey_vk"])


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("hotkey"), str):  # settings from the older version
            data["hotkey_mods"], data["hotkey_vk"] = parse_legacy_hotkey(data.pop("hotkey"))
        if "autostart" in data:
            data["autostart_recording"] = data.pop("autostart")
        s.update({k: v for k, v in data.items() if k in DEFAULTS})
    except Exception:
        pass
    if os.path.normcase(str(s["clips_dir"])) == os.path.normcase(OLD_DEFAULT_DIR):
        s["clips_dir"] = DEFAULTS["clips_dir"]
    return s


def save_settings(s):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


class KillOnCloseJob:
    """A Windows 'job object': every FFmpeg LiteClip starts is put in it, and Windows kills them all
    the moment LiteClip exits - even if it crashes or is ended in Task Manager. Without this, an
    orphaned recorder can keep running invisibly (using the GPU and locking files)."""

    def __init__(self):
        self.handle = None
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

            class BASIC(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class IO(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOps", "WriteOps", "OtherOps",
                                                           "ReadBytes", "WriteBytes", "OtherBytes")]

            class EXTENDED(ctypes.Structure):
                _fields_ = [("Basic", BASIC), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            handle = k32.CreateJobObjectW(None, None)
            info = EXTENDED()
            info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if handle and k32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                self.handle = handle
                self.k32 = k32
        except Exception:
            self.handle = None

    def add(self, proc):
        if self.handle:
            try:
                self.k32.AssignProcessToJobObject(self.handle, int(proc._handle))
            except Exception:
                pass


JOB = KillOnCloseJob()


def kill_stale_recorder():
    """If an older LiteClip left its recorder running, stop it (only if that PID really is ffmpeg.exe)."""
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return
    try:
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x0001 | 0x1000, False, pid)  # TERMINATE | QUERY_LIMITED_INFORMATION
        if h:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if k32.QueryFullProcessImageNameW(wintypes.HANDLE(h), 0, buf, ctypes.byref(size)):
                if os.path.basename(buf.value).lower() == "ffmpeg.exe":
                    k32.TerminateProcess(wintypes.HANDLE(h), 1)
                    time.sleep(0.5)
            k32.CloseHandle(wintypes.HANDLE(h))
    except Exception:
        pass
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def winget_ffmpeg_paths():
    local = os.environ.get("LOCALAPPDATA", "")
    paths = [os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe")]
    paths += glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "*", "bin", "ffmpeg.exe"))
    return paths


def resolve_ffmpeg(setting):
    """Find FFmpeg: a custom path, next to LiteClip, on PATH, or where winget installs it
    (so a fresh winget install works straight away, without restarting the PC)."""
    if setting and setting != "ffmpeg" and os.path.isfile(setting):
        return setting
    local = os.path.join(APP_HOME, "ffmpeg.exe")
    if os.path.isfile(local):
        return local
    found = shutil.which("ffmpeg")
    if found:
        return found
    for p in winget_ffmpeg_paths():
        if os.path.isfile(p):
            return p
    return "ffmpeg"


def friendly_error(log):
    """Turn FFmpeg's error text into something a normal person can act on."""
    l = (log or "").lower()
    if "nvenc" in l or "nvcuda" in l or "openencodesession" in l or "no capable devices" in l:
        return ("Your NVIDIA video encoder couldn't start. LiteClip needs an NVIDIA graphics card "
                "(GTX 900 series or newer) with up-to-date drivers. Too many apps recording at once "
                "(OBS, Discord streaming, the NVIDIA app) can also cause this.")
    if "ddagrab" in l and ("no such filter" in l or "not found" in l):
        return "Your FFmpeg is too old. Update it with:  winget upgrade Gyan.FFmpeg"
    if "output_idx" in l or "invalid output" in l or ("ddagrab" in l and "output" in l and "fail" in l):
        return "That monitor couldn't be found. Choose Monitor 1 in Settings."
    if "dshow" in l or "audio device" in l or "could not find audio" in l:
        return "The audio device couldn't be opened. Pick another one in Settings > Audio, or choose None."
    if "access" in l and "lost" in l or "duplicat" in l:
        return ("Windows briefly blocked screen capture. This can happen during admin (UAC) prompts, "
                "the lock screen, or when a game changes resolution.")
    return None


FFMPEG = "ffmpeg"


def ffmpeg_available():
    return shutil.which(FFMPEG) is not None or os.path.isfile(FFMPEG)


def run_once(args, timeout=60):
    p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, errors="replace", creationflags=NO_WINDOW | LOW_PRIORITY)
    JOB.add(p)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise
    return subprocess.CompletedProcess(args, p.returncode, out, err)


def send_to_recycle_bin(path):
    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", ctypes.c_uint), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
    op = SHFILEOPSTRUCTW()
    op.wFunc = 3  # FO_DELETE
    op.pFrom = os.path.abspath(path) + "\0"  # list must end with a double null
    op.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400  # ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not op.fAnyOperationsAborted


def list_audio_devices():
    try:
        r = run_once([FFMPEG, "-hide_banner", "-nostdin", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                     timeout=15)
    except Exception:
        return []
    return re.findall(r'"([^"]+)"\s+\(audio\)', r.stderr)


def wrap_count(clip_seconds):
    """How many chunk files the rolling buffer cycles through."""
    return math.ceil(int(clip_seconds) / SEGMENT_SECONDS) + 3


def buffer_has_video():
    return any(os.path.getsize(f) > 0 for f in glob.glob(os.path.join(BUF_DIR, "buf*.ts")))


def clean_dir(raw):
    p = (raw or "").strip().strip('"').strip("'").strip()
    if not p:
        return DEFAULTS["clips_dir"]
    p = os.path.expandvars(os.path.expanduser(p))
    if not os.path.isabs(p):
        p = os.path.join(real_videos_dir(), p)
    return os.path.normpath(p)


def check_writable(folder):
    try:
        os.makedirs(folder, exist_ok=True)
        test = os.path.join(folder, ".liteclip_write_test")
        with open(test, "wb") as f:
            f.write(b"ok")
        os.remove(test)
        return None
    except PermissionError:
        return ("Windows blocked LiteClip from saving there. If 'Controlled folder access' is on in "
                "Windows Security, allow LiteClip and ffmpeg.exe, or choose another folder.")
    except OSError as e:
        return f"That folder can't be used: {e.strerror or e}"


def beep(freq=880, ms=110):
    try:
        import winsound
        winsound.Beep(freq, ms)
    except Exception:
        pass


def nearest(value, options):
    return min(options, key=lambda o: abs(o - value))


def bitrate_to_quality(bitrate):
    try:
        n = float(str(bitrate).rstrip("Mm"))
    except ValueError:
        return "Medium"
    return min(QUALITY, key=lambda q: abs(float(QUALITY[q].rstrip("M")) - n))


def safe_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def friendly_time(ts):
    d = datetime.datetime.fromtimestamp(ts)
    today = datetime.date.today()
    if d.date() == today:
        day = "Today"
    elif d.date() == today - datetime.timedelta(days=1):
        day = "Yesterday"
    else:
        day = f"{d.day} {d.strftime('%b')}"
    return f"{day}, {d.strftime('%H:%M')}"


# ---------- hotkey naming
VK_NAMES = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x13: "Pause", 0x20: "Space", 0x21: "Page Up",
    0x22: "Page Down", 0x23: "End", 0x24: "Home", 0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
    0x2C: "Print Screen", 0x2D: "Insert", 0x2E: "Delete", 0x6A: "Num *", 0x6B: "Num +", 0x6D: "Num -",
    0x6E: "Num .", 0x6F: "Num /", 0x91: "Scroll Lock", 0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-",
    0xBE: ".", 0xBF: "/", 0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
}
MOD_KEYSYMS = {"Control_L": MOD_CONTROL, "Control_R": MOD_CONTROL, "Alt_L": MOD_ALT, "Alt_R": MOD_ALT,
               "Shift_L": MOD_SHIFT, "Shift_R": MOD_SHIFT}
IGNORED_KEYSYMS = {"Caps_Lock", "Num_Lock", "Win_L", "Win_R", "App", "Super_L", "Super_R"}


def vk_name(vk):
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    if 0x60 <= vk <= 0x69:
        return f"Num {vk - 0x60}"
    return VK_NAMES.get(vk, f"Key {vk}")


def hotkey_label(mods, vk):
    parts = []
    if mods & MOD_CONTROL:
        parts.append("Ctrl")
    if mods & MOD_ALT:
        parts.append("Alt")
    if mods & MOD_SHIFT:
        parts.append("Shift")
    parts.append(vk_name(vk))
    return " + ".join(parts)


def validate_hotkey(mods, vk):
    if mods == 0 and (0x30 <= vk <= 0x5A or vk in (0x20, 0x0D, 0x08, 0x09)):
        return "Add Ctrl, Alt or Shift to that key so it doesn't fire while you're typing."
    if mods == MOD_ALT and vk in (0x73, 0x09):
        return "That shortcut belongs to Windows. Pick a different one."
    return None


# ---------- icon
def make_icon_image(size=64, recording=False):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = size * 0.04
    d.rounded_rectangle([pad, pad, size - pad, size - pad], radius=size * 0.26, fill=(124, 92, 255, 255))
    c, r = size / 2, size * 0.2
    ring = size * 0.31
    d.ellipse([c - ring, c - ring, c + ring, c + ring], outline=(255, 255, 255, 235), width=max(2, int(size * 0.06)))
    d.ellipse([c - r, c - r, c + r, c + r], fill=(239, 68, 68, 255) if recording else (255, 255, 255, 255))
    return img


def write_ico(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    make_icon_image(256).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


# ---------- start with Windows
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.isfile(pyw):
        exe = pyw
    return f'"{exe}" "{os.path.abspath(__file__)}" --minimized'


def is_run_at_startup():
    """True if Windows is set to start LiteClip at login (by the app or by the installer)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
        return True
    except OSError:
        return False


def set_run_at_startup(enabled):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, launch_command())
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


# =================================================================== global hotkey (native Windows)
class GlobalHotkey(threading.Thread):
    """Uses RegisterHotKey, so it works system-wide (including fullscreen games) with no keyboard hook."""

    def __init__(self, on_press):
        super().__init__(daemon=True)
        self.on_press = on_press
        self.thread_id = None
        self.ready = threading.Event()
        self.request = (0, 0)
        self.reply = queue.Queue()

    def run(self):
        user32 = ctypes.windll.user32
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # create this thread's message queue
        self.ready.set()
        registered = False
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.on_press()
            elif msg.message == WM_APP_SET:
                if registered:
                    user32.UnregisterHotKey(None, 1)
                    registered = False
                mods, vk = self.request
                ok = True
                if vk:
                    ok = bool(user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk))
                    registered = ok
                self.reply.put(ok)
            elif msg.message == WM_APP_QUIT:
                break
        if registered:
            user32.UnregisterHotKey(None, 1)

    def set(self, mods, vk):
        """Register a new hotkey (vk=0 just unregisters). Returns False if another app owns it."""
        if not self.ready.wait(3):
            return False
        while not self.reply.empty():
            self.reply.get_nowait()
        self.request = (mods, vk)
        ctypes.windll.user32.PostThreadMessageW(self.thread_id, WM_APP_SET, 0, 0)
        try:
            return self.reply.get(timeout=3)
        except queue.Empty:
            return False

    def stop(self):
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, WM_APP_QUIT, 0, 0)


# =================================================================== recorder
class Recorder:
    def __init__(self):
        self.proc = None
        self.log = None

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, s):
        kill_stale_recorder()
        shutil.rmtree(BUF_DIR, ignore_errors=True)
        os.makedirs(BUF_DIR, exist_ok=True)
        fps = int(s["fps"])
        audio = s["audio_device"] not in ("", "None")

        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i", f"ddagrab=output_idx={int(s['monitor'])}:framerate={fps}"]
        if audio:
            cmd += ["-f", "dshow", "-audio_buffer_size", "80", "-i", f"audio={s['audio_device']}"]
        cmd += ["-map", "0:v"]
        if audio:
            cmd += ["-map", "1:a", "-af", "aresample=async=1000", "-c:a", "aac", "-b:a", "160k"]
        cmd += ["-c:v", CODECS.get(s["codec"], "h264_nvenc"), "-preset", "p4", "-rc", "cbr",
                "-b:v", s["bitrate"], "-g", str(fps * SEGMENT_SECONDS),
                "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_SECONDS})"]
        wrap = wrap_count(s["clip_seconds"])
        cmd += ["-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-segment_wrap", str(wrap),
                "-segment_format", "mpegts", "-segment_list", SEGMENT_LIST, "-segment_list_type", "csv",
                os.path.join(BUF_DIR, "buf%03d.ts")]

        self.log = open(LOG_FILE, "w")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=self.log, stderr=self.log,
                                     creationflags=NO_WINDOW | LOW_PRIORITY)
        JOB.add(self.proc)
        try:
            with open(PID_FILE, "w") as f:
                f.write(str(self.proc.pid))
        except OSError:
            pass

    def stop(self):
        if self.running():
            try:
                self.proc.stdin.write(b"q")
                self.proc.stdin.flush()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
        self.proc = None
        if self.log:
            self.log.close()
            self.log = None
        try:
            os.remove(PID_FILE)
        except OSError:
            pass

    @staticmethod
    def last_error():
        try:
            with open(LOG_FILE, "r", errors="replace") as f:
                return f.read()[-1500:].strip()
        except Exception:
            return ""


def finished_chunks():
    """Finished buffer chunks in recording order, as listed by FFmpeg itself."""
    try:
        with open(SEGMENT_LIST, "r", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    names = []
    for line in lines:
        parts = line.strip().split(",")
        if len(parts) < 3:
            continue  # a line FFmpeg is still writing
        try:
            float(parts[-2]), float(parts[-1])
        except ValueError:
            continue
        names.append(os.path.basename(",".join(parts[:-2]).strip('"')))
    return names


def pick_chunks(clip_seconds):
    done = finished_chunks()
    need = math.ceil(int(clip_seconds) / SEGMENT_SECONDS)
    chosen = done[-need:]
    # The chunk being written right now holds the moments just before you pressed the hotkey.
    if done:
        m = re.match(r"buf(\d+)\.ts$", done[-1])
        if m:
            chosen.append(f"buf{(int(m.group(1)) + 1) % wrap_count(clip_seconds):03d}.ts")
    else:
        chosen = ["buf000.ts"]
    return [os.path.join(BUF_DIR, n) for n in chosen if os.path.isfile(os.path.join(BUF_DIR, n))]


def drop_unfinished_frame(data):
    """The newest chunk is still being written, so its last video frame may be incomplete.
    Cut the data just before that frame starts."""
    data = data[: len(data) - len(data) % 188]
    count = len(data) // 188
    pid_counts = {}
    for i in range(count):
        b = data[i * 188: i * 188 + 3]
        if b[0] == 0x47:
            pid = ((b[1] & 0x1F) << 8) | b[2]
            pid_counts[pid] = pid_counts.get(pid, 0) + 1
    if not pid_counts:
        return data
    video_pid = max(pid_counts, key=pid_counts.get)  # video makes up most of the stream
    for i in range(count - 1, -1, -1):
        b = data[i * 188: i * 188 + 3]
        if b[0] == 0x47 and (b[1] & 0x40) and (((b[1] & 0x1F) << 8) | b[2]) == video_pid:
            return data[: i * 188]
    return data


def save_clip(s):
    segs = pick_chunks(s["clip_seconds"])
    if not segs:
        raise RuntimeError("Nothing has been recorded yet. Check Settings > Troubleshooting > View log.")

    os.makedirs(s["clips_dir"], exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_path = os.path.join(s["clips_dir"], f"clip_{stamp}.mp4")
    joined = os.path.join(tempfile.gettempdir(), f"liteclip_{stamp}.ts")
    with open(joined, "wb") as dst:
        for i, seg in enumerate(segs):
            with open(seg, "rb") as fh:
                data = fh.read()
            if i == len(segs) - 1:
                data = drop_unfinished_frame(data)
            else:
                data = data[: len(data) - len(data) % 188]
            dst.write(data)

    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", joined,
           "-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart"]
    if s["codec"] == "HEVC":
        cmd += ["-tag:v", "hvc1"]
    cmd.append(out_path)
    try:
        r = run_once(cmd, timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Saving took too long and was cancelled.")
    finally:
        try:
            os.remove(joined)
        except OSError:
            pass
    if r.returncode != 0 or not os.path.isfile(out_path):
        err = r.stderr.strip()[-300:]
        if "Permission denied" in err:
            raise RuntimeError("Windows blocked FFmpeg from writing to that folder (Controlled folder access?).")
        raise RuntimeError(err or "FFmpeg couldn't create the MP4.")
    return out_path


# =================================================================== thumbnails
def thumb_path_for(clip):
    base = re.sub(r"[^\w\-]", "_", os.path.splitext(os.path.basename(clip))[0])
    return os.path.join(THUMB_DIR, f"{base}_{int(safe_mtime(clip))}.jpg")


def prune_thumbs(max_age_days=45):
    cutoff = time.time() - max_age_days * 86400
    for t in glob.glob(os.path.join(THUMB_DIR, "*.jpg")):
        if safe_mtime(t) < cutoff:
            try:
                os.remove(t)
            except OSError:
                pass


class ThumbWorker(threading.Thread):
    def __init__(self, out_q):
        super().__init__(daemon=True)
        self.tasks = queue.Queue()
        self.out = out_q
        self.pending = set()

    def request(self, clip):
        thumb = thumb_path_for(clip)
        if os.path.isfile(thumb):
            return thumb
        if clip not in self.pending:
            self.pending.add(clip)
            self.tasks.put(clip)
        return None

    def run(self):
        while True:
            clip = self.tasks.get()
            thumb = thumb_path_for(clip)
            try:
                os.makedirs(THUMB_DIR, exist_ok=True)
                run_once([FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-ss", "1",
                          "-i", clip, "-frames:v", "1", "-vf", "scale=224:-2", thumb], timeout=20)
                if os.path.isfile(thumb):
                    self.out.put(("thumb", (clip, thumb)))
            except Exception:
                pass
            self.pending.discard(clip)


# =================================================================== app
class App:
    def __init__(self, root, start_minimized=False):
        self.root = root
        self.q = queue.Queue()
        self.rec = Recorder()
        self.s = load_settings()
        self.s["run_at_startup"] = is_run_at_startup()
        self.recording = False
        self.saving = False
        self.rec_started = 0.0
        self.capturing = False
        self.cap_mods = set()
        self.thumb_labels = {}
        self.thumb_images = {}
        self.toast_job = None
        self.toast_link = None
        self.quitting = False
        self.tick = 0
        self.told_about_tray = False

        global FFMPEG
        FFMPEG = resolve_ffmpeg(self.s["ffmpeg"])

        self._fonts()
        root.title(APP_NAME)
        root.geometry("1000x660")
        root.minsize(900, 600)
        root.configure(fg_color=BG)
        root.grid_columnconfigure(1, weight=1)
        root.grid_rowconfigure(0, weight=1)
        try:
            write_ico(ICON_FILE)
            root.after(250, lambda: root.iconbitmap(ICON_FILE))
        except Exception:
            pass

        self._build_sidebar()
        self.main = ctk.CTkFrame(root, fg_color=BG, corner_radius=0)
        self.main.grid(row=0, column=1, sticky="nsew")
        self.main.grid_rowconfigure(0, weight=1)
        self.main.grid_columnconfigure(0, weight=1)
        self.pages = {"home": self._build_home(self.main), "settings": self._build_settings(self.main)}
        for p in self.pages.values():
            p.grid(row=0, column=0, sticky="nsew")
        self._build_toast()
        self.show_page("home")

        # background helpers
        self.hotkey = GlobalHotkey(lambda: self.q.put(("save", None)))
        self.hotkey.start()
        self.thumbs = ThumbWorker(self.q)
        self.thumbs.start()
        self._build_tray()
        threading.Thread(target=lambda: self.q.put(("audio", list_audio_devices())), daemon=True).start()

        if not self.hotkey.set(self.s["hotkey_mods"], self.s["hotkey_vk"]):
            self.root.after(800, lambda: self.notify(
                f"{self.hotkey_text()} is already used by another app (NVIDIA's overlay uses Alt+F10). "
                f"Set a new hotkey in Settings.", "error", ms=9000))
        if self.s["run_at_startup"]:
            set_run_at_startup(True)  # keeps the path correct if the app was moved

        self.refresh_hotkey_labels()
        self.update_stats()
        self.refresh_clips()
        self.set_recording(False)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Unmap>", self.on_unmap)
        root.after(100, self.poll)

        if start_minimized:
            root.withdraw()

        if not ffmpeg_available():
            root.after(700, self.offer_ffmpeg_install)
        elif self.s["autostart_recording"]:
            root.after(500, self.start)

    # ------------------------------------------------------------ fonts & styles
    def _fonts(self):
        f = lambda size, weight="normal": ctk.CTkFont(family=FONT, size=size, weight=weight)
        self.f_logo = f(20, "bold")
        self.f_nav = f(14)
        self.f_h1 = f(26, "bold")
        self.f_h2 = f(16, "bold")
        self.f_hero = f(26, "bold")
        self.f_body = f(13)
        self.f_small = f(12)
        self.f_caption = f(11, "bold")
        self.f_stat = f(18, "bold")
        self.f_btn = f(14, "bold")
        self.f_label = f(13, "bold")
        self.f_pill = f(11, "bold")
        self.seg_style = dict(font=self.f_small, height=32, corner_radius=8, fg_color=CARD_2,
                              selected_color=ACCENT, selected_hover_color=ACCENT_H,
                              unselected_color=CARD_2, unselected_hover_color=BORDER, text_color=TEXT)
        self.opt_style = dict(font=self.f_small, dropdown_font=self.f_small, height=32, corner_radius=8,
                              fg_color=CARD_2, button_color=CARD_2, button_hover_color=BORDER,
                              dropdown_fg_color=CARD_2, dropdown_hover_color=BORDER,
                              text_color=TEXT, dropdown_text_color=TEXT)
        self.ghost_btn = dict(fg_color=CARD_2, hover_color=BORDER, text_color=TEXT, border_width=1,
                              border_color=BORDER, corner_radius=8, height=32, font=self.f_small)

    # ------------------------------------------------------------ sidebar
    def _build_sidebar(self):
        sb = ctk.CTkFrame(self.root, width=220, corner_radius=0, fg_color=SIDEBAR)
        sb.grid(row=0, column=0, sticky="nsw")
        sb.grid_propagate(False)
        sb.pack_propagate(False)

        logo = ctk.CTkImage(make_icon_image(96), size=(32, 32))
        ctk.CTkLabel(sb, text="  LiteClip", image=logo, compound="left", font=self.f_logo,
                     text_color=TEXT).pack(anchor="w", padx=22, pady=(28, 30))

        self.nav = {}
        for key, label in (("home", "Home"), ("settings", "Settings")):
            b = ctk.CTkButton(sb, text=label, anchor="w", height=40, corner_radius=10, font=self.f_nav,
                              fg_color="transparent", hover_color=CARD_2, text_color=MUTED,
                              command=lambda k=key: self.show_page(k))
            b.pack(fill="x", padx=14, pady=3)
            self.nav[key] = b

        ctk.CTkButton(sb, text="Quit LiteClip", height=34, corner_radius=8, font=self.f_small,
                      fg_color="transparent", hover_color=CARD_2, text_color=MUTED,
                      command=self.quit).pack(side="bottom", fill="x", padx=14, pady=(0, 16))

        card = ctk.CTkFrame(sb, fg_color=CARD, corner_radius=12, border_width=1, border_color=BORDER)
        card.pack(side="bottom", fill="x", padx=14, pady=(0, 10))
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(anchor="w", padx=14, pady=(12, 0))
        self.side_dot = ctk.CTkLabel(row, text="●", font=self.f_small, text_color=MUTED)
        self.side_dot.pack(side="left")
        self.side_status = ctk.CTkLabel(row, text="  Idle", font=self.f_label, text_color=TEXT)
        self.side_status.pack(side="left")
        self.side_hotkey = ctk.CTkLabel(card, text="", font=self.f_small, text_color=MUTED)
        self.side_hotkey.pack(anchor="w", padx=14, pady=(0, 12))
        self.root.title(f"{APP_NAME} {APP_VERSION}")

    def show_page(self, key):
        self.pages[key].tkraise()
        for k, b in self.nav.items():
            b.configure(fg_color=CARD_2 if k == key else "transparent", text_color=TEXT if k == key else MUTED)
        if self.toast.winfo_ismapped():
            self.toast.lift()

    # ------------------------------------------------------------ home page
    def _build_home(self, parent):
        page = ctk.CTkFrame(parent, fg_color=BG, corner_radius=0)
        page.grid_columnconfigure((0, 1, 2), weight=1, uniform="stats")
        page.grid_rowconfigure(3, weight=1)

        head = ctk.CTkFrame(page, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=3, sticky="ew", padx=32, pady=(28, 0))
        ctk.CTkLabel(head, text="Home", font=self.f_h1, text_color=TEXT).pack(anchor="w")
        ctk.CTkLabel(head, text="Your instant replay, running quietly in the background.",
                     font=self.f_body, text_color=MUTED).pack(anchor="w")

        hero = ctk.CTkFrame(page, fg_color=CARD, corner_radius=16, border_width=1, border_color=BORDER)
        hero.grid(row=1, column=0, columnspan=3, sticky="ew", padx=32, pady=(20, 0))
        hero.grid_columnconfigure(0, weight=1)

        left = ctk.CTkFrame(hero, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w", padx=26, pady=24)
        self.pill = ctk.CTkLabel(left, text="", font=self.f_pill, corner_radius=12, height=26)
        self.pill.pack(anchor="w")
        self.hero_title = ctk.CTkLabel(left, text="", font=self.f_hero, text_color=TEXT)
        self.hero_title.pack(anchor="w", pady=(12, 2))
        self.hero_sub = ctk.CTkLabel(left, text="", font=self.f_body, text_color=MUTED,
                                     justify="left", wraplength=440)
        self.hero_sub.pack(anchor="w")
        self.buf_bar = ctk.CTkProgressBar(left, width=420, height=6, corner_radius=3,
                                          progress_color=ACCENT, fg_color=CARD_2)
        self.buf_bar.set(0)
        self.buf_bar.pack(anchor="w", pady=(16, 0))

        right = ctk.CTkFrame(hero, fg_color="transparent")
        right.grid(row=0, column=1, sticky="e", padx=26, pady=24)
        self.rec_btn = ctk.CTkButton(right, text="", width=200, height=48, corner_radius=12,
                                     font=self.f_btn, command=self.toggle)
        self.rec_btn.pack(pady=(0, 10))
        self.save_btn = ctk.CTkButton(right, text="Save clip", width=200, height=48, corner_radius=12,
                                      font=self.f_btn, fg_color=CARD_2, hover_color=BORDER, text_color=TEXT,
                                      text_color_disabled=MUTED, border_width=1, border_color=BORDER,
                                      command=self.save)
        self.save_btn.pack()

        self.stat_vals = {}
        for i, (key, label) in enumerate((("length", "Clip length"), ("video", "Video"), ("today", "Clips today"))):
            c = ctk.CTkFrame(page, fg_color=CARD, corner_radius=14, border_width=1, border_color=BORDER)
            c.grid(row=2, column=i, sticky="ew", pady=16,
                   padx=(32 if i == 0 else 8, 32 if i == 2 else 8))
            ctk.CTkLabel(c, text=label.upper(), font=self.f_caption, text_color=MUTED).pack(
                anchor="w", padx=18, pady=(14, 0))
            v = ctk.CTkLabel(c, text="-", font=self.f_stat, text_color=TEXT)
            v.pack(anchor="w", padx=18, pady=(2, 14))
            self.stat_vals[key] = v

        clips = ctk.CTkFrame(page, fg_color=CARD, corner_radius=16, border_width=1, border_color=BORDER)
        clips.grid(row=3, column=0, columnspan=3, sticky="nsew", padx=32, pady=(0, 28))
        clips.grid_columnconfigure(0, weight=1)
        clips.grid_rowconfigure(1, weight=1)
        top = ctk.CTkFrame(clips, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 8))
        ctk.CTkLabel(top, text="Recent clips", font=self.f_h2, text_color=TEXT).pack(side="left")
        ctk.CTkButton(top, text="Open folder", width=110, command=self.open_folder,
                      **self.ghost_btn).pack(side="right")
        ctk.CTkButton(top, text="Refresh", width=80, command=self.refresh_clips,
                      **self.ghost_btn).pack(side="right", padx=(0, 8))
        self.clip_list = ctk.CTkScrollableFrame(clips, fg_color="transparent", corner_radius=0,
                                                scrollbar_button_color=CARD_2,
                                                scrollbar_button_hover_color=BORDER)
        self.clip_list.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 12))
        self.clip_list.grid_columnconfigure(0, weight=1)
        return page

    # ------------------------------------------------------------ settings page
    def _section(self, parent, title, subtitle=None):
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=16, border_width=1, border_color=BORDER)
        card.pack(fill="x", padx=12, pady=8)
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(card, text=title, font=self.f_h2, text_color=TEXT).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=22, pady=(18, 0 if subtitle else 6))
        if subtitle:
            ctk.CTkLabel(card, text=subtitle, font=self.f_small, text_color=MUTED).grid(
                row=1, column=0, columnspan=2, sticky="w", padx=22, pady=(0, 6))
        card.next_row = 2
        return card

    def _setting(self, card, title, desc, build):
        r = card.next_row
        card.next_row += 1
        txt = ctk.CTkFrame(card, fg_color="transparent")
        txt.grid(row=r, column=0, sticky="w", padx=22, pady=10)
        ctk.CTkLabel(txt, text=title, font=self.f_label, text_color=TEXT).pack(anchor="w")
        if desc:
            ctk.CTkLabel(txt, text=desc, font=self.f_small, text_color=MUTED, wraplength=360,
                         justify="left").pack(anchor="w")
        ctrl = build(card)
        ctrl.grid(row=r, column=1, sticky="e", padx=22, pady=10)
        return ctrl

    def _switch(self, master, var):
        return ctk.CTkSwitch(master, text="", variable=var, onvalue=True, offvalue=False,
                             command=self.on_setting_change, progress_color=ACCENT, fg_color=BORDER,
                             button_color="#ffffff", button_hover_color="#e6e6e6",
                             switch_width=46, switch_height=24)

    def _build_settings(self, parent):
        page = ctk.CTkFrame(parent, fg_color=BG, corner_radius=0)
        page.grid_rowconfigure(1, weight=1)
        page.grid_columnconfigure(0, weight=1)
        head = ctk.CTkFrame(page, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=32, pady=(28, 6))
        ctk.CTkLabel(head, text="Settings", font=self.f_h1, text_color=TEXT).pack(anchor="w")
        ctk.CTkLabel(head, text="Changes are saved automatically.", font=self.f_body,
                     text_color=MUTED).pack(anchor="w")

        scroll = ctk.CTkScrollableFrame(page, fg_color=BG, corner_radius=0,
                                        scrollbar_button_color=CARD_2, scrollbar_button_hover_color=BORDER)
        scroll.grid(row=1, column=0, sticky="nsew", padx=20, pady=(4, 16))

        # --- Recording
        rec = self._section(scroll, "Recording", "Capture runs on your RTX GPU, so games stay smooth.")
        self.lock_lbl = ctk.CTkLabel(rec, text="", font=self.f_small, text_color=AMBER)
        self.lock_lbl.grid(row=2, column=0, columnspan=2, sticky="w", padx=22)
        rec.next_row = 3

        self.w_len = self._setting(rec, "Clip length", "How far back each saved clip goes.",
                                   lambda m: ctk.CTkSegmentedButton(m, values=[f"{x}s" for x in CLIP_LENGTHS],
                                                                    command=self.on_setting_change,
                                                                    **self.seg_style))
        self.w_len.set(f"{nearest(int(self.s['clip_seconds']), CLIP_LENGTHS)}s")

        self.w_fps = self._setting(rec, "Frame rate", "Higher looks smoother but makes bigger files.",
                                   lambda m: ctk.CTkSegmentedButton(m, values=FPS_OPTIONS,
                                                                    command=self.on_setting_change,
                                                                    **self.seg_style))
        self.w_fps.set(str(nearest(int(self.s["fps"]), [int(x) for x in FPS_OPTIONS])))

        self.w_quality = self._setting(rec, "Quality", "Low 10, Medium 20, High 35, Ultra 50 Mbps.",
                                       lambda m: ctk.CTkSegmentedButton(m, values=list(QUALITY),
                                                                        command=self.on_setting_change,
                                                                        **self.seg_style))
        self.w_quality.set(bitrate_to_quality(self.s["bitrate"]))

        self.w_codec = self._setting(rec, "Video format",
                                     "H.264 plays everywhere (Discord, phones). HEVC makes smaller files.",
                                     lambda m: ctk.CTkSegmentedButton(m, values=list(CODECS),
                                                                      command=self.on_setting_change,
                                                                      **self.seg_style))
        self.w_codec.set(self.s["codec"] if self.s["codec"] in CODECS else "H.264")

        self.v_monitor = tk.StringVar(value=f"Monitor {int(self.s['monitor']) + 1}")
        self.w_monitor = self._setting(rec, "Monitor", "Which screen to record.",
                                       lambda m: ctk.CTkOptionMenu(m, values=[f"Monitor {i}" for i in range(1, 5)],
                                                                   variable=self.v_monitor, width=160,
                                                                   command=self.on_setting_change,
                                                                   **self.opt_style))

        self.v_audio = tk.StringVar(value=self.s["audio_device"])

        def audio_ctrl(m):
            f = ctk.CTkFrame(m, fg_color="transparent")
            self.w_audio = ctk.CTkOptionMenu(f, values=["None"], variable=self.v_audio, width=260,
                                             dynamic_resizing=False, command=self.on_setting_change,
                                             **self.opt_style)
            self.w_audio.pack(side="left")
            self.w_audio_refresh = ctk.CTkButton(f, text="↻", width=34, command=self.reload_audio,
                                                 **self.ghost_btn)
            self.w_audio_refresh.pack(side="left", padx=(8, 0))
            return f

        self._setting(rec, "Audio", "Record desktop sound. Needs 'Stereo Mix' or a virtual audio cable.",
                      audio_ctrl)

        self.capture_widgets = [self.w_len, self.w_fps, self.w_quality, self.w_codec,
                                self.w_monitor, self.w_audio, self.w_audio_refresh]

        # --- Hotkey
        hk = self._section(scroll, "Hotkey")
        self.hk_btn = self._setting(
            hk, "Save clip", "Works everywhere, even in fullscreen games. Click it, then press your new "
                             "shortcut. Esc cancels.",
            lambda m: ctk.CTkButton(m, text="", width=230, height=40, corner_radius=10, font=self.f_label,
                                    fg_color=CARD_2, hover_color=BORDER, text_color=TEXT, border_width=1,
                                    border_color=BORDER, command=self.begin_capture))
        ctk.CTkLabel(hk, text="", height=6).grid(row=hk.next_row, column=0)

        # --- Storage
        st = self._section(scroll, "Storage")
        self.v_dir = tk.StringVar(value=self.s["clips_dir"])

        def dir_ctrl(m):
            f = ctk.CTkFrame(m, fg_color="transparent")
            e = ctk.CTkEntry(f, textvariable=self.v_dir, width=300, height=32, corner_radius=8,
                             fg_color=CARD_2, border_color=BORDER, text_color=TEXT, font=self.f_small)
            e.pack(side="left")
            e.bind("<Return>", lambda _e: self.apply_dir())
            e.bind("<FocusOut>", lambda _e: self.apply_dir(quiet=True))
            ctk.CTkButton(f, text="Browse", width=80, command=self.browse,
                          **self.ghost_btn).pack(side="left", padx=(8, 0))
            return f

        self._setting(st, "Clips folder", "Where your clips are saved.", dir_ctrl)
        ctk.CTkLabel(st, text="", height=6).grid(row=st.next_row, column=0)

        # --- Startup & background
        bg = self._section(scroll, "Startup & background")
        self.v_auto = tk.BooleanVar(value=bool(self.s["autostart_recording"]))
        self.v_startup = tk.BooleanVar(value=bool(self.s["run_at_startup"]))
        self.v_tray = tk.BooleanVar(value=bool(self.s["tray_on_close"]))
        self._setting(bg, "Start recording automatically", "Begin recording as soon as LiteClip opens.",
                      lambda m: self._switch(m, self.v_auto))
        self._setting(bg, "Start with Windows", "Launch quietly in the system tray when you log in.",
                      lambda m: self._switch(m, self.v_startup))
        self._setting(bg, "Keep running in the tray", "Minimising or closing the window hides it to the "
                                                      "tray instead of quitting.",
                      lambda m: self._switch(m, self.v_tray))
        ctk.CTkLabel(bg, text="", height=6).grid(row=bg.next_row, column=0)

        # --- Troubleshooting
        tr = self._section(scroll, "Troubleshooting")
        self.ffmpeg_lbl = self._setting(
            tr, "FFmpeg log", "Shows the recorder's last error if recording or saving fails.",
            lambda m: ctk.CTkButton(m, text="View log", width=110, command=self.open_log, **self.ghost_btn))
        self.ffmpeg_path_lbl = ctk.CTkLabel(tr, text="", font=self.f_small, text_color=MUTED,
                                            wraplength=640, justify="left")
        self.ffmpeg_path_lbl.grid(row=tr.next_row, column=0, columnspan=2, sticky="w", padx=22, pady=(0, 4))
        ctk.CTkLabel(tr, text=f"LiteClip {APP_VERSION}", font=self.f_small, text_color=MUTED).grid(
            row=tr.next_row + 1, column=0, columnspan=2, sticky="w", padx=22, pady=(0, 16))
        self.update_ffmpeg_label()
        ctk.CTkLabel(scroll, text="", height=8).pack()
        return page

    # ------------------------------------------------------------ toast
    def _build_toast(self):
        self.toast = ctk.CTkFrame(self.main, fg_color=CARD_2, corner_radius=12, border_width=1, border_color=BORDER)
        self.toast_lbl = ctk.CTkLabel(self.toast, text="", font=self.f_body, text_color=TEXT,
                                      wraplength=560, justify="left")
        self.toast_lbl.pack(padx=18, pady=11)
        for w in (self.toast, self.toast_lbl):
            w.bind("<Button-1>", lambda _e: self.on_toast_click())

    def notify(self, text, kind="info", link=None, ms=3800):
        color = {"info": TEXT, "ok": GREEN, "error": RED, "warn": AMBER}.get(kind, TEXT)
        self.toast_lbl.configure(text=text, text_color=color)
        self.toast.configure(border_color=color if kind != "info" else BORDER)
        self.toast_link = link
        self.toast.place(relx=0.5, rely=1.0, y=-24, anchor="s")
        self.toast.lift()
        if self.toast_job:
            self.root.after_cancel(self.toast_job)
        self.toast_job = self.root.after(ms, self.toast.place_forget)

    def on_toast_click(self):
        if self.toast_link:
            self.reveal(self.toast_link)
        self.toast.place_forget()

    # ------------------------------------------------------------ tray
    def _build_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Open LiteClip", lambda icon, item: self.q.put(("show", None)), default=True),
            pystray.MenuItem("Save clip", lambda icon, item: self.q.put(("save", None))),
            pystray.MenuItem(lambda item: "Stop recording" if self.recording else "Start recording",
                             lambda icon, item: self.q.put(("toggle", None))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda icon, item: self.q.put(("quit", None))),
        )
        self.tray = pystray.Icon(APP_NAME, make_icon_image(64), "LiteClip - idle", menu)
        self.tray.run_detached()

    def update_tray(self):
        try:
            self.tray.icon = make_icon_image(64, self.recording)
            self.tray.title = "LiteClip - recording" if self.recording else "LiteClip - idle"
            self.tray.update_menu()
        except Exception:
            pass

    def show_window(self):
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()
        self.refresh_clips()

    def hide_to_tray(self):
        self.root.withdraw()
        if not self.told_about_tray:
            self.told_about_tray = True
            try:
                self.tray.notify("LiteClip is still running. Right-click the tray icon to quit.", APP_NAME)
            except Exception:
                pass

    def on_close(self):
        if self.v_tray.get():
            self.hide_to_tray()
        else:
            self.quit()

    def on_unmap(self, event):
        if event.widget is self.root and self.v_tray.get() and self.root.state() == "iconic":
            self.root.after(50, self.hide_to_tray)

    def quit(self):
        if self.quitting:
            return
        self.quitting = True
        try:
            self.gather()
        except Exception:
            pass
        self.rec.stop()
        self.hotkey.stop()
        try:
            self.tray.stop()
        except Exception:
            pass
        shutil.rmtree(BUF_DIR, ignore_errors=True)
        self.root.destroy()

    # ------------------------------------------------------------ settings logic
    def gather(self):
        s = self.s
        s["clip_seconds"] = int(self.w_len.get().rstrip("s") or 30)
        s["fps"] = int(self.w_fps.get() or 60)
        s["bitrate"] = QUALITY.get(self.w_quality.get(), "20M")
        s["codec"] = self.w_codec.get() or "H.264"
        s["monitor"] = int(self.v_monitor.get().split()[-1]) - 1
        s["audio_device"] = self.v_audio.get() or "None"
        s["clips_dir"] = clean_dir(self.v_dir.get())
        s["autostart_recording"] = bool(self.v_auto.get())
        s["tray_on_close"] = bool(self.v_tray.get())
        if bool(self.v_startup.get()) != bool(s["run_at_startup"]):
            if set_run_at_startup(bool(self.v_startup.get())):
                s["run_at_startup"] = bool(self.v_startup.get())
            else:
                self.v_startup.set(s["run_at_startup"])
                self.notify("Couldn't change the Windows startup setting.", "error")
        save_settings(s)
        return dict(s)

    def on_setting_change(self, *_):
        self.gather()
        self.update_stats()
        self.update_hero(force=True)

    def update_stats(self):
        s = self.s
        self.stat_vals["length"].configure(text=f"{s['clip_seconds']} seconds")
        self.stat_vals["video"].configure(text=f"{s['fps']} fps · {bitrate_to_quality(s['bitrate'])} · {s['codec']}")

    def apply_dir(self, quiet=False):
        folder = clean_dir(self.v_dir.get())
        if self.v_dir.get() != folder:
            self.v_dir.set(folder)
        problem = check_writable(folder)
        if problem:
            self.notify(problem, "error", ms=8000)
            return
        changed = os.path.normcase(folder) != os.path.normcase(self.s.get("clips_dir", ""))
        if changed or not quiet:
            self.gather()
            self.refresh_clips()
            self.notify(f"Clips will be saved to {folder}", "ok")

    def browse(self):
        d = filedialog.askdirectory(initialdir=clean_dir(self.v_dir.get()))
        if d:
            self.v_dir.set(os.path.normpath(d))
            self.apply_dir()

    def reload_audio(self):
        self.notify("Looking for audio devices…")
        threading.Thread(target=lambda: self.q.put(("audio", list_audio_devices())), daemon=True).start()

    def update_ffmpeg_label(self):
        self.ffmpeg_path_lbl.configure(
            text=f"Using FFmpeg at: {FFMPEG}" if ffmpeg_available() else "FFmpeg: not found")

    def offer_ffmpeg_install(self):
        self.show_window()
        if not messagebox.askyesno(
                APP_NAME, "LiteClip needs FFmpeg (free) to record your screen, and it isn't installed yet.\n\n"
                          "Install it now? A window will open and install it with winget. "
                          "It's about 100 MB and takes a minute or two."):
            self.notify("LiteClip can't record without FFmpeg. Put ffmpeg.exe next to LiteClip, "
                        "or restart LiteClip to install it.", "warn", ms=9000)
            return
        try:
            proc = subprocess.Popen(["winget", "install", "--id", "Gyan.FFmpeg", "-e",
                                     "--accept-source-agreements", "--accept-package-agreements"],
                                    creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        except OSError:
            messagebox.showinfo(APP_NAME, "winget isn't available on this PC, so FFmpeg has to be downloaded "
                                          "manually.\n\nYour browser will open the download page. Get the "
                                          "\"release full\" build, unzip it, and put bin\\ffmpeg.exe next to "
                                          "LiteClip.exe.")
            os.startfile("https://www.gyan.dev/ffmpeg/builds/")
            return
        self.notify("Installing FFmpeg… LiteClip will be ready as soon as it finishes.", ms=8000)
        threading.Thread(target=lambda: self.q.put(("ffmpeg_installed", proc.wait())), daemon=True).start()

    def open_log(self):
        if os.path.isfile(LOG_FILE) and os.path.getsize(LOG_FILE) > 0:
            subprocess.Popen(["notepad.exe", LOG_FILE])
        else:
            self.notify("The log is empty, so FFmpeg hasn't reported any errors.")

    # ------------------------------------------------------------ hotkey
    def hotkey_text(self):
        return hotkey_label(self.s["hotkey_mods"], self.s["hotkey_vk"])

    def refresh_hotkey_labels(self):
        hk = self.hotkey_text()
        if not self.capturing:
            self.hk_btn.configure(text=hk, border_color=BORDER)
        self.side_hotkey.configure(text=f"Save clip: {hk}")
        self.save_btn.configure(text=f"Save clip  ·  {hk}")
        self.update_hero(force=True)

    def begin_capture(self):
        if self.capturing:
            return
        self.capturing = True
        self.cap_mods = set()
        self.hotkey.set(0, 0)  # pause the current hotkey while choosing
        self.hk_btn.configure(text="Press your shortcut…", border_color=ACCENT)
        self.root.bind("<KeyPress>", self.on_cap_press)
        self.root.bind("<KeyRelease>", self.on_cap_release)
        self.root.focus_set()

    def on_cap_press(self, e):
        ks = e.keysym
        if ks in MOD_KEYSYMS:
            self.cap_mods.add(MOD_KEYSYMS[ks])
            return "break"
        if ks in IGNORED_KEYSYMS:
            return "break"
        if ks == "Escape":
            self.end_capture(None)
            return "break"
        mods = 0
        for m in self.cap_mods:
            mods |= m
        if e.state & 0x0004:
            mods |= MOD_CONTROL
        if e.state & 0x0001:
            mods |= MOD_SHIFT
        if e.state & 0x20000:
            mods |= MOD_ALT
        self.end_capture((mods, e.keycode))
        return "break"

    def on_cap_release(self, e):
        if e.keysym in MOD_KEYSYMS:
            self.cap_mods.discard(MOD_KEYSYMS[e.keysym])
        return "break"

    def end_capture(self, combo):
        self.capturing = False
        self.root.unbind("<KeyPress>")
        self.root.unbind("<KeyRelease>")
        if combo:
            problem = validate_hotkey(*combo)
            if problem:
                self.notify(problem, "warn", ms=6000)
                combo = None
        if combo and self.hotkey.set(*combo):
            self.s["hotkey_mods"], self.s["hotkey_vk"] = combo
            save_settings(self.s)
            self.notify(f"Save hotkey set to {self.hotkey_text()}", "ok")
        else:
            if combo:
                self.notify(f"{hotkey_label(*combo)} is already used by another app. Try a different one.",
                            "error", ms=6000)
            self.hotkey.set(self.s["hotkey_mods"], self.s["hotkey_vk"])
        self.refresh_hotkey_labels()

    # ------------------------------------------------------------ recording
    def toggle(self):
        self.stop() if self.recording else self.start()

    def start(self):
        if self.recording:
            return
        s = self.gather()
        if not ffmpeg_available():
            self.offer_ffmpeg_install()
            return
        try:
            self.rec.start(s)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Couldn't start recording:\n\n{e}")
            return
        self.rec_started = time.time()
        self.set_recording(True)
        self.root.after(6000, self.check_buffer)

    def stop(self):
        self.rec.stop()
        self.set_recording(False)

    def handle_recorder_exit(self):
        """FFmpeg stopped by itself. If it had been working (e.g. a game changed resolution, a UAC prompt,
        sleep), restart it quietly. Only bother the user if it keeps failing."""
        err = Recorder.last_error()
        self.rec.stop()
        now = time.time()
        self.restarts = [t for t in getattr(self, "restarts", []) if now - t < 120]
        if now - self.rec_started > 10 and len(self.restarts) < 3:
            self.restarts.append(now)
            try:
                self.rec.start(self.s)
                self.rec_started = now
                self.notify("Recording had a hiccup and was restarted automatically.", "warn", ms=5000)
                return
            except Exception:
                pass
        self.set_recording(False)
        message = friendly_error(err) or "The recorder stopped unexpectedly."
        if self.root.state() == "withdrawn":
            try:
                self.tray.notify(message, "LiteClip stopped recording")
            except Exception:
                pass
            self.notify(message, "error", ms=15000)
        else:
            messagebox.showerror(APP_NAME, message + "\n\nDetails:\n" + (err[-600:] or "(none)"))

    def check_buffer(self):
        if self.recording and not buffer_has_video():
            err = Recorder.last_error()
            self.notify("Recording is on, but no video is being captured. See Settings > View log.", "error",
                        ms=9000)
            if err:
                messagebox.showwarning(APP_NAME, "The recorder isn't capturing video.\n\n"
                                       + (friendly_error(err) or "") + "\n\nDetails:\n" + err[-600:])

    def set_recording(self, on):
        self.recording = on
        if on:
            self.pill.configure(text="   ●  RECORDING   ", text_color=RED, fg_color=RED_BG)
            self.rec_btn.configure(text="Stop recording", fg_color=RED, hover_color=RED_H, text_color="#ffffff")
            self.side_dot.configure(text_color=RED)
            self.side_status.configure(text="  Recording")
        else:
            self.pill.configure(text="   ●  IDLE   ", text_color=MUTED, fg_color=CARD_2)
            self.rec_btn.configure(text="Start recording", fg_color=ACCENT, hover_color=ACCENT_H,
                                   text_color="#ffffff")
            self.side_dot.configure(text_color=MUTED)
            self.side_status.configure(text="  Idle")
        for w in self.capture_widgets:
            w.configure(state="disabled" if on else "normal")
        self.lock_lbl.configure(text="Stop recording to change these settings." if on else "")
        self.update_hero(force=True)
        self.update_tray()

    def update_hero(self, force=False):
        hk, length = self.hotkey_text(), self.s["clip_seconds"]
        if self.recording:
            elapsed = time.time() - self.rec_started
            frac = min(1.0, elapsed / max(1, length))
            if frac < 1:
                title = "Recording"
                sub = f"Filling the replay buffer… {int(elapsed)} of {length} s. {hk} saves what's there so far."
            else:
                title = "Ready to clip"
                sub = f"Press {hk} anytime to save the last {length} seconds."
        else:
            frac = 0
            title = "Not recording"
            sub = f"Start recording, then press {hk} in-game to save the last {length} seconds."
        if force or self.hero_title.cget("text") != title or self.hero_sub.cget("text") != sub:
            self.hero_title.configure(text=title)
            self.hero_sub.configure(text=sub)
        self.buf_bar.set(frac)

    # ------------------------------------------------------------ saving
    def save(self):
        if not self.recording:
            self.notify("Start recording first, then save a clip.", "warn")
            beep(440, 120)
            return
        if self.saving:
            return
        folder = clean_dir(self.v_dir.get())
        problem = check_writable(folder)
        if problem:
            self.notify(problem, "error", ms=8000)
            beep(300, 250)
            return
        self.saving = True
        self.save_btn.configure(text="Saving…", state="disabled")
        s = dict(self.s, clips_dir=folder)

        def work():
            try:
                path = save_clip(s)
                beep()
                self.q.put(("saved", path))
            except Exception as e:
                beep(300, 250)
                self.q.put(("save_failed", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def finish_save(self):
        self.saving = False
        self.save_btn.configure(state="normal")
        self.refresh_hotkey_labels()

    # ------------------------------------------------------------ clips list
    def refresh_clips(self):
        for w in self.clip_list.winfo_children():
            w.destroy()
        self.thumb_labels.clear()
        folder = clean_dir(self.v_dir.get())
        files = sorted(glob.glob(os.path.join(folder, "*.mp4")), key=safe_mtime, reverse=True)
        today = datetime.date.today()
        self.stat_vals["today"].configure(
            text=str(sum(1 for f in files if datetime.date.fromtimestamp(safe_mtime(f)) == today)))
        if not files:
            ctk.CTkLabel(self.clip_list, text=f"No clips yet. Start recording and press {self.hotkey_text()} "
                                              f"to save your first one.",
                         font=self.f_body, text_color=MUTED).grid(row=0, column=0, pady=40)
            return
        for i, f in enumerate(files[:40]):
            self._clip_row(f, i)

    def _clip_row(self, path, idx):
        row = ctk.CTkFrame(self.clip_list, fg_color=CARD_2, corner_radius=12)
        row.grid(row=idx, column=0, sticky="ew", padx=6, pady=4)
        row.grid_columnconfigure(1, weight=1)

        thumb = ctk.CTkLabel(row, text="", width=112, height=63, fg_color=BORDER, corner_radius=8)
        thumb.grid(row=0, column=0, rowspan=2, padx=(10, 14), pady=8)
        thumb.bind("<Button-1>", lambda _e, p=path: self.play(p))
        self.thumb_labels[path] = thumb
        t = self.thumbs.request(path)
        if t:
            self.set_thumb(path, t)

        try:
            size = os.path.getsize(path) / 1_000_000
        except OSError:
            size = 0
        ctk.CTkLabel(row, text=friendly_time(safe_mtime(path)), font=self.f_label, text_color=TEXT).grid(
            row=0, column=1, sticky="sw", pady=(14, 0))
        ctk.CTkLabel(row, text=f"{size:.0f} MB  ·  {os.path.basename(path)}", font=self.f_small,
                     text_color=MUTED).grid(row=1, column=1, sticky="nw", pady=(0, 14))

        btns = ctk.CTkFrame(row, fg_color="transparent")
        btns.grid(row=0, column=2, rowspan=2, padx=12)
        ctk.CTkButton(btns, text="Play", width=64, height=30, corner_radius=8, font=self.f_small,
                      fg_color=ACCENT, hover_color=ACCENT_H, command=lambda p=path: self.play(p)).pack(side="left")
        ctk.CTkButton(btns, text="Show", width=64, height=30, corner_radius=8, font=self.f_small,
                      fg_color=BORDER, hover_color="#3a404b", text_color=TEXT,
                      command=lambda p=path: self.reveal(p)).pack(side="left", padx=6)
        ctk.CTkButton(btns, text="Delete", width=64, height=30, corner_radius=8, font=self.f_small,
                      fg_color="transparent", hover_color=RED_BG, text_color=RED,
                      command=lambda p=path: self.delete_clip(p)).pack(side="left")

    def set_thumb(self, clip, thumb):
        lbl = self.thumb_labels.get(clip)
        if not lbl:
            return
        try:
            with Image.open(thumb) as im:
                img = ctk.CTkImage(light_image=im.copy(), dark_image=im.copy(), size=(112, 63))
            self.thumb_images[clip] = img
            lbl.configure(image=img, fg_color="transparent")
        except Exception:
            pass

    def play(self, path):
        try:
            os.startfile(path)
        except OSError as e:
            self.notify(f"Couldn't open clip: {e}", "error")

    def reveal(self, path):
        if path and os.path.isfile(path):
            subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
        else:
            self.notify("That clip can't be found any more.", "error")

    def delete_clip(self, path):
        if not messagebox.askyesno(APP_NAME, f"Move {os.path.basename(path)} to the Recycle Bin?"):
            return
        thumb = thumb_path_for(path)
        try:
            ok = send_to_recycle_bin(path)
        except Exception:
            ok = False
        if ok and not os.path.exists(path):
            try:
                os.remove(thumb)
            except OSError:
                pass
            self.notify("Clip moved to the Recycle Bin.")
        else:
            self.notify("Couldn't delete that clip. Is it open in a video player?", "error")
        self.refresh_clips()

    def open_folder(self):
        d = clean_dir(self.v_dir.get())
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

    # ------------------------------------------------------------ event loop
    def poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "save":
                    if not self.capturing:
                        self.save()
                elif kind == "saved":
                    self.finish_save()
                    self.notify(f"Clip saved · {os.path.basename(data)}   (click to show it)", "ok", link=data,
                                ms=5000)
                    self.refresh_clips()
                elif kind == "save_failed":
                    self.finish_save()
                    self.notify(f"Couldn't save clip: {data}", "error", ms=8000)
                elif kind == "audio":
                    values = ["None"] + data
                    if data:  # an empty list usually means FFmpeg isn't there yet, so keep the saved choice
                        self.w_audio.configure(values=values)
                        if self.v_audio.get() not in values:
                            self.v_audio.set("None")
                            self.gather()
                elif kind == "ffmpeg_installed":
                    global FFMPEG
                    FFMPEG = resolve_ffmpeg(self.s["ffmpeg"])
                    self.update_ffmpeg_label()
                    if ffmpeg_available():
                        self.notify("FFmpeg is installed. You're ready to record!", "ok", ms=6000)
                        self.reload_audio()
                    else:
                        self.notify("FFmpeg didn't install. Check the installer window for the error.", "error",
                                    ms=9000)
                elif kind == "thumb":
                    self.set_thumb(*data)
                elif kind == "show":
                    self.show_window()
                elif kind == "toggle":
                    self.toggle()
                elif kind == "quit":
                    self.quit()
                    return
        except queue.Empty:
            pass

        if self.recording and not self.rec.running():
            self.handle_recorder_exit()

        self.tick += 1
        if self.tick % 5 == 0 and self.recording:
            self.update_hero()
        self.root.after(100, self.poll)


# =================================================================== entry point
def main():
    if "--make-icon" in sys.argv:
        write_ico(os.path.join(APP_HOME, "liteclip.ico"))
        return

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    mutex = kernel32.CreateMutexW(None, False, "LiteClip_SingleInstance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        r = tk.Tk()
        r.withdraw()
        messagebox.showinfo(APP_NAME, "LiteClip is already running.\n\nLook for its icon in the system tray "
                                      "(bottom-right, near the clock - it may be under the ^ arrow).")
        return

    kill_stale_recorder()  # only one LiteClip can run, so any recorder still listed is a leftover
    threading.Thread(target=prune_thumbs, daemon=True).start()

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    App(root, start_minimized="--minimized" in sys.argv)
    root.mainloop()
    del mutex


if __name__ == "__main__":
    main()
