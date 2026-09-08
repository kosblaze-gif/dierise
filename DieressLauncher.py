"""
Vanta Vision Updater Launcher
- Opens updater UI first
- Checks/downloads updates from manifest
- Then loads protected core only after Launch is clicked
- Exposes VWorker so Helios/CVPython accepts it
"""

import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import site
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import zipfile

try:
    import tkinter as tk
    from tkinter import messagebox
except Exception:
    tk = None
    messagebox = None

APP_VERSION = "6.1.1"
UPDATE_MANIFEST_URL = os.environ.get(
    "VANTA_UPDATE_MANIFEST_URL",
    "https://raw.githubusercontent.com/kosblaze-gif/updatingvnta/main/update_manifest.json"
).strip()
UPDATE_TIMEOUT_SECS = 20
AUTO_CHECK_ON_OPEN = True

_HERE = os.path.dirname(os.path.abspath(__file__))
_VI = sys.version_info
_CORE_BASENAME = f"_vv_core.cp{_VI.major}{_VI.minor}-win_amd64.pyd"
_CORE_PATH = os.path.join(_HERE, _CORE_BASENAME)

_PY_ENV = os.path.join(_HERE, "py-env")
_PY_ENV_SP = os.path.join(_PY_ENV, "Lib", "site-packages")
_PY_ENV_EXE = os.path.join(_PY_ENV, "python.exe")
_PYWIN32_SYS32 = os.path.join(_PY_ENV_SP, "pywin32_system32")

LOG_PATH = os.path.join(_HERE, "VantaUpdater.log")
BACKUP_DIR = os.path.join(_HERE, "launcher_backups")
PENDING_DIR = os.path.join(_HERE, "pending_update")
LOCAL_MANIFEST_CACHE = os.path.join(_HERE, "update_manifest_cache.json")

# Legacy fallback digest only. Preferred verification uses manifest pyd sha256.
_E = "LA2pbg/jTlApBxLDJbwjGiQBQPB31znZu34OnjclTS4="
_K = "dnYtdmlzaW9uLTIwMjYtcHJpdmF0ZS1idWlsZA=="

BG = "#061326"
PANEL = "#0B1D35"
PANEL_2 = "#0F2A4A"
BLUE = "#28A8FF"
BLUE_2 = "#147BFF"
CYAN = "#55E6FF"
TEXT = "#EAF7FF"
MUTED = "#8EB4D9"
GOOD = "#2FE58E"
WARN = "#FFC857"
BAD = "#FF5E7A"
BORDER = "#173C62"


def log(msg):
    print(f"[VantaUpdater] {msg}", flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _fail(msg):
    raise ImportError(f"Vanta Vision: {msg}")


def _bundled_pyenv_is_complete():
    try:
        if not os.path.isfile(_PY_ENV_EXE):
            return False
        lib = os.path.join(_PY_ENV, "Lib")
        required = [
            os.path.join(lib, "re", "__init__.py"),
            os.path.join(lib, "json", "__init__.py"),
            os.path.join(lib, "socket.py"),
            os.path.join(lib, "threading.py"),
            os.path.join(lib, "tempfile.py"),
            os.path.join(lib, "subprocess.py"),
            os.path.join(lib, "random.py"),
        ]
        return all(os.path.exists(p) for p in required)
    except Exception:
        return False


def _find_safe_python_exe():
    guesses = [
        os.path.expandvars(r"%USERPROFILE%\Miniconda3\envs\VantaVisionENV\python.exe"),
        os.path.expandvars(r"%USERPROFILE%\Miniconda3\envs\VantaENV\python.exe"),
        os.path.expandvars(r"%USERPROFILE%\miniconda3\envs\VantaVisionENV\python.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python311\python.exe"),
        r"C:\Python311\python.exe",
        r"C:\Program Files\Python311\python.exe",
    ]
    for p in guesses:
        try:
            if p and os.path.exists(p):
                return p
        except Exception:
            pass
    try:
        import shutil as _shutil
        for name in ("python.exe", "python3.exe", "pythonw.exe", "python"):
            p = _shutil.which(name)
            if p and os.path.exists(p):
                return p
    except Exception:
        pass
    return ""


def _wire_bundled_env():
    try:
        if os.path.isdir(_PY_ENV_SP):
            site.addsitedir(_PY_ENV_SP)
    except Exception:
        pass
    try:
        if os.path.isdir(_PYWIN32_SYS32) and hasattr(os, "add_dll_directory"):
            os.add_dll_directory(_PYWIN32_SYS32)
    except Exception:
        pass
    try:
        if _bundled_pyenv_is_complete():
            os.environ["VV_PYTHON_EXE"] = _PY_ENV_EXE
        else:
            safe = _find_safe_python_exe()
            if safe:
                os.environ["VV_PYTHON_EXE"] = safe
            else:
                os.environ.pop("VV_PYTHON_EXE", None)
            try:
                with open(os.path.join(_HERE, "VantaLauncher_env_warning.log"), "a", encoding="utf-8") as f:
                    f.write("Bundled py-env incomplete; updater using fallback Python: " + str(os.environ.get("VV_PYTHON_EXE", "system default")) + "\n")
            except Exception:
                pass
    except Exception:
        pass


def _legacy_expected_digest():
    ob = base64.b64decode(_E)
    kb = base64.b64decode(_K)
    return bytes(b ^ kb[i % len(kb)] for i, b in enumerate(ob))


def _sha256_file_hex(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_bytes_hex(data):
    return hashlib.sha256(data).hexdigest()


def _version_tuple(v):
    nums = re.findall(r"\d+", str(v or "0"))
    return tuple(int(x) for x in nums[:6]) if nums else (0,)


def _is_newer_version(latest, current):
    return _version_tuple(latest) > _version_tuple(current)


def _download_bytes(url):
    req = urllib.request.Request(
        str(url),
        headers={"User-Agent": f"VantaUpdater/{APP_VERSION}", "Accept": "application/octet-stream, application/json, */*"},
    )
    with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT_SECS) as r:
        return r.read()


def _backup_file(path):
    try:
        if os.path.isfile(path):
            os.makedirs(BACKUP_DIR, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            shutil.copy2(path, os.path.join(BACKUP_DIR, f"{os.path.basename(path)}.{stamp}.bak"))
    except Exception:
        pass


def _replace_file(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _backup_file(path)
        tmp = path + ".download"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return True
    except Exception as exc:
        log(f"replace failed for {path}: {exc}")
        try:
            os.makedirs(PENDING_DIR, exist_ok=True)
            with open(os.path.join(PENDING_DIR, os.path.basename(path)), "wb") as f:
                f.write(data)
        except Exception:
            pass
        return False


def _apply_pending_updates():
    if not os.path.isdir(PENDING_DIR):
        return []
    applied = []
    for name in list(os.listdir(PENDING_DIR)):
        src = os.path.join(PENDING_DIR, name)
        dst = os.path.join(_HERE, name)
        if not os.path.isfile(src):
            continue
        try:
            _backup_file(dst)
            os.replace(src, dst)
            applied.append(name)
        except Exception as exc:
            log(f"pending still locked for {name}: {exc}")
    return applied


def _runtime_sha_from_manifest(manifest):
    try:
        for item in manifest.get("files") or []:
            rel = str(item.get("path") or item.get("name") or "").replace("\\", "/").lower()
            if rel.endswith(".pyd"):
                s = str(item.get("sha256") or "").strip().lower()
                if re.fullmatch(r"[0-9a-f]{64}", s):
                    return s
        for k in ("runtime_sha256", "pyd_sha256"):
            s = str(manifest.get(k) or "").strip().lower()
            if re.fullmatch(r"[0-9a-f]{64}", s):
                return s
    except Exception:
        pass
    return ""


def _load_cached_manifest():
    try:
        if os.path.isfile(LOCAL_MANIFEST_CACHE):
            return json.loads(open(LOCAL_MANIFEST_CACHE, "r", encoding="utf-8").read())
    except Exception:
        pass
    return None


def _cache_manifest(manifest):
    try:
        with open(LOCAL_MANIFEST_CACHE, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
    except Exception:
        pass


def check_manifest():
    _apply_pending_updates()
    raw = _download_bytes(UPDATE_MANIFEST_URL)
    manifest = json.loads(raw.decode("utf-8", errors="replace"))
    _cache_manifest(manifest)
    return manifest


def update_needed(manifest):
    latest = str(manifest.get("latest_version") or manifest.get("version") or "").strip()
    min_required = str(manifest.get("min_required_version") or "").strip()
    force = bool(manifest.get("force_update"))
    return force or _is_newer_version(latest, APP_VERSION) or (min_required and _is_newer_version(min_required, APP_VERSION))


def install_update(manifest, status_cb=None):
    files = manifest.get("files") or []
    normalized = []
    for item in files:
        rel = str(item.get("path") or item.get("name") or "").strip().replace("\\", "/")
        url = str(item.get("url") or item.get("download_url") or "").strip()
        expected = str(item.get("sha256") or "").strip().lower()
        if rel and url and not rel.startswith("/") and ".." not in rel.split("/"):
            normalized.append({"path": rel, "url": url, "sha256": expected})

    def order(it):
        low = it["path"].lower()
        if low.endswith(".pyd"):
            return 0
        if low.endswith("vantalauncher.py"):
            return 9
        return 5

    installed = []
    for item in sorted(normalized, key=order):
        rel = item["path"]
        if status_cb:
            status_cb(f"Downloading {rel}...")
        data = _download_bytes(item["url"])
        if item["sha256"] and _hash_bytes_hex(data).lower() != item["sha256"]:
            raise RuntimeError(f"SHA256 mismatch for {rel}")
        dst = os.path.join(_HERE, *rel.split("/"))
        if not _replace_file(dst, data):
            raise RuntimeError(f"{rel} is locked. Update saved pending. Close Gtuner and reopen.")
        installed.append(rel)
        log(f"installed {rel}")
    _cache_manifest(manifest)
    return installed


def verify_runtime(manifest=None):
    _apply_pending_updates()
    if not os.path.isfile(_CORE_PATH):
        return False, f"Missing runtime: {_CORE_BASENAME}"
    actual = _sha256_file_hex(_CORE_PATH).lower()
    manifest = manifest or _load_cached_manifest() or {}
    expected = _runtime_sha_from_manifest(manifest)
    if expected:
        return (actual == expected), ("Runtime OK" if actual == expected else "Runtime SHA mismatch")
    try:
        return (bytes.fromhex(actual) == _legacy_expected_digest()), "Runtime OK"
    except Exception:
        return False, "Runtime integrity unknown"


def load_runtime():
    _wire_bundled_env()
    ok, msg = verify_runtime()
    if not ok:
        _fail(msg)
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    spec = importlib.util.spec_from_file_location("_vv_core", _CORE_PATH)
    if spec is None or spec.loader is None:
        _fail("could not create runtime module spec")
    core = importlib.util.module_from_spec(spec)
    sys.modules["_vv_core"] = core
    spec.loader.exec_module(core)
    for k in dir(core):
        if not k.startswith("__"):
            globals()[k] = getattr(core, k)
    return core


class VButton(tk.Button):
    def __init__(self, master, **kw):
        super().__init__(master, relief="flat", bd=0, cursor="hand2", font=("Segoe UI", 9, "bold"),
                         padx=12, pady=8, bg=kw.pop("bg", BLUE_2), fg=kw.pop("fg", "white"),
                         activebackground=kw.pop("activebackground", BLUE), activeforeground="white", **kw)


class UpdaterUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Vanta Vision Updater")
        self.root.geometry("460x320")
        self.root.resizable(False, False)
        self.root.configure(bg=BG)
        self.manifest = None
        self.launch = False
        self.pulse = False

        card = tk.Frame(root, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="both", expand=True, padx=14, pady=14)

        top = tk.Frame(card, bg=PANEL)
        top.pack(fill="x", padx=18, pady=(16, 8))
        self.logo = tk.Canvas(top, width=48, height=48, bg=PANEL, highlightthickness=0)
        self.logo.grid(row=0, column=0, rowspan=2, padx=(0, 12))
        self.logo_ring = self.logo.create_oval(5, 5, 43, 43, outline=BLUE, width=2)
        self.logo_text = self.logo.create_text(24, 24, text="V", fill=CYAN, font=("Segoe UI", 19, "bold"))
        tk.Label(top, text="VANTA VISION", bg=PANEL, fg=TEXT, font=("Segoe UI", 17, "bold")).grid(row=0, column=1, sticky="w")
        tk.Label(top, text="Updater • Runtime Verification", bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).grid(row=1, column=1, sticky="w")

        self.status = tk.Label(card, text="Updater ready.", bg=PANEL_2, fg=CYAN, font=("Segoe UI", 9, "bold"), padx=12, pady=8, wraplength=390)
        self.status.pack(fill="x", padx=18, pady=(8, 10))

        self.notes = tk.Label(card, text="Click Check, then Update, then Launch.", bg=PANEL, fg=MUTED, font=("Segoe UI", 8), wraplength=395, justify="left")
        self.notes.pack(fill="x", padx=18, pady=(0, 12))

        row = tk.Frame(card, bg=PANEL)
        row.pack(padx=18, pady=(0, 12))
        self.check_btn = VButton(row, text="Check", width=10, command=self.check_async)
        self.check_btn.grid(row=0, column=0, padx=5)
        self.update_btn = VButton(row, text="Update", width=10, command=self.update_async, state="disabled", bg="#0E5EB8")
        self.update_btn.grid(row=0, column=1, padx=5)
        self.launch_btn = VButton(row, text="Launch", width=10, command=self.do_launch, bg=GOOD, fg="#03120B", activebackground="#52FFAA")
        self.launch_btn.grid(row=0, column=2, padx=5)
        self.folder_btn = VButton(row, text="Folder", width=10, command=self.open_folder, bg="#12395F")
        self.folder_btn.grid(row=0, column=3, padx=5)

        ok, msg = verify_runtime()
        self.runtime = tk.Label(card, text=f"Runtime: {msg}", bg=PANEL, fg=GOOD if ok else WARN, font=("Segoe UI", 9))
        self.runtime.pack(anchor="w", padx=20)

        self.animate()
        if AUTO_CHECK_ON_OPEN:
            self.root.after(500, self.check_async)

    def set_status(self, msg, color=CYAN):
        self.root.after(0, lambda: self.status.config(text=str(msg), fg=color))

    def set_notes(self, notes):
        if isinstance(notes, list):
            txt = "  ".join("• " + str(x) for x in notes[:5])
        else:
            txt = str(notes)
        self.root.after(0, lambda: self.notes.config(text=txt))

    def refresh_runtime(self):
        ok, msg = verify_runtime(self.manifest)
        self.runtime.config(text=f"Runtime: {msg}", fg=GOOD if ok else WARN)

    def check_async(self):
        self.check_btn.config(state="disabled")
        self.set_status("Checking for updates...")
        threading.Thread(target=self._check, daemon=True).start()

    def _check(self):
        try:
            self.manifest = check_manifest()
            latest = str(self.manifest.get("latest_version") or self.manifest.get("version") or "unknown")
            self.set_notes(self.manifest.get("notes") or [f"Latest: {latest}"])
            if update_needed(self.manifest):
                self.set_status(f"Update available: {latest}", WARN)
                self.root.after(0, lambda: self.update_btn.config(state="normal"))
            else:
                self.set_status(f"Up to date: {latest}", GOOD)
        except Exception as exc:
            self.set_status(f"Check failed: {exc}", BAD)
        finally:
            self.root.after(0, lambda: self.check_btn.config(state="normal"))
            self.root.after(0, self.refresh_runtime)

    def update_async(self):
        if not self.manifest:
            self.check_async()
            return
        self.update_btn.config(state="disabled")
        self.launch_btn.config(state="disabled")
        threading.Thread(target=self._update, daemon=True).start()

    def _update(self):
        try:
            installed = install_update(self.manifest, self.set_status)
            self.set_status("Installed: " + ", ".join(installed), GOOD)
        except Exception as exc:
            log(traceback.format_exc())
            self.set_status(f"Update failed: {exc}", BAD)
            if messagebox:
                err = str(exc)
                self.root.after(0, lambda err=err: messagebox.showerror("Vanta Update Failed", err))
        finally:
            self.root.after(0, lambda: self.launch_btn.config(state="normal"))
            self.root.after(0, self.refresh_runtime)

    def do_launch(self):
        self.launch = True
        self.root.destroy()

    def open_folder(self):
        try:
            if sys.platform == "win32":
                os.startfile(_HERE)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", _HERE])
        except Exception as exc:
            self.set_status(str(exc), BAD)

    def animate(self):
        self.pulse = not self.pulse
        try:
            self.logo.itemconfig(self.logo_ring, outline=CYAN if self.pulse else BLUE, width=3 if self.pulse else 2)
            self.logo.itemconfig(self.logo_text, fill=CYAN if self.pulse else BLUE)
            if str(self.launch_btn["state"]) != "disabled":
                self.launch_btn.config(bg="#52FFAA" if self.pulse else GOOD)
        except Exception:
            pass
        self.root.after(700, self.animate)


def run_updater_ui_blocking():
    log("updater UI start")
    _wire_bundled_env()
    if tk is None:
        try:
            m = check_manifest()
            if update_needed(m):
                install_update(m, log)
        except Exception as exc:
            log(f"headless update skipped: {exc}")
        return True

    root = tk.Tk()
    ui = UpdaterUI(root)
    root.mainloop()
    return bool(ui.launch)


class GCVWorker:
    def __new__(cls, *args, **kwargs):
        if not run_updater_ui_blocking():
            raise ImportError("Vanta Vision: launch cancelled.")
        core = load_runtime()
        real = getattr(core, "GCVWorker", None)
        if real is None:
            raise ImportError("Vanta Vision: protected runtime does not expose GCVWorker.")
        return real(*args, **kwargs)


def main():
    if run_updater_ui_blocking():
        load_runtime()


if __name__ == "__main__":
    main()
