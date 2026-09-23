# -*- coding: utf-8 -*-
"""
DISM Tool  –  Windows Image Servicing & Deployment GUI
Requires: Python 3.8+, Windows, Run as Administrator.
"""

import ctypes, json, os, re, subprocess, sys, tempfile, threading, tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk
from datetime import datetime

# ── Executables ───────────────────────────────────────────────────────────────
DISM_EXE  = r"C:\Windows\System32\dism.exe"
DISKPART  = r"C:\Windows\System32\diskpart.exe"
ROBOCOPY  = r"C:\Windows\System32\Robocopy.exe"
BCDBOOT   = r"C:\Windows\System32\bcdboot.exe"
SYSPREP   = r"C:\Windows\System32\Sysprep\sysprep.exe"
WDSUTIL   = r"C:\Windows\System32\wdsutil.exe"
DISM_LOG  = r"C:\Windows\Logs\DISM\dism.log"
RECENT_FILE = os.path.join(os.environ.get("APPDATA", tempfile.gettempdir()), "dism_tool_recent.json")


def _find_oscdimg() -> str:
    """
    Locate oscdimg.exe by trying (in order):
      1. Windows registry  – KitsRoot10 / KitsRoot81 install paths
      2. Standard ADK candidate folders (multiple Kit versions × architectures)
      3. PATH environment variable
    Returns the full path if found, otherwise an empty string.
    """
    # --- 1. Registry probe ---------------------------------------------------
    _reg_roots = []
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for view in (winreg.KEY_READ | winreg.KEY_WOW64_32KEY,
                         winreg.KEY_READ | winreg.KEY_WOW64_64KEY):
                try:
                    key = winreg.OpenKey(
                        hive,
                        r"SOFTWARE\Microsoft\Windows Kits\Installed Roots",
                        access=view)
                    for value_name in ("KitsRoot10", "KitsRoot81", "KitsRoot"):
                        try:
                            val, _ = winreg.QueryValueEx(key, value_name)
                            _reg_roots.append(val)
                        except OSError:
                            pass
                    winreg.CloseKey(key)
                except OSError:
                    pass
    except Exception:
        pass

    # --- 2. Candidate folder list --------------------------------------------
    _prog86  = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    _prog64  = os.environ.get("ProgramFiles",      r"C:\Program Files")
    _adk_rel = r"Assessment and Deployment Kit\Deployment Tools"
    _kit_ver = ("10", "11", "8.1")
    _archs   = ("amd64", "x86", "arm64")

    candidates = []
    # From registry roots first (most authoritative)
    for root in _reg_roots:
        for arch in _archs:
            candidates.append(
                os.path.join(root, _adk_rel, arch, "Oscdimg", "oscdimg.exe"))
    # Then well-known install locations
    for base in (_prog86, _prog64):
        for ver in _kit_ver:
            for arch in _archs:
                candidates.append(os.path.join(
                    base, "Windows Kits", ver,
                    _adk_rel, arch, "Oscdimg", "oscdimg.exe"))

    for path in candidates:
        if os.path.isfile(path):
            return path

    # --- 3. PATH fallback ----------------------------------------------------
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(directory, "oscdimg.exe")
        if os.path.isfile(p):
            return p

    return ""


OSCDIMG = _find_oscdimg()

# ── Colours ───────────────────────────────────────────────────────────────────
BG_DARK   = "#1a1a1a"
BG_PANEL  = "#0d2b0d"
BG_FRAME  = "#252525"
BTN_BG    = "#363636"
BTN_HOV   = "#484848"
BTN_ACT   = "#2a5a2a"
FG_TEXT   = "#e8e8e8"
FG_DIM    = "#888888"
FG_GREEN  = "#4db34d"
FG_RED    = "#e05050"
FG_YELLOW = "#e0c050"
FG_BLUE   = "#5090e0"
GROUP_FG  = "#aaaaaa"


# ── Admin ─────────────────────────────────────────────────────────────────────
def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ── Path helpers ──────────────────────────────────────────────────────────────
def _norm(path: str) -> str:
    return os.path.normpath(path) if path else ""

def _ask(parent, title, prompt, default=""):
    return simpledialog.askstring(title, prompt, parent=parent, initialvalue=default)

def _ask_file(parent, title, ftypes=None):
    p = filedialog.askopenfilename(title=title, parent=parent,
                                   filetypes=ftypes or [("All files","*.*")])
    return _norm(p)

def _ask_save(parent, title, ext=".txt", ftypes=None):
    p = filedialog.asksaveasfilename(title=title, parent=parent,
                                     defaultextension=ext,
                                     filetypes=ftypes or [("Text","*.txt"),("All","*.*")])
    return _norm(p)

def _ask_dir(parent, title):
    p = filedialog.askdirectory(title=title, parent=parent)
    return _norm(p)


# ── Parse dism /Get-ImageInfo output ─────────────────────────────────────────
def _parse_image_info(text: str) -> list:
    indexes, cur = [], {}
    for raw in text.splitlines():
        line = raw.strip()
        def val(): return line.split(":", 1)[1].strip()
        if line.startswith("Index :"):
            if cur: indexes.append(cur)
            cur = {"index": val()}
        elif line.startswith("Name :"):          cur["name"]    = val()
        elif line.startswith("Description :"):   cur["desc"]    = val()
        elif line.startswith("Size :"):          cur["size"]    = val()
        elif line.startswith("Architecture :"): cur["arch"]    = val()
        elif line.startswith("Version :"):       cur["version"] = val()
        elif line.startswith("Edition :"):       cur["edition"] = val()
    if cur: indexes.append(cur)
    return indexes


# ── List physical disks via wmic ──────────────────────────────────────────────
def _list_disks() -> list:
    disks = []
    try:
        r = subprocess.run(
            ["wmic", "diskdrive", "get",
             "Index,Model,Size,InterfaceType,MediaType", "/format:csv"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        lines = r.stdout.decode("utf-8", errors="replace").splitlines()
        headers = None
        for line in lines:
            parts = [p.strip() for p in line.split(",")]
            if not any(parts): continue
            if headers is None:
                headers = [h.lower() for h in parts]
                continue
            d = dict(zip(headers, parts))
            try:
                gb = f"{int(d.get('size','0'))/1024**3:.1f} GB"
            except Exception:
                gb = "?"
            disks.append({
                "index":  d.get("index","?"),
                "model":  d.get("model","Unknown"),
                "size":   gb,
                "itype":  d.get("interfacetype",""),
                "media":  d.get("mediatype",""),
            })
    except Exception:
        pass
    return disks


# ── Run a diskpart script ─────────────────────────────────────────────────────
def _run_diskpart(commands: list) -> tuple:
    """Write commands to a temp file, run diskpart, return (stdout, stderr)."""
    tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    tmp.write("\n".join(commands) + "\n")
    tmp.close()
    try:
        r = subprocess.run([DISKPART, "/s", tmp.name],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        return (r.stdout.decode("utf-8", errors="replace"),
                r.stderr.decode("utf-8", errors="replace"))
    finally:
        os.unlink(tmp.name)


# ── Get mounted images (synchronous) ──────────────────────────────────────────
def _get_mounted_images() -> dict:
    """Query DISM for mounted images. Returns {mountdir: (wimfile, index)} or empty dict on error."""
    mounts = {}
    try:
        r = subprocess.run([DISM_EXE, "/Get-MountedImageInfo"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=subprocess.CREATE_NO_WINDOW,
                          timeout=10)
        output = r.stdout.decode("utf-8", errors="replace")
        lines = output.splitlines()

        cur_mount = None
        for line in lines:
            line = line.strip()
            if line.startswith("Mount Dir :"):
                cur_mount = line.split(":", 1)[1].strip()
                if cur_mount not in mounts:
                    mounts[cur_mount] = {}
            elif cur_mount and line.startswith("Wim File :"):
                mounts[cur_mount]["wim"] = line.split(":", 1)[1].strip()
            elif cur_mount and line.startswith("Image Index :"):
                mounts[cur_mount]["index"] = line.split(":", 1)[1].strip()
    except Exception:
        pass
    return mounts


# ── Recent-file persistence ───────────────────────────────────────────────────
def _load_recent() -> dict:
    try:
        if os.path.exists(RECENT_FILE):
            with open(RECENT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"images": [], "mounts": []}

def _save_recent(data: dict):
    try:
        with open(RECENT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def _push_recent(data: dict, key: str, value: str, limit: int = 12):
    lst = data.setdefault(key, [])
    if value in lst:
        lst.remove(value)
    lst.insert(0, value)
    data[key] = lst[:limit]


# ── Smart index-picker dialog ─────────────────────────────────────────────────
class IndexPickerDialog(tk.Toplevel):
    """Query a WIM/ESD and let the user pick an index before mounting."""

    def __init__(self, master, img_path: str):
        super().__init__(master)
        self.title("Select Image Index")
        self.geometry("660x360")
        self.configure(bg=BG_DARK)
        self.resizable(True, True)
        self.grab_set()
        self.chosen_index = None    # set to index string on OK

        tk.Label(self, text=os.path.basename(img_path),
                 bg=BG_DARK, fg=FG_YELLOW,
                 font=("Segoe UI", 9, "italic")).pack(fill="x", padx=10, pady=(8,2))
        tk.Label(self, text="Choose an edition / index to mount:",
                 bg=BG_DARK, fg=FG_TEXT,
                 font=("Segoe UI", 10)).pack(fill="x", padx=10)

        # Treeview
        cols = ("Index","Edition / Name","Architecture","Size","Description")
        tf = tk.Frame(self, bg=BG_DARK)
        tf.pack(fill="both", expand=True, padx=10, pady=6)

        self.tv = ttk.Treeview(tf, columns=cols, show="headings",
                               style="D.Treeview", selectmode="browse")
        for col, w in zip(cols, (55, 240, 100, 90, 160)):
            self.tv.heading(col, text=col)
            self.tv.column(col, width=w, anchor="w", minwidth=40)
        vsb = ttk.Scrollbar(tf, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=vsb.set)
        self.tv.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tv.bind("<Double-1>", lambda _e: self._ok())

        self._status = tk.Label(self, text="Querying image…",
                                bg=BG_DARK, fg=FG_YELLOW,
                                font=("Segoe UI", 8))
        self._status.pack(fill="x", padx=10, pady=2)

        br = tk.Frame(self, bg=BG_DARK)
        br.pack(fill="x", padx=10, pady=(4,8))
        _mk_btn(br, "Mount Selected", self._ok).pack(side="right", padx=4)
        _mk_btn(br, "Cancel", self.destroy).pack(side="right", padx=4)

        threading.Thread(target=self._query, args=(img_path,), daemon=True).start()

    def _query(self, path):
        try:
            r = subprocess.run(
                [DISM_EXE, "/Get-ImageInfo", f"/ImageFile:{path}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW)
            out = r.stdout.decode("utf-8", errors="replace")
            indexes = _parse_image_info(out)
            self.after(0, lambda: self._populate(indexes, out))
        except Exception as e:
            self.after(0, lambda: self._status.configure(
                text=f"Error querying image: {e}", fg=FG_RED))

    def _populate(self, indexes, raw):
        if not indexes:
            self._status.configure(
                text="No indexes found. Is this a valid WIM/ESD?", fg=FG_RED)
            return
        for ix in indexes:
            size = ix.get("size","")
            try:
                bytes_val = int(size.replace(",","").split()[0])
                size = f"{bytes_val/1024**3:.2f} GB"
            except Exception:
                pass
            self.tv.insert("", "end", values=(
                ix.get("index",""),
                ix.get("name",""),
                ix.get("arch",""),
                size,
                ix.get("desc",""),
            ))
        kids = self.tv.get_children()
        if kids:
            self.tv.selection_set(kids[0])
            self.tv.focus(kids[0])
        self._status.configure(
            text=f"{len(indexes)} edition(s) found. Double-click or press Mount Selected.",
            fg=FG_GREEN)

    def _ok(self):
        sel = self.tv.selection()
        if not sel:
            messagebox.showwarning("No selection",
                                   "Please select an edition.", parent=self)
            return
        self.chosen_index = self.tv.item(sel[0], "values")[0]
        self.destroy()


# ── Disk-picker dialog ────────────────────────────────────────────────────────
class DiskPickerDialog(tk.Toplevel):
    """Show physical disks and let the user choose one for deployment."""

    def __init__(self, master, prompt="Select target disk"):
        super().__init__(master)
        self.title("Select Disk")
        self.geometry("620x300")
        self.configure(bg=BG_DARK)
        self.grab_set()
        self.chosen_disk = None     # disk index string

        tk.Label(self, text=prompt, bg=BG_DARK, fg=FG_TEXT,
                 font=("Segoe UI", 10)).pack(fill="x", padx=10, pady=8)

        cols = ("Disk #","Model","Size","Interface","Media")
        tf = tk.Frame(self, bg=BG_DARK)
        tf.pack(fill="both", expand=True, padx=10, pady=4)

        self.tv = ttk.Treeview(tf, columns=cols, show="headings",
                               style="D.Treeview", selectmode="browse")
        for col, w in zip(cols, (60, 260, 80, 90, 110)):
            self.tv.heading(col, text=col)
            self.tv.column(col, width=w, anchor="w")
        vsb = ttk.Scrollbar(tf, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=vsb.set)
        self.tv.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tv.bind("<Double-1>", lambda _e: self._ok())

        br = tk.Frame(self, bg=BG_DARK)
        br.pack(fill="x", padx=10, pady=(4,8))
        _mk_btn(br, "Select", self._ok).pack(side="right", padx=4)
        _mk_btn(br, "Refresh", self._load).pack(side="right", padx=4)
        _mk_btn(br, "Cancel", self.destroy).pack(side="right", padx=4)

        self._load()

    def _load(self):
        for row in self.tv.get_children():
            self.tv.delete(row)
        for d in _list_disks():
            self.tv.insert("", "end", values=(
                d["index"], d["model"], d["size"], d["itype"], d["media"]))

    def _ok(self):
        sel = self.tv.selection()
        if not sel:
            messagebox.showwarning("No selection","Select a disk.", parent=self)
            return
        self.chosen_disk = self.tv.item(sel[0], "values")[0]
        self.destroy()


# ── Output window (supports live streaming) ───────────────────────────────────
class OutputWindow(tk.Toplevel):
    def __init__(self, master, title: str, cmd_line: str, app=None):
        super().__init__(master)
        self.title(title)
        self.geometry("860x560")
        self.configure(bg=BG_DARK)
        self.focus_set()
        self._full_text = ""
        self._done = False
        self.app = app

        bar = tk.Frame(self, bg=BG_FRAME)
        bar.pack(fill="x")
        tk.Label(bar, text="Command:", bg=BG_FRAME, fg=FG_DIM,
                 font=("Segoe UI", 8)).pack(side="left", padx=8, pady=4)
        tk.Label(bar, text=cmd_line, bg=BG_FRAME, fg=FG_YELLOW,
                 font=("Consolas", 8), anchor="w",
                 wraplength=750).pack(side="left", fill="x", expand=True, pady=4)

        self._prog_var = tk.StringVar(value="Running…")
        tk.Label(bar, textvariable=self._prog_var, bg=BG_FRAME,
                 fg=FG_GREEN, font=("Segoe UI", 8)).pack(side="right", padx=8)

        self._st = scrolledtext.ScrolledText(
            self, bg="#141414", fg=FG_TEXT, font=("Consolas", 9),
            wrap="word", relief="flat", bd=0,
            selectbackground="#2a5a2a", insertbackground=FG_TEXT)
        self._st.pack(fill="both", expand=True, padx=4, pady=4)
        self._st.tag_configure("err",  foreground=FG_RED)
        self._st.tag_configure("warn", foreground=FG_YELLOW)
        self._st.tag_configure("ok",   foreground=FG_GREEN)
        self._st.tag_configure("prog", foreground=FG_BLUE)

        br = tk.Frame(self, bg=BG_DARK)
        br.pack(fill="x", padx=8, pady=(0,6))
        _mk_btn(br, "Copy to clipboard", self._copy).pack(side="left", padx=4)
        _mk_btn(br, "Save to file…",     self._save).pack(side="left", padx=4)
        self._close_btn = _mk_btn(br, "Close", self.destroy)
        self._close_btn.pack(side="right", padx=4)

    def append(self, text: str):
        """Called from worker thread via root.after() to stream output."""
        self._full_text += text
        self._st.configure(state="normal")
        lo = text.lower()
        if any(w in lo for w in ("error","failed","cannot","invalid","0x8")):
            tag = "err"
        elif "warning" in lo:
            tag = "warn"
        elif any(w in lo for w in ("successfully","complete","100.0%")):
            tag = "ok"
        elif re.search(r"\d+\.\d+%", text):
            tag = "prog"
        else:
            tag = ""
        self._st.insert("end", text, tag)
        self._st.see("end")
        self._st.configure(state="disabled")
        # Extract % for header label
        m = re.search(r"(\d+\.\d+)%", text)
        if m:
            self._prog_var.set(f"{m.group(1)}%")

    def mark_done(self):
        self._done = True
        self._prog_var.set("Done")
        self.title(self.title() + "  ✓")

        # Detect mount conflict error (0xc1420127)
        if "0xc1420127" in self._full_text.lower() or "already mounted for read/write" in self._full_text.lower():
            self._show_mount_recovery_options()
        # Detect unmount error (0xc1420117)
        elif "0xc1420117" in self._full_text.lower() or "could not be completely unmounted" in self._full_text.lower():
            self._show_unmount_recovery_options()

    def _copy(self):
        self.clipboard_clear()
        self.clipboard_append(self._full_text)

    def _save(self):
        p = filedialog.asksaveasfilename(parent=self, defaultextension=".txt",
                                         filetypes=[("Text","*.txt"),("All","*.*")])
        if p:
            with open(p, "w", encoding="utf-8") as f:
                f.write(self._full_text)

    def _show_mount_recovery_options(self):
        """Show recovery dialog when mount conflict is detected."""
        if not self.app:
            return
        msg = (
            "⚠️  MOUNT CONFLICT DETECTED\n\n"
            "Error 0xc1420127: The image is already mounted for read/write access.\n\n"
            "OPTIONS:\n"
            "  • YES  → Run cleanup and retry\n"
            "  • NO   → Show mounted images\n"
            "  • CANCEL → Close this dialog"
        )
        choice = messagebox.askyesnocancel("Mount Conflict Recovery", msg, parent=self)
        if choice:
            self.app.cleanup_mountpoints()
        elif choice is False:
            self.app.check_mount_status()

    def _show_unmount_recovery_options(self):
        """Show recovery dialog when unmount fails due to open files."""
        if not self.app:
            return
        msg = (
            "⚠️  UNMOUNT FAILED\n\n"
            "Error 0xc1420117: Files are still open in the mount directory.\n"
            "Applications may have handles to the mounted image.\n\n"
            "OPTIONS:\n"
            "  • YES  → Reload session (recover mount state)\n"
            "  • NO   → Show mounted images to investigate\n"
            "  • CANCEL → Close this dialog\n\n"
            "ACTION: Close any applications using files in the mount directory,\n"
            "then click YES to reload and retry unmounting."
        )
        choice = messagebox.askyesnocancel("Unmount Failed - Files Open", msg, parent=self)
        if choice:
            self.app.reload_session()
        elif choice is False:
            self.app.check_mount_status()


# ── Reusable button factory ───────────────────────────────────────────────────
def _mk_btn(parent, text, cmd, **kw):
    defaults = dict(bg=BTN_BG, fg=FG_TEXT, relief="flat",
                    font=("Segoe UI", 9), padx=14, pady=4,
                    activebackground=BTN_HOV, activeforeground=FG_TEXT,
                    cursor="hand2")
    defaults.update(kw)
    return tk.Button(parent, text=text, command=cmd, **defaults)


# ── Tooltip ───────────────────────────────────────────────────────────────────
class Tooltip:
    """
    Hover tooltip with three reliability fixes:
      1. Class-level singleton  — only one tip visible at a time; entering a new
         widget always destroys the previous one first.
      2. Show delay (450 ms)   — fast mouse passes don't flash a tooltip.
      3. Bounds check on Leave — ignores spurious Leave events that tkinter fires
         when the cursor moves from a Frame into one of its own child Labels.
    """
    _active  = None   # currently visible Tooltip instance
    _pending = None   # (after_id, widget) of a scheduled-but-not-yet-shown tip

    def __init__(self, widget, text):
        self._widget = widget
        self._text   = text
        self._tip    = None
        for w in (widget, *widget.winfo_children()):
            w.bind("<Enter>",       self._on_enter, add="+")
            w.bind("<Leave>",       self._on_leave, add="+")
            w.bind("<ButtonPress>", self._on_click,  add="+")

    # ── event handlers ────────────────────────────────────────────────────
    def _on_enter(self, _event):
        Tooltip._cancel_pending()
        if Tooltip._active is self:
            return                      # already showing for this widget
        if Tooltip._active:
            Tooltip._active._hide()     # destroy any other visible tip
        aid = self._widget.after(450, self._show)
        Tooltip._pending = (aid, self._widget)

    def _on_leave(self, event):
        # Ignore Leave events caused by moving into a child of the same button
        try:
            wx = self._widget.winfo_rootx()
            wy = self._widget.winfo_rooty()
            ww = self._widget.winfo_width()
            wh = self._widget.winfo_height()
            if wx <= event.x_root < wx + ww and wy <= event.y_root < wy + wh:
                return
        except Exception:
            pass
        Tooltip._cancel_pending()
        self._hide()

    def _on_click(self, _event):
        Tooltip._cancel_pending()
        self._hide()

    # ── show / hide ───────────────────────────────────────────────────────
    def _show(self):
        if Tooltip._active and Tooltip._active is not self:
            Tooltip._active._hide()
        try:
            w  = self._widget
            x  = w.winfo_rootx() + w.winfo_width() // 2 - 170
            y  = w.winfo_rooty() + w.winfo_height() + 6
            sw = w.winfo_screenwidth()
            x  = max(0, min(x, sw - 360))   # keep on screen
            tw = tk.Toplevel(w)
            tw.wm_overrideredirect(True)
            tw.wm_geometry(f"+{x}+{y}")
            tw.attributes("-topmost", True)
            tk.Label(tw, text=self._text,
                     bg="#1a2e1a", fg="#d8e8d8",
                     font=("Segoe UI", 8), relief="flat",
                     padx=9, pady=5, wraplength=340,
                     justify="left").pack()
            self._tip    = tw
            Tooltip._active = self
        except Exception:
            pass

    def _hide(self):
        if self._tip:
            try: self._tip.destroy()
            except Exception: pass
            self._tip = None
        if Tooltip._active is self:
            Tooltip._active = None

    # ── class helpers ─────────────────────────────────────────────────────
    @staticmethod
    def _cancel_pending():
        if Tooltip._pending:
            aid, widget = Tooltip._pending
            try: widget.after_cancel(aid)
            except Exception: pass
            Tooltip._pending = None


# ── Feature Manager dialog ────────────────────────────────────────────────────
class FeatureManagerDialog(tk.Toplevel):
    """
    Lists all Windows features in the mounted image.
    Supports text-filter, state-filter, multi-select (Ctrl/Shift click),
    and batch Enable / Disable with a single click.
    """

    def __init__(self, master, mount_point: str, app=None, initial_filter="All"):
        super().__init__(master)
        self.title("Feature Manager")
        self.geometry("780x600")
        self.minsize(600, 400)
        self.configure(bg=BG_DARK)
        self.resizable(True, True)
        self.focus_set()
        self._mp   = mount_point
        self._app  = app
        self._all  = []          # [{"name": str, "state": str}]
        self._filter_var = tk.StringVar()
        self._state_var  = tk.StringVar(value=initial_filter)
        self._build()
        threading.Thread(target=self._query, daemon=True).start()

    # ── UI construction ───────────────────────────────────────────────────
    def _build(self):
        tk.Label(self, text=f"Image mount:  {self._mp}",
                 bg=BG_DARK, fg=FG_YELLOW,
                 font=("Segoe UI", 8, "italic")).pack(fill="x", padx=10, pady=(8, 2))

        # Filter bar
        fb = tk.Frame(self, bg=BG_DARK)
        fb.pack(fill="x", padx=10, pady=(0, 4))
        tk.Label(fb, text="Search:", bg=BG_DARK, fg=FG_DIM,
                 font=("Segoe UI", 8)).pack(side="left")
        tk.Entry(fb, textvariable=self._filter_var,
                 bg=BG_FRAME, fg=FG_TEXT, font=("Segoe UI", 9),
                 relief="flat", insertbackground=FG_TEXT,
                 width=30).pack(side="left", padx=4)
        self._filter_var.trace_add("write", lambda *_: self._apply_filter())

        tk.Label(fb, text="State:", bg=BG_DARK, fg=FG_DIM,
                 font=("Segoe UI", 8)).pack(side="left", padx=(12, 2))
        state_cb = ttk.Combobox(fb, textvariable=self._state_var,
                                values=["All", "Enabled", "Disabled"],
                                state="readonly", width=10, font=("Segoe UI", 8))
        state_cb.pack(side="left")
        state_cb.bind("<<ComboboxSelected>>", lambda _: self._apply_filter())

        _mk_btn(fb, "Refresh", self._refresh,
                font=("Segoe UI", 8), padx=8, pady=2).pack(side="right")

        # Treeview
        cols = ("Feature Name", "State")
        tf = tk.Frame(self, bg=BG_DARK)
        tf.pack(fill="both", expand=True, padx=10, pady=2)

        self.tv = ttk.Treeview(tf, columns=cols, show="headings",
                               style="D.Treeview", selectmode="extended")
        self.tv.heading("Feature Name", text="Feature Name",
                        command=lambda: self._sort("name"))
        self.tv.heading("State", text="State",
                        command=lambda: self._sort("state"))
        self.tv.column("Feature Name", width=560, anchor="w", minwidth=200)
        self.tv.column("State",        width=110, anchor="center", minwidth=80)
        self.tv.tag_configure("enabled",  foreground=FG_GREEN)
        self.tv.tag_configure("disabled", foreground=FG_DIM)
        self.tv.tag_configure("pending",  foreground=FG_YELLOW)

        vsb = ttk.Scrollbar(tf, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=vsb.set)
        self.tv.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # Bind Ctrl+A
        self.tv.bind("<Control-a>",
                     lambda _: self.tv.selection_set(self.tv.get_children()))

        # Status
        self._status = tk.Label(self, text="Querying features…",
                                bg=BG_DARK, fg=FG_YELLOW, font=("Segoe UI", 8))
        self._status.pack(fill="x", padx=10, pady=2)

        # Buttons
        br = tk.Frame(self, bg=BG_DARK)
        br.pack(fill="x", padx=10, pady=(4, 10))
        _mk_btn(br, "✅  Enable Selected",
                self._enable_selected).pack(side="left", padx=4)
        _mk_btn(br, "🚫  Disable Selected",
                self._disable_selected).pack(side="left", padx=4)
        _mk_btn(br, "Select All (Ctrl+A)",
                lambda: self.tv.selection_set(
                    self.tv.get_children())).pack(side="left", padx=4)
        _mk_btn(br, "Close", self.destroy).pack(side="right", padx=4)

    # ── Data loading ──────────────────────────────────────────────────────
    def _query(self):
        try:
            r = subprocess.run(
                [DISM_EXE, f"/Image:{self._mp}", "/Get-Features"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW, timeout=90)
            features = self._parse(r.stdout.decode("utf-8", errors="replace"))
            self.after(0, lambda: self._populate(features))
        except Exception as exc:
            self.after(0, lambda: self._status.configure(
                text=f"Error querying features: {exc}", fg=FG_RED))

    @staticmethod
    def _parse(text: str) -> list:
        features, cur = [], {}
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("Feature Name :"):
                if cur:
                    features.append(cur)
                cur = {"name": line.split(":", 1)[1].strip()}
            elif line.startswith("State :") and cur:
                cur["state"] = line.split(":", 1)[1].strip()
        if cur:
            features.append(cur)
        return features

    def _populate(self, features):
        self._all = features
        self._sort_key  = "name"
        self._sort_asc  = True
        self._apply_filter()
        enabled = sum(1 for f in features if "Enabled" in f.get("state", ""))
        self._status.configure(
            text=(f"{len(features)} features total  —  "
                  f"{enabled} enabled  /  {len(features) - enabled} disabled  "
                  "  (Ctrl+A = select all,  Ctrl+Click = multi-select)"),
            fg=FG_GREEN)

    def _apply_filter(self):
        query  = self._filter_var.get().lower()
        sf     = self._state_var.get()
        for iid in self.tv.get_children():
            self.tv.delete(iid)
        for f in self._all:
            name  = f.get("name", "")
            state = f.get("state", "")
            if query and query not in name.lower():
                continue
            if sf == "Enabled"  and "Enabled"  not in state: continue
            if sf == "Disabled" and "Disabled" not in state: continue
            tag = ("enabled"  if "Enabled" in state  else
                   "pending"  if "Pending" in state  else "disabled")
            self.tv.insert("", "end", values=(name, state), tags=(tag,))

    def _sort(self, key):
        if not self._all:
            return
        asc = True
        if hasattr(self, "_sort_key") and self._sort_key == key:
            asc = not getattr(self, "_sort_asc", True)
        self._sort_key = key
        self._sort_asc = asc
        self._all.sort(key=lambda f: f.get(key, "").lower(), reverse=not asc)
        self._apply_filter()

    def _refresh(self):
        self._status.configure(text="Refreshing…", fg=FG_YELLOW)
        self._all = []
        for iid in self.tv.get_children():
            self.tv.delete(iid)
        threading.Thread(target=self._query, daemon=True).start()

    # ── Actions ───────────────────────────────────────────────────────────
    def _selected_names(self) -> list:
        return [self.tv.item(iid, "values")[0] for iid in self.tv.selection()]

    def _enable_selected(self):
        names = self._selected_names()
        if not names:
            messagebox.showwarning("Nothing selected",
                                   "Ctrl+Click or drag to select features, then click Enable.",
                                   parent=self)
            return
        enable_deps = messagebox.askyesno(
            "Enable parent features?",
            f"Enable {len(names)} feature(s).\n\n"
            "Also automatically enable all required parent features?\n"
            "(Recommended — prevents missing-dependency errors)",
            parent=self)
        self.destroy()
        if self._app:
            self._app._batch_enable_features(names, enable_deps)

    def _disable_selected(self):
        names = self._selected_names()
        if not names:
            messagebox.showwarning("Nothing selected",
                                   "Ctrl+Click or drag to select features, then click Disable.",
                                   parent=self)
            return
        if not messagebox.askyesno("Confirm disable",
                f"Disable {len(names)} feature(s)?\n\n"
                "Disabling features may affect dependent components.",
                parent=self):
            return
        self.destroy()
        if self._app:
            self._app._batch_disable_features(names)


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN APPLICATION
# ═══════════════════════════════════════════════════════════════════════════════
class DismTool:

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("DISM Tool  –  Windows Image Servicing & Deployment")
        self.root.geometry("1480x940")
        self.root.minsize(1100, 720)
        self.root.configure(bg=BG_DARK)

        self.mount_point   = tk.StringVar()
        self.image_file    = tk.StringVar()
        self.image_index   = tk.StringVar(value="1")
        self.image_name    = tk.StringVar(value="—")
        self.image_version = tk.StringVar(value="—")
        self.image_desc    = tk.StringVar(value="—")
        self.status_var    = tk.StringVar(value="Ready")
        self._admin        = _is_admin()
        self._cmd_history  = []
        self._recent       = _load_recent()

        self._apply_treeview_style()
        self._build_ui()
        self._bind_shortcuts()

        if not self._admin:
            self.root.after(250, self._warn_admin)
        if not os.path.isfile(DISM_EXE):
            self.root.after(350, self._warn_dism)
        self.root.after(900, self._auto_detect_mounts)

    def _apply_treeview_style(self):
        s = ttk.Style()
        s.theme_use("default")
        s.configure("D.Treeview", background=BG_DARK, foreground=FG_TEXT,
                    fieldbackground=BG_DARK, borderwidth=0, rowheight=22)
        s.map("D.Treeview", background=[("selected","#1e4a1e")])
        # Main top-level notebook — bold, prominent
        s.configure("TNotebook", background=BG_DARK, borderwidth=0)
        s.configure("TNotebook.Tab", background="#1a2e1a", foreground="#6a9a6a",
                    padding=[20, 7], font=("Segoe UI", 10, "bold"))
        s.map("TNotebook.Tab",
              background=[("selected", "#2a4a2a")],
              foreground=[("selected", FG_GREEN)])
        # Sub-notebook — compact, sits inside each main tab
        s.configure("Sub.TNotebook", background=BG_FRAME, borderwidth=0)
        s.configure("Sub.TNotebook.Tab", background="#252525", foreground=FG_DIM,
                    padding=[12, 4], font=("Segoe UI", 8))
        s.map("Sub.TNotebook.Tab",
              background=[("selected", "#363636")],
              foreground=[("selected", FG_TEXT)])
        s.configure("TScrollbar", background=BTN_BG, troughcolor=BG_DARK,
                    borderwidth=0, arrowcolor=FG_DIM)
        s.configure("TCombobox", fieldbackground=BG_FRAME, background=BG_FRAME,
                    foreground=FG_TEXT, selectbackground="#1e4a1e")

    # ── Warnings ──────────────────────────────────────────────────────────────
    def _warn_admin(self):
        messagebox.showwarning("Not Administrator",
            "Most DISM operations require Administrator privileges.\n\n"
            "Right-click the program → Run as administrator.",
            parent=self.root)

    def _warn_dism(self):
        messagebox.showerror("DISM not found",
            f"DISM not found at:\n  {DISM_EXE}\n\n"
            "This tool requires Windows with DISM installed.",
            parent=self.root)

    # ── Top-level layout ──────────────────────────────────────────────────────
    def _build_ui(self):
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

        self._build_left_panel()
        self._build_centre()
        self._build_right_panel()
        self._build_status_bar()

    # ── Left panel ────────────────────────────────────────────────────────────
    def _build_left_panel(self):
        left = tk.Frame(self.root, bg=BG_PANEL, width=205)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_propagate(False)

        hdr = tk.Frame(left, bg=BG_PANEL)
        hdr.pack(fill="x")
        tk.Label(hdr, text="PROJECT", bg=BG_PANEL, fg=FG_DIM,
                 font=("Segoe UI",7,"bold")).pack(side="left", padx=10, pady=6)
        tk.Label(hdr, text="IMAGE", bg=BG_PANEL, fg=FG_DIM,
                 font=("Segoe UI",7,"bold")).pack(side="right", padx=10)
        tk.Frame(left, bg="#2e2e2e", height=1).pack(fill="x")

        adm_clr = FG_GREEN if self._admin else FG_RED
        adm_txt = "● Administrator" if self._admin else "● Not Administrator"
        tk.Label(left, text=adm_txt, bg=BG_PANEL, fg=adm_clr,
                 font=("Segoe UI",7,"bold")).pack(anchor="w", padx=10, pady=(4,0))

        info = tk.Frame(left, bg=BG_PANEL)
        info.pack(fill="x", padx=8, pady=4)

        def irow(label, var, edit=False):
            f = tk.Frame(info, bg=BG_PANEL)
            f.pack(fill="x", pady=1)
            tk.Label(f, text=label, bg=BG_PANEL, fg=FG_DIM,
                     font=("Segoe UI",7), width=13, anchor="w").pack(side="left")
            if edit:
                tk.Entry(f, textvariable=var, bg="#1e3a1e", fg=FG_TEXT,
                         font=("Segoe UI",7), relief="flat",
                         insertbackground=FG_TEXT, width=13
                         ).pack(side="left", fill="x", expand=True)
            else:
                tk.Label(f, textvariable=var, bg=BG_PANEL, fg=FG_TEXT,
                         font=("Segoe UI",7), anchor="w",
                         wraplength=108).pack(side="left")

        irow("Image index:", self.image_index, edit=True)
        irow("Mount point:", self.mount_point, edit=True)
        tk.Frame(left, bg="#2e2e2e", height=1).pack(fill="x", pady=3)
        irow("Version:",     self.image_version)
        irow("Name:",        self.image_name)
        tk.Label(info, text="Description:", bg=BG_PANEL, fg=FG_DIM,
                 font=("Segoe UI",7), anchor="w").pack(fill="x", pady=(3,0))
        tk.Label(info, textvariable=self.image_desc, bg=BG_PANEL, fg=FG_TEXT,
                 font=("Segoe UI",7), anchor="nw", wraplength=180,
                 justify="left").pack(fill="x")

        def _browse_mp():
            d = _ask_dir(self.root, "Select mount point directory")
            if d: self.mount_point.set(d)

        _mk_btn(left, "Browse mount point…", _browse_mp,
                font=("Segoe UI",7)
                ).pack(fill="x", padx=8, pady=(5,0))

        tk.Frame(left, bg=BG_PANEL).pack(fill="both", expand=True)
        tk.Frame(left, bg="#2e2e2e", height=1).pack(fill="x")

        tk.Label(left, text="Image Tasks", bg=BG_PANEL, fg=FG_TEXT,
                 font=("Segoe UI",8,"bold"), anchor="w"
                 ).pack(fill="x", padx=10, pady=(6,2))

        def tlink(label, cmd):
            f = tk.Frame(left, bg=BG_PANEL, cursor="hand2")
            f.pack(fill="x", padx=8, pady=1)
            ico = tk.Label(f, text="⊙", bg=BG_PANEL, fg=FG_GREEN, font=("Segoe UI",10))
            ico.pack(side="left")
            lbl = tk.Label(f, text=label, bg=BG_PANEL, fg=FG_GREEN,
                           font=("Segoe UI",8), cursor="hand2", anchor="w")
            lbl.pack(side="left")
            for w in (f, ico, lbl):
                w.bind("<Button-1>", lambda e, c=cmd: c())

        tlink("View image properties",    self.view_image_properties)
        tlink("List all mounted images",  self.list_mounted_images)
        tlink("Unmount image (discard)",  self.unmount_image_discard)
        tlink("Open DISM log",            self.open_dism_log)
        tk.Frame(left, bg=BG_PANEL, height=6).pack()

    # ── Right panel ───────────────────────────────────────────────────────────
    def _build_right_panel(self):
        right = tk.Frame(self.root, bg=BG_DARK, width=235)
        right.grid(row=0, column=2, sticky="nsew")
        right.grid_propagate(False)

        tv = ttk.Treeview(right, style="D.Treeview", show="tree")
        tv.pack(fill="both", expand=True, padx=4, pady=4)
        proj = tv.insert("","end", text='Project: "Windows 11 Customized"', open=True)
        adk  = tv.insert(proj,"end", text="ADK Deployment Tools", open=True)
        for t in ("Deployment Tools (x86)","Deployment Tools (AMD64)",
                  "Deployment Tools (ARM)","Deployment Tools (ARM64)"):
            tv.insert(adk,"end", text=t)
        tv.insert(proj,"end", text="Mount point")
        tv.insert(proj,"end", text="Unattended answer files")
        tv.insert(proj,"end", text="Scratch directory")
        tv.insert(proj,"end", text="Project reports")

    # ── Status bar ────────────────────────────────────────────────────────────
    def _build_status_bar(self):
        bar = tk.Frame(self.root, bg="#111111")
        bar.grid(row=1, column=0, columnspan=3, sticky="ew")
        tk.Label(bar, textvariable=self.status_var, bg="#111111",
                 fg=FG_DIM, font=("Segoe UI",8), anchor="w"
                 ).pack(side="left", padx=8, pady=2)
        self._progress = ttk.Progressbar(bar, mode="determinate",
                                          length=130, maximum=100)
        self._progress.pack(side="right", padx=8, pady=3)
        self._mount_dot = tk.Label(bar, text="●", bg="#111111",
                                    fg=FG_DIM, font=("Segoe UI", 10))
        self._mount_dot.pack(side="right", padx=(0,2), pady=2)
        tk.Label(bar, text="Mount:", bg="#111111", fg=FG_DIM,
                 font=("Segoe UI", 8)).pack(side="right", padx=(8,0), pady=2)

    # ── Centre: 4 main tabs each with sub-tabs ────────────────────────────────
    def _build_centre(self):
        outer = tk.Frame(self.root, bg=BG_DARK)
        outer.grid(row=0, column=1, sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        tk.Label(outer, text="Windows Image Servicing & Deployment",
                 bg=BG_DARK, fg=FG_TEXT, font=("Segoe UI", 13),
                 anchor="w").grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 4))

        nb = ttk.Notebook(outer)
        nb.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))

        self._build_tab_image(nb)
        self._build_tab_deploy(nb)
        self._build_tab_storage(nb)
        self._build_tab_diagnostics(nb)

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers: sub-notebook factory + scrollable sub-tab
    # ─────────────────────────────────────────────────────────────────────────
    def _sub_nb(self, parent):
        """Second-level notebook with Sub.TNotebook style."""
        nb = ttk.Notebook(parent, style="Sub.TNotebook")
        nb.pack(fill="both", expand=True, padx=2, pady=2)
        return nb

    def _scrollable(self, nb, label):
        """Add a scrollable content frame as a new tab in *nb*."""
        outer = tk.Frame(nb, bg=BG_DARK)
        nb.add(outer, text=f"  {label}  ")
        canvas = tk.Canvas(outer, bg=BG_DARK, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        content = tk.Frame(canvas, bg=BG_DARK)
        win_id = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>",
                     lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(win_id, width=e.width))
        # Bind mousewheel only while the mouse is over this canvas
        def _on_enter(_):
            canvas.bind_all("<MouseWheel>",
                            lambda e: canvas.yview_scroll(-1*(e.delta//120), "units"))
        def _on_leave(_):
            canvas.unbind_all("<MouseWheel>")
        canvas.bind("<Enter>", _on_enter)
        canvas.bind("<Leave>", _on_leave)
        return content

    # ─────────────────────────────────────────────────────────────────────────
    # Group + button builders
    # ─────────────────────────────────────────────────────────────────────────
    _ICONS = {
        "Mount":"📁","Apply":"📁","Commit":"💾","Save":"💾",
        "Unmount":"⏏","Remove":"🗑","Disable":"🗑","Delete":"🗑",
        "Get":"📋","View":"📋","List":"📋","Show":"📋",
        "Add":"➕","Enable":"➕","Install":"➕",
        "Capture":"📷","Reload":"🔄","Switch":"🔀",
        "Perform":"🔧","Cleanup":"🔧","Repair":"🔧","Run":"🔧",
        "Set":"⚙","Create":"⚙","Format":"⚙","Partition":"⚙",
        "Copy":"📤","Upload":"📤","Transfer":"📤","Export":"📤",
        "Convert":"🔁","Split":"✂","Open":"📖","Analyse":"🔍",
        "Check":"🔍","Sysprep":"🛠","Generate":"📝","Inject":"📝",
    }

    def _icon(self, label):
        return self._ICONS.get(label.split()[0], "▶")

    def _group(self, parent, title, buttons, cols, row, col,
               colspan=1, padx=3, pady=3):
        frm = tk.LabelFrame(parent, text=f"  {title}  ",
                            bg=BG_DARK, fg=GROUP_FG,
                            font=("Segoe UI",8), bd=1, relief="groove",
                            labelanchor="nw", padx=5, pady=5)
        frm.grid(row=row, column=col, columnspan=colspan,
                 sticky="nsew", padx=padx, pady=pady)
        for i, (label, cmd, tip) in enumerate(buttons):
            r, c = divmod(i, cols)
            b = self._btn(frm, label, cmd, tip)
            b.grid(row=r, column=c, padx=3, pady=3, sticky="nsew")
            frm.rowconfigure(r, weight=1)
        for c in range(cols):
            frm.columnconfigure(c, weight=1)

    def _btn(self, parent, label, cmd, tip=""):
        f = tk.Frame(parent, bg=BTN_BG, cursor="hand2", relief="flat")
        ico = tk.Label(f, text=self._icon(label), bg=BTN_BG, fg=FG_TEXT,
                       font=("Segoe UI",14))
        ico.pack(side="top", pady=(6,0))
        txt = tk.Label(f, text=label, bg=BTN_BG, fg=FG_TEXT,
                       font=("Segoe UI",8), wraplength=135,
                       justify="center", padx=4, pady=5)
        txt.pack(side="top")
        ws = (f, ico, txt)
        def _e(_): [w.configure(bg=BTN_HOV) for w in ws]
        def _l(_): [w.configure(bg=BTN_BG)  for w in ws]
        def _c(_):
            [w.configure(bg=BTN_ACT) for w in ws]
            self.root.after(130, lambda: [w.configure(bg=BTN_BG) for w in ws])
            cmd()
        for w in ws:
            w.bind("<Enter>",    _e)
            w.bind("<Leave>",    _l)
            w.bind("<Button-1>", _c)
        if tip:
            Tooltip(f, tip)
        return f

    # ═════════════════════════════════════════════════════════════════════════
    # MAIN TAB 1 — ⚙ Image Servicing
    # ═════════════════════════════════════════════════════════════════════════
    def _build_tab_image(self, nb):
        outer = tk.Frame(nb, bg=BG_DARK)
        nb.add(outer, text="  ⚙  Image Servicing  ")
        sub = self._sub_nb(outer)
        self._build_sub_mount(sub)
        self._build_sub_packages(sub)
        self._build_sub_features(sub)
        self._build_sub_appx_caps(sub)
        self._build_sub_drivers(sub)
        self._build_sub_winpe_sys(sub)

    def _build_sub_mount(self, nb):
        p = self._scrollable(nb, "🖼 Mount & Capture")
        r = tk.Frame(p, bg=BG_DARK)
        r.pack(fill="x", padx=8, pady=6)
        r.columnconfigure(0, weight=1)
        self._group(r, "Image operations", [
            ("Mount image...",                     self.mount_image,
             "Pick a WIM/ESD — shows all editions so you can choose the right one"),
            ("Commit current changes",             self.commit_image,
             "Save edits to the mounted image without unmounting"),
            ("Commit and unmount image",           self.commit_unmount_image,
             "Save and close the mounted image"),
            ("Unmount image discarding changes",   self.unmount_image_discard,
             "Close the image and throw away all changes"),
            ("Reload servicing session",           self.reload_session,
             "Recover an orphaned mount point after a crash"),
            ("Switch image indexes...",            self.switch_image_index,
             "Browse all editions inside a WIM/ESD file"),
            ("Apply image...",                     self.apply_image,
             "Expand a WIM/ESD to a folder or volume"),
            ("Capture image...",                   self.capture_image,
             "Create a new WIM from a folder"),
            ("Remove volume images...",            self.remove_volume_image,
             "Delete one edition from a multi-index WIM"),
            ("Save complete image information...", self.save_image_info,
             "Get metadata for all editions in an image file"),
            ("Check WIM integrity...",             self.check_wim_integrity,
             "Verify the checksum of every file in a WIM/ESD"),
        ], cols=3, row=0, col=0)

    def _build_sub_packages(self, nb):
        p = self._scrollable(nb, "📦 Packages")
        r = tk.Frame(p, bg=BG_DARK)
        r.pack(fill="x", padx=8, pady=6)
        r.columnconfigure(0, weight=1)
        self._group(r, "Package operations", [
            ("Add package...",
             self.add_package,
             "Install a .cab or .msu update into the mounted image"),
            ("Batch install packages...",
             self.batch_add_packages,
             "Queue multiple .cab/.msu files and install them all in one shot"),
            ("Get package information...",
             self.get_package_info,
             "Query details for a package already in the image"),
            ("Save installed package information...",
             self.save_package_info,
             "List every package installed in the mounted image"),
            ("Remove package...",
             self.remove_package,
             "Uninstall a package from the mounted image"),
            ("Perform component store maintenance and cleanup...",
             self.cleanup_image,
             "Reduce image size and fix corruption (StartComponentCleanup / RevertPendingActions)"),
        ], cols=3, row=0, col=0)

    def _build_sub_features(self, nb):
        p = self._scrollable(nb, "✅ Features")
        r = tk.Frame(p, bg=BG_DARK)
        r.pack(fill="x", padx=8, pady=6)
        r.columnconfigure(0, weight=1)
        self._group(r, "Feature operations", [
            ("Manage all features...",      self.manage_features,
             "Opens the Feature Manager — search, filter, enable or disable any selection"),
            ("Enable features...",          self.enable_feature,
             "Feature Manager pre-filtered to Disabled features"),
            ("Disable features...",         self.disable_feature,
             "Feature Manager pre-filtered to Enabled features"),
            ("Get feature information...",  self.get_feature_info,
             "Run /Get-Features on the mounted image and show raw output"),
            ("Save feature information...", self.save_feature_info,
             "Export the full feature list to a text file"),
        ], cols=3, row=0, col=0)

    def _build_sub_appx_caps(self, nb):
        p = self._scrollable(nb, "📱 AppX & Capabilities")
        r = tk.Frame(p, bg=BG_DARK)
        r.pack(fill="x", padx=8, pady=6)
        r.columnconfigure(0, weight=1); r.columnconfigure(1, weight=1)
        self._group(r, "AppX package operations", [
            ("Add AppX package...",
             self.add_appx_package,
             "Provision an .appx/.msix so it installs for all new users"),
            ("Bulk remove AppX packages...",
             self.bulk_remove_appx,
             "Interactive checklist — query all provisioned apps and remove any selection"),
            ("Get app information...",
             self.get_appx_info,
             "List all provisioned AppX packages"),
            ("Save installed AppX package information...",
             self.save_appx_info,
             "Export the provisioned AppX list to a file"),
            ("Remove AppX package...",
             self.remove_appx_package,
             "De-provision a single AppX package from the image"),
        ], cols=2, row=0, col=0)
        self._group(r, "Capability operations", [
            ("Add capability...",              self.add_capability,
             "Add a Features-on-Demand capability"),
            ("Remove capability...",           self.remove_capability,
             "Remove a capability from the image"),
            ("Get capability information...",  self.get_capability_info,
             "Query details for a capability"),
            ("Save capability information...", self.save_capability_info,
             "List all capabilities and their state"),
        ], cols=2, row=0, col=1)

    def _build_sub_drivers(self, nb):
        p = self._scrollable(nb, "🔌 Drivers")
        r = tk.Frame(p, bg=BG_DARK)
        r.pack(fill="x", padx=8, pady=6)
        r.columnconfigure(0, weight=1)
        self._group(r, "Driver operations", [
            ("Add driver package...",              self.add_driver,
             "Inject a third-party driver (.inf) or folder of drivers into the image"),
            ("Get driver information...",          self.get_driver_info,
             "Query details for an injected driver"),
            ("Save installed driver information...", self.save_driver_info,
             "List all third-party drivers in the image"),
            ("Remove driver...",                   self.remove_driver,
             "Remove an injected driver from the image"),
        ], cols=4, row=0, col=0)

    def _build_sub_winpe_sys(self, nb):
        p = self._scrollable(nb, "⚙ WinPE & System")
        r1 = tk.Frame(p, bg=BG_DARK)
        r1.pack(fill="x", padx=8, pady=6)
        r1.columnconfigure(0, weight=1)
        self._group(r1, "Windows PE operations", [
            ("Get configuration",     self.pe_get_config,
             "Show current WinPE settings (target path, scratch space)"),
            ("Save configuration...", self.pe_save_config,
             "Export WinPE settings to a text file"),
            ("Set target path...",    self.pe_set_target_path,
             "Set the WinPE target drive letter (e.g. X:\\)"),
            ("Set scratch space...",  self.pe_set_scratch_space,
             "Set WinPE RAM disk scratch space: 32/64/128/256/512 MB"),
        ], cols=4, row=0, col=0)
        r2 = tk.Frame(p, bg=BG_DARK)
        r2.pack(fill="x", padx=8, pady=(4, 12))
        r2.columnconfigure(0, weight=2); r2.columnconfigure(1, weight=1)
        self._group(r2, "Registry hive operations", [
            ("Load registry hive...",   self.load_reg_hive,
             "Load an offline registry hive (SYSTEM, SOFTWARE…) into HKLM for editing in regedit"),
            ("Unload registry hive...", self.unload_reg_hive,
             "Unload a previously loaded offline hive — do this before unmounting"),
        ], cols=2, row=0, col=0)
        self._group(r2, "ISO creation", [
            ("Create bootable ISO...", self.create_iso,
             "Package a Windows folder into a bootable ISO using oscdimg (requires ADK)"),
        ], cols=1, row=0, col=1)

    # ═════════════════════════════════════════════════════════════════════════
    # MAIN TAB 2 — 🚀 Deploy
    # ═════════════════════════════════════════════════════════════════════════
    def _build_tab_deploy(self, nb):
        outer = tk.Frame(nb, bg=BG_DARK)
        nb.add(outer, text="  🚀  Deploy  ")
        sub = self._sub_nb(outer)
        self._build_sub_usb(sub)
        self._build_sub_baremetal(sub)
        self._build_sub_network_wds(sub)
        self._build_sub_sysprep(sub)

    def _build_sub_usb(self, nb):
        p = self._scrollable(nb, "💾 USB Boot")
        r = tk.Frame(p, bg=BG_DARK)
        r.pack(fill="x", padx=8, pady=6)
        r.columnconfigure(0, weight=1)
        self._group(r, "USB Deployment", [
            ("Create bootable USB (UEFI + BIOS)...",
             self.usb_create_bootable,
             "Format USB, create UEFI+BIOS partitions, apply WIM, install bootloader"),
            ("Create bootable USB (UEFI / FAT32)...",
             self.usb_create_uefi,
             "Format USB as FAT32, copy WinPE/Windows files — UEFI systems only"),
            ("Apply WIM to USB drive...",
             self.usb_apply_wim,
             "Apply a WIM image directly to an existing USB partition"),
            ("Format & wipe USB drive...",
             self.usb_format,
             "Erase and reformat a USB drive using diskpart"),
        ], cols=2, row=0, col=0)

    def _build_sub_baremetal(self, nb):
        p = self._scrollable(nb, "🖥 Bare Metal")
        r = tk.Frame(p, bg=BG_DARK)
        r.pack(fill="x", padx=8, pady=6)
        r.columnconfigure(0, weight=1)
        self._group(r, "Apply to Local Disk (Bare Metal)", [
            ("Apply image to disk partition...",
             self.deploy_apply_to_disk,
             "Partition disk, apply WIM, install bootloader — full bare-metal deploy"),
            ("Partition disk (GPT / UEFI)...",
             self.deploy_partition_gpt,
             "Create EFI + MSR + Windows partitions on a disk (GPT)"),
            ("Partition disk (MBR / BIOS)...",
             self.deploy_partition_mbr,
             "Create a single active NTFS partition on a disk (MBR)"),
            ("Install bootloader (bcdboot)...",
             self.deploy_bcdboot,
             "Write boot files to a partition using bcdboot"),
        ], cols=2, row=0, col=0)

    def _build_sub_network_wds(self, nb):
        p = self._scrollable(nb, "🌐 Network & WDS")
        r = tk.Frame(p, bg=BG_DARK)
        r.pack(fill="x", padx=8, pady=6)
        r.columnconfigure(0, weight=1); r.columnconfigure(1, weight=1)
        self._group(r, "Network / LAN Deployment", [
            ("Copy WIM to network share (LAN)...",
             self.net_copy_to_share,
             "Robocopy the WIM/ESD to a UNC path (\\\\server\\share\\...)"),
            ("Copy WIM to NAS...",
             self.net_copy_to_nas,
             "Copy image files to a NAS — prompts for UNC path and optional credentials"),
            ("Apply image from network share...",
             self.net_apply_from_share,
             "Run DISM /Apply-Image directly from a UNC network path"),
            ("Map network drive...",
             self.net_map_drive,
             "Map a UNC share to a drive letter (net use)"),
            ("Disconnect network drive...",
             self.net_disconnect_drive,
             "Remove a mapped network drive (net use /delete)"),
        ], cols=2, row=0, col=0)
        self._group(r, "WDS / PXE Deployment", [
            ("Upload image to WDS server...",
             self.wds_upload_image,
             "Add a WIM to a Windows Deployment Services server (wdsutil)"),
            ("List WDS images...",
             self.wds_list_images,
             "Show all images registered on a WDS server"),
            ("Remove image from WDS...",
             self.wds_remove_image,
             "Delete an image from a WDS image group"),
            ("Generate PXE boot files...",
             self.wds_gen_pxe,
             "Create a WinPE boot WIM and copy to TFTP root for PXE booting"),
            ("Show WDS server info...",
             self.wds_server_info,
             "Query the WDS server configuration"),
        ], cols=2, row=0, col=1)

    def _build_sub_sysprep(self, nb):
        p = self._scrollable(nb, "🛠 Sysprep")
        r = tk.Frame(p, bg=BG_DARK)
        r.pack(fill="x", padx=8, pady=6)
        r.columnconfigure(0, weight=1)
        self._group(r, "Sysprep & Deployment Preparation", [
            ("Sysprep OOBE + Generalize (shutdown)...",
             self.sysprep_oobe,
             "Generalise image for mass deployment then shut down"),
            ("Sysprep OOBE + Generalize (reboot)...",
             self.sysprep_oobe_reboot,
             "Generalise image for mass deployment then reboot"),
            ("Sysprep Audit mode...",
             self.sysprep_audit,
             "Reboot into audit mode for further customisation"),
            ("Inject unattend.xml into image...",
             self.inject_unattend,
             "Copy an answer file into the mounted image at Windows\\Panther"),
            ("Set Windows edition...",
             self.set_edition,
             "Upgrade the edition of the mounted image (e.g. Home → Pro)"),
            ("Set product key...",
             self.set_product_key,
             "Inject a product key into the mounted image"),
        ], cols=3, row=0, col=0)

    # ═════════════════════════════════════════════════════════════════════════
    # MAIN TAB 3 — 💾 Storage
    # ═════════════════════════════════════════════════════════════════════════
    def _build_tab_storage(self, nb):
        outer = tk.Frame(nb, bg=BG_DARK)
        nb.add(outer, text="  💾  Storage  ")
        sub = self._sub_nb(outer)
        self._build_sub_export(sub)
        self._build_sub_vhd(sub)
        self._build_sub_disk(sub)

    def _build_sub_export(self, nb):
        p = self._scrollable(nb, "📤 Export & Convert")
        r = tk.Frame(p, bg=BG_DARK)
        r.pack(fill="x", padx=8, pady=6)
        r.columnconfigure(0, weight=1); r.columnconfigure(1, weight=1)
        self._group(r, "Export & Compress", [
            ("Export image to new WIM...",
             self.export_to_wim,
             "Export one index from a WIM to a new WIM file (removes dead space)"),
            ("Export as compressed ESD...",
             self.export_to_esd,
             "Export with maximum compression (/Compress:recovery) — smaller but slow"),
            ("Export all indexes to new WIM...",
             self.export_all_indexes,
             "Export every index from one WIM to a clean, optimised WIM"),
            ("Optimize WIM (re-export)...",
             self.optimize_wim,
             "Export in place to reclaim space after modifications"),
        ], cols=2, row=0, col=0)
        self._group(r, "Split & Merge", [
            ("Split WIM into SWM files...",
             self.split_wim,
             "Split a large WIM into FAT32-friendly SWM chunks (e.g. 3900 MB each)"),
            ("Merge SWM files to WIM...",
             self.merge_swm,
             "Combine split SWM files back into a single WIM"),
            ("Append index to existing WIM...",
             self.append_to_wim,
             "Add a new captured image as an extra index inside an existing WIM"),
            ("Delete index from WIM...",
             self.remove_volume_image,
             "Permanently remove one edition from a multi-index WIM"),
        ], cols=2, row=0, col=1)

    def _build_sub_vhd(self, nb):
        p = self._scrollable(nb, "🖥 Virtual Disk")
        r1 = tk.Frame(p, bg=BG_DARK)
        r1.pack(fill="x", padx=8, pady=6)
        r1.columnconfigure(0, weight=1); r1.columnconfigure(1, weight=1)
        self._group(r1, "Convert to Virtual Disk", [
            ("Convert to VHD (fixed)...",
             self.convert_to_vhd,
             "Apply the image into a fixed-size VHD virtual disk"),
            ("Convert to VHDX (dynamic)...",
             self.convert_to_vhdx,
             "Apply the image into a dynamically expanding VHDX"),
            ("Mount VHD/VHDX...",
             self.mount_vhd,
             "Attach a VHD/VHDX so it appears as a drive letter"),
            ("Capture mounted VHD to WIM...",
             self.capture_image,
             "Capture a VHD's Windows folder back to a WIM file"),
        ], cols=2, row=0, col=0)
        self._group(r1, "Image Information", [
            ("Save complete image information...",
             self.save_image_info,
             "List all indexes, sizes, and metadata for a WIM/ESD"),
            ("Get current edition...",
             self.get_current_edition,
             "Show the Windows edition of the mounted image"),
            ("List all mounted images...",
             self.list_mounted_images,
             "Show every currently mounted image and its mount point"),
            ("Cleanup stale mount points...",
             self.cleanup_mountpoints,
             "Remove orphaned mount entries left by crashes"),
        ], cols=2, row=0, col=1)

    def _build_sub_disk(self, nb):
        p = self._scrollable(nb, "💿 Disk Tools")
        r1 = tk.Frame(p, bg=BG_DARK)
        r1.pack(fill="x", padx=8, pady=6)
        r1.columnconfigure(0, weight=1); r1.columnconfigure(1, weight=1)
        self._group(r1, "Disk Information", [
            ("List all disks...",       self.disk_list,
             "Show all physical disks detected on this system (diskpart)"),
            ("List disk partitions...", self.disk_list_partitions,
             "Show partitions on a selected disk"),
            ("List drive letters...",   self.disk_list_volumes,
             "Show all volumes and their drive letters"),
            ("Show disk details...",    self.disk_detail,
             "Full diskpart detail output for a selected disk"),
        ], cols=2, row=0, col=0)
        self._group(r1, "Partition & Format", [
            ("Partition disk (GPT / UEFI)...",  self.deploy_partition_gpt,
             "Wipe and create EFI + MSR + Windows partitions (UEFI systems)"),
            ("Partition disk (MBR / BIOS)...",  self.deploy_partition_mbr,
             "Wipe and create a single active NTFS partition (legacy BIOS)"),
            ("Format partition...",             self.disk_format_partition,
             "Format a partition as NTFS, FAT32, or exFAT"),
            ("Assign drive letter...",          self.disk_assign_letter,
             "Assign a specific drive letter to a partition"),
            ("Remove drive letter...",          self.disk_remove_letter,
             "Remove the drive letter from a partition"),
            ("Set partition active (MBR)...",   self.disk_set_active,
             "Mark a partition as active for BIOS booting"),
        ], cols=2, row=0, col=1)
        r2 = tk.Frame(p, bg=BG_DARK)
        r2.pack(fill="x", padx=8, pady=(4, 12))
        r2.columnconfigure(0, weight=1)
        self._group(r2, "Run Custom Diskpart Script", [
            ("Run diskpart script file...", self.disk_run_script,
             "Load a .txt diskpart script and execute it"),
            ("Open diskpart console...",    self.disk_open_console,
             "Open an interactive diskpart window (Administrator)"),
        ], cols=2, row=0, col=0)

    # ═════════════════════════════════════════════════════════════════════════
    # MAIN TAB 4 — 📊 Diagnostics
    # ═════════════════════════════════════════════════════════════════════════
    def _build_tab_diagnostics(self, nb):
        outer = tk.Frame(nb, bg=BG_DARK)
        nb.add(outer, text="  📊  Diagnostics  ")
        sub = self._sub_nb(outer)
        self._build_sub_log_health(sub)
        self._build_tab_history(sub)   # reuses existing history builder

    def _build_sub_log_health(self, nb):
        p = tk.Frame(nb, bg=BG_DARK)
        nb.add(p, text="  📋  Log & Health  ")
        p.columnconfigure(0, weight=1)
        p.rowconfigure(1, weight=1)

        btn_row = tk.Frame(p, bg=BG_FRAME)
        btn_row.grid(row=0, column=0, sticky="ew")
        for txt, cmd in [
            ("Open DISM Log",         self.open_dism_log),
            ("Refresh Log",           self.refresh_log),
            ("Clear DISM Log",        self.clear_dism_log),
            ("Check Mount Status",    self.check_mount_status),
            ("Find Open Handles",     self.find_open_handles),
            ("List Mounted Images",   self.list_mounted_images),
            ("Cleanup Mount Points",  self.cleanup_mountpoints),
            ("Check Online Health",   self.check_online_health),
            ("Analyse Online Image",  self.analyse_online_health),
            ("Restore Online Health", self.restore_online_health),
        ]:
            _mk_btn(btn_row, txt, cmd).pack(side="left", padx=4, pady=4)

        self._log_text = scrolledtext.ScrolledText(
            p, bg="#0d0d0d", fg=FG_TEXT, font=("Consolas", 8),
            wrap="word", relief="flat", bd=0,
            selectbackground="#1e3a1e")
        self._log_text.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        self._log_text.tag_configure("err",  foreground=FG_RED)
        self._log_text.tag_configure("warn", foreground=FG_YELLOW)
        self._log_text.tag_configure("ok",   foreground=FG_GREEN)
        self.root.after(500, self.refresh_log)

    # ─────────────────────────────────────────────────────────────────────────
    # Core runners
    # ─────────────────────────────────────────────────────────────────────────
    def _run(self, args, title="DISM Output"):
        cmd_line = DISM_EXE + " " + " ".join(args)
        self.status_var.set("Running: " + " ".join(args[:3]) + " …")
        self._log_history(cmd_line)
        self.root.after(0, lambda: self._progress.configure(value=0))
        win = OutputWindow(self.root, title, cmd_line, app=self)

        def _worker():
            ok = True
            try:
                proc = subprocess.Popen(
                    [DISM_EXE] + args,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                for raw in proc.stdout:
                    line = raw.decode("utf-8", errors="replace")
                    self.root.after(0, lambda l=line: win.append(l))
                    m = re.search(r"(\d+\.\d+)%", line)
                    if m:
                        pct = float(m.group(1))
                        self.root.after(0, lambda v=pct: self._progress.configure(value=v))
                proc.wait()
                ok = proc.returncode == 0
            except FileNotFoundError:
                self.root.after(0, lambda: win.append(
                    f"ERROR: DISM not found at {DISM_EXE}\n"))
                ok = False
            except Exception as exc:
                self.root.after(0, lambda: win.append(f"ERROR: {exc}\n"))
                ok = False
            self.root.after(0, win.mark_done)
            self.root.after(0, lambda: self.status_var.set("Ready"))
            self.root.after(0, lambda: self._progress.configure(value=0))
            self._mark_hist_done(ok)

        threading.Thread(target=_worker, daemon=True).start()

    def _run_cmd(self, exe, args, title="Output", log=True):
        cmd_line = exe + " " + " ".join(args)
        if log:
            self.status_var.set(f"Running: {os.path.basename(exe)} …")
        self._log_history(cmd_line)
        win = OutputWindow(self.root, title, cmd_line, app=self)

        def _worker():
            ok = True
            try:
                proc = subprocess.Popen(
                    [exe] + args,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                for raw in proc.stdout:
                    line = raw.decode("utf-8", errors="replace")
                    self.root.after(0, lambda l=line: win.append(l))
                proc.wait()
                ok = proc.returncode == 0
            except Exception as exc:
                self.root.after(0, lambda: win.append(f"ERROR: {exc}\n"))
                ok = False
            self.root.after(0, win.mark_done)
            self._mark_hist_done(ok)
            if log:
                self.root.after(0, lambda: self.status_var.set("Ready"))

        threading.Thread(target=_worker, daemon=True).start()

    def _run_diskpart_gui(self, commands, title="Diskpart Output"):
        cmd_str = "diskpart /s <script>"
        win = OutputWindow(self.root, title, cmd_str, app=self)
        self.status_var.set("Running diskpart …")

        def _worker():
            try:
                out, err = _run_diskpart(commands)
                self.root.after(0, lambda: win.append(out or "(no output)"))
                if err:
                    self.root.after(0, lambda: win.append("\n--- STDERR ---\n" + err))
            except Exception as exc:
                self.root.after(0, lambda: win.append(f"ERROR: {exc}\n"))
            self.root.after(0, win.mark_done)
            self.root.after(0, lambda: self.status_var.set("Ready"))

        threading.Thread(target=_worker, daemon=True).start()

    def _run_robocopy(self, src, dst, extra_args=None, title="Robocopy"):
        args = [src, dst, "/E", "/Z", "/NP", "/ETA"] + (extra_args or [])
        self._run_cmd(ROBOCOPY, args, title)

    def _mp(self):
        mp = _norm(self.mount_point.get().strip())
        if mp:
            self.mount_point.set(mp)
            return mp
        messagebox.showwarning(
            "No Mount Point",
            "Please set a mount point first.\n\n"
            "Type the path into the 'Mount point' field on the left,\n"
            "or click  'Browse mount point…'  to choose a folder.",
            parent=self.root)
        return ""

    # ── IMAGE SERVICING ────────────────────────────────────────────────────

    def mount_image(self):
        img = _ask_file(self.root, "Select WIM/ESD image",
                        [("Image files", "*.wim *.esd *.swm"), ("All", "*.*")])
        if not img:
            return

        # Check if this image is already mounted
        mounts = _get_mounted_images()
        img_norm = _norm(img).lower()
        already_mounted = []
        for mp, info in mounts.items():
            if info.get("wim", "").lower() == img_norm:
                already_mounted.append((mp, info.get("index", "?")))

        if already_mounted:
            msg = "This image is already mounted:\n\n"
            for mp, idx in already_mounted:
                msg += f"  • Index {idx} at {mp}\n"
            msg += "\nOptions:\n"
            msg += "  YES    = Remount at new location\n"
            msg += "  NO     = Use existing mount\n"
            msg += "  CANCEL = Show mounted images & cleanup"

            choice = messagebox.askyesnocancel("Image Already Mounted", msg, parent=self.root)
            if choice is None:
                self.list_mounted_images()
                return
            elif choice is False:
                self.mount_point.set(already_mounted[0][0])
                messagebox.showinfo("Mount Point Set", f"Mount point set to:\n{already_mounted[0][0]}", parent=self.root)
                return

        mp = _ask_dir(self.root, "Select (empty) mount point directory")
        if not mp:
            return
        picker = IndexPickerDialog(self.root, img)
        self.root.wait_window(picker)
        if not picker.chosen_index:
            return
        idx = picker.chosen_index
        ro_ans = messagebox.askyesno("Read-Only?",
            "Mount read-only?\n\nYes = read-only (safe browsing)\nNo = read-write (editing)",
            parent=self.root)
        ro = "/ReadOnly" if ro_ans else ""
        args = ["/Mount-Wim", f"/WimFile:{img}", f"/index:{idx}", f"/MountDir:{mp}"]
        if ro:
            args.append(ro)
        # Track recently used paths
        _push_recent(self._recent, "images", img)
        _push_recent(self._recent, "mounts", mp)
        _save_recent(self._recent)
        self.mount_point.set(mp)
        self._run(args, f"Mount Image  [idx {idx}]")

    def commit_image(self):
        mp = self._mp()
        if not mp:
            return
        if not messagebox.askyesno("Commit Changes",
                "Commit all changes to the mounted image?\n(Image stays mounted)",
                parent=self.root):
            return
        self._run(["/Commit-Wim", f"/MountDir:{mp}"], "Commit Image")

    def commit_unmount_image(self):
        mp = self._mp()
        if not mp:
            return
        if not messagebox.askyesno("Save & Unmount",
                "Save all changes and unmount the image?", parent=self.root):
            return
        self._run(["/Unmount-Wim", f"/MountDir:{mp}", "/Commit"], "Save & Unmount")

    def unmount_image_discard(self):
        mp = self._mp()
        if not mp:
            return
        if not messagebox.askyesno("Discard & Unmount",
                "DISCARD all changes and unmount?\nThis cannot be undone!", parent=self.root):
            return
        self._run(["/Unmount-Wim", f"/MountDir:{mp}", "/Discard"], "Discard & Unmount")

    def reload_session(self):
        mp = self._mp()
        if not mp:
            return
        self._run(["/Remount-Wim", f"/MountDir:{mp}"], "Reload / Remount Session")

    def switch_image_index(self):
        img = _ask_file(self.root, "Select WIM/ESD image",
                        [("Image files", "*.wim *.esd"), ("All", "*.*")])
        if not img:
            return
        picker = IndexPickerDialog(self.root, img)
        self.root.wait_window(picker)
        if not picker.chosen_index:
            return
        mp = _ask_dir(self.root, "Select mount point to switch index")
        if not mp:
            return
        self._run(["/Remount-Wim", f"/WimFile:{img}",
                   f"/index:{picker.chosen_index}", f"/MountDir:{mp}"],
                  f"Switch to Index {picker.chosen_index}")

    def apply_image(self):
        img = _ask_file(self.root, "Select WIM/ESD image",
                        [("Image files", "*.wim *.esd"), ("All", "*.*")])
        if not img:
            return
        picker = IndexPickerDialog(self.root, img)
        self.root.wait_window(picker)
        if not picker.chosen_index:
            return
        dst = _ask_dir(self.root, "Select apply destination (drive root or folder)")
        if not dst:
            return
        self._run(["/Apply-Image", f"/ImageFile:{img}",
                   f"/index:{picker.chosen_index}", f"/ApplyDir:{dst}"],
                  "Apply Image")

    def capture_image(self):
        src = _ask_dir(self.root, "Select source directory to capture")
        if not src:
            return
        dst = filedialog.asksaveasfilename(
            title="Save captured WIM as", parent=self.root,
            defaultextension=".wim",
            filetypes=[("WIM", "*.wim"), ("All", "*.*")])
        dst = _norm(dst)
        if not dst:
            return
        name = simpledialog.askstring("Image Name", "Enter image name:", parent=self.root) or "Captured"
        desc = simpledialog.askstring("Description", "Enter description (optional):", parent=self.root) or ""
        args = ["/Capture-Image", f"/ImageFile:{dst}",
                f"/CaptureDir:{src}", f"/Name:{name}"]
        if desc:
            args.append(f"/Description:{desc}")
        self._run(args, "Capture Image")

    def remove_volume_image(self):
        img = _ask_file(self.root, "Select WIM file",
                        [("WIM", "*.wim"), ("All", "*.*")])
        if not img:
            return
        picker = IndexPickerDialog(self.root, img)
        self.root.wait_window(picker)
        if not picker.chosen_index:
            return
        if not messagebox.askyesno("Remove Index",
                f"Permanently remove index {picker.chosen_index} from the WIM?\n"
                "This cannot be undone!", parent=self.root):
            return
        self._run(["/Delete-Image", f"/ImageFile:{img}",
                   f"/index:{picker.chosen_index}"], "Remove Volume Image")

    def save_image_info(self):
        img = _ask_file(self.root, "Select image to query",
                        [("Image files", "*.wim *.esd *.swm"), ("All", "*.*")])
        if not img:
            return
        out_path = filedialog.asksaveasfilename(
            title="Save info as", parent=self.root,
            defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("All", "*.*")])
        out_path = _norm(out_path)
        if not out_path:
            return
        self._run(["/Get-ImageInfo", f"/ImageFile:{img}", f"/LogPath:{out_path}"],
                  "Save Image Info")

    def view_image_properties(self):
        mp = self._mp()
        if not mp:
            return
        self._run(["/Get-MountedImageInfo"], "View Mounted Image Properties")

    def list_mounted_images(self):
        self._run(["/Get-MountedImageInfo"], "List Mounted Images")

    def check_mount_status(self):
        """Show a dialog with current mount status and quick actions."""
        mounts = _get_mounted_images()

        if not mounts:
            messagebox.showinfo("No Mounts", "No images currently mounted.", parent=self.root)
            return

        msg = f"Currently mounted: {len(mounts)} image(s)\n\n"
        for mp, info in sorted(mounts.items()):
            wim = info.get("wim", "?")
            idx = info.get("index", "?")
            msg += f"📍 {mp}\n"
            msg += f"   {os.path.basename(wim)} [idx {idx}]\n\n"

        choice = messagebox.askyesnocancel("Mount Status", msg + "Cleanup stale mounts?", parent=self.root)
        if choice:
            self.cleanup_mountpoints()

    def find_open_handles(self):
        """Find processes with open handles to mount directory."""
        mp = _norm(self.mount_point.get().strip())
        if not mp:
            messagebox.showwarning("No Mount Point", "Set mount point first.", parent=self.root)
            return

        msg = "Checking for open handles in:\n" + mp + "\n\n"
        try:
            r = subprocess.run(
                ["powershell", "-Command",
                 f"Get-Process | ForEach-Object {{$_.Modules | Where-Object {{$_.FileName -like '{mp}*'}} | Select-Object @{{Name='Process';Expression={{$_.ProcessName}}}} }}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            output = r.stdout.decode("utf-8", errors="replace").strip()
            if output:
                msg += "Processes with open handles:\n" + output
            else:
                msg += "No processes found with open handles.\n\nTry:\n"
                msg += "  1. Close Explorer/file managers showing this directory\n"
                msg += "  2. Close any applications with files from this mount\n"
                msg += "  3. Retry unmounting"
        except Exception as e:
            msg += f"Could not query handles: {e}\n\n"
            msg += "Manual troubleshooting:\n"
            msg += "  1. Close all applications accessing this mount\n"
            msg += "  2. Close Explorer windows showing mount directory\n"
            msg += "  3. Reload session and try again"

        messagebox.showinfo("Open Handles", msg, parent=self.root)

    # ── PACKAGES ─────────────────────────────────────────────────────────

    def add_package(self):
        mp = self._mp()
        if not mp:
            return
        pkg = _ask_file(self.root, "Select package (.cab or .msu)",
                        [("Packages", "*.cab *.msu"), ("All", "*.*")])
        if not pkg:
            return
        self._run(["/Image:" + mp, "/Add-Package", f"/PackagePath:{pkg}"],
                  "Add Package")

    def get_packages(self):
        mp = self._mp()
        if not mp:
            return
        self._run(["/Image:" + mp, "/Get-Packages"], "Get Packages")

    def save_packages(self):
        mp = self._mp()
        if not mp:
            return
        out = filedialog.asksaveasfilename(
            title="Save package list", parent=self.root,
            defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("All", "*.*")])
        out = _norm(out)
        if not out:
            return
        self._run(["/Image:" + mp, "/Get-Packages", f"/LogPath:{out}"],
                  "Save Package List")

    def remove_package(self):
        mp = self._mp()
        if not mp:
            return
        pkg = simpledialog.askstring("Package Name",
            "Enter package identity or .cab path to remove:", parent=self.root)
        if not pkg:
            return
        pkg = _norm(pkg)
        if pkg.lower().endswith(".cab"):
            arg = f"/PackagePath:{pkg}"
        else:
            arg = f"/PackageName:{pkg}"
        self._run(["/Image:" + mp, "/Remove-Package", arg], "Remove Package")

    def cleanup_image(self):
        mp = self._mp()
        if not mp:
            return
        choice = messagebox.askyesnocancel(
            "Cleanup Image",
            "Yes  = StartComponentCleanup (smaller WIM)\n"
            "No   = RevertPendingActions (roll back pending)\n"
            "Cancel = abort",
            parent=self.root)
        if choice is None:
            return
        flag = "/StartComponentCleanup" if choice else "/RevertPendingActions"
        self._run(["/Image:" + mp, "/Cleanup-Image", flag], "Cleanup Image")

    # ── FEATURES ─────────────────────────────────────────────────────────

    def enable_feature(self):
        mp = self._mp()
        if not mp:
            return
        FeatureManagerDialog(self.root, mp, app=self, initial_filter="Disabled")

    def get_features(self):
        mp = self._mp()
        if not mp:
            return
        self._run(["/Image:" + mp, "/Get-Features"], "Get Features")

    def save_features(self):
        mp = self._mp()
        if not mp:
            return
        out = filedialog.asksaveasfilename(
            title="Save feature list", parent=self.root,
            defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("All", "*.*")])
        out = _norm(out)
        if not out:
            return
        self._run(["/Image:" + mp, "/Get-Features", f"/LogPath:{out}"],
                  "Save Feature List")

    def disable_feature(self):
        mp = self._mp()
        if not mp:
            return
        FeatureManagerDialog(self.root, mp, app=self, initial_filter="Enabled")

    def manage_features(self):
        mp = self._mp()
        if not mp:
            return
        FeatureManagerDialog(self.root, mp, app=self, initial_filter="All")

    # ── APPX ─────────────────────────────────────────────────────────────

    def add_appx_package(self):
        mp = self._mp()
        if not mp:
            return
        pkg = _ask_file(self.root, "Select AppX package",
                        [("AppX packages", "*.appx *.appxbundle *.msix"), ("All", "*.*")])
        if not pkg:
            return
        lic = _ask_file(self.root, "Select license file (optional – cancel to skip)",
                        [("License", "*.xml"), ("All", "*.*")])
        args = ["/Image:" + mp, "/Add-ProvisionedAppxPackage",
                f"/PackagePath:{pkg}", "/SkipLicense"]
        if lic:
            args = ["/Image:" + mp, "/Add-ProvisionedAppxPackage",
                    f"/PackagePath:{pkg}", f"/LicensePath:{lic}"]
        self._run(args, "Add AppX Package")

    def get_appx_packages(self):
        mp = self._mp()
        if not mp:
            return
        self._run(["/Image:" + mp, "/Get-ProvisionedAppxPackages"], "Get AppX Packages")

    def save_appx_packages(self):
        mp = self._mp()
        if not mp:
            return
        out = filedialog.asksaveasfilename(
            title="Save AppX list", parent=self.root,
            defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("All", "*.*")])
        out = _norm(out)
        if not out:
            return
        self._run(["/Image:" + mp, "/Get-ProvisionedAppxPackages", f"/LogPath:{out}"],
                  "Save AppX List")

    def remove_appx_package(self):
        mp = self._mp()
        if not mp:
            return
        name = simpledialog.askstring("Package Name",
            "Enter provisioned AppX package name to remove:", parent=self.root)
        if not name:
            return
        self._run(["/Image:" + mp, "/Remove-ProvisionedAppxPackage",
                   f"/PackageName:{name}"], "Remove AppX Package")

    # ── CAPABILITIES ─────────────────────────────────────────────────────

    def add_capability(self):
        mp = self._mp()
        if not mp:
            return
        cap = simpledialog.askstring("Capability Name",
            "Enter capability name (e.g. Language.Basic~~~en-US~0.0.1.0):",
            parent=self.root)
        if not cap:
            return
        self._run(["/Image:" + mp, "/Add-Capability", f"/CapabilityName:{cap}"],
                  f"Add Capability: {cap}")

    def get_capabilities(self):
        mp = self._mp()
        if not mp:
            return
        self._run(["/Image:" + mp, "/Get-Capabilities"], "Get Capabilities")

    def save_capabilities(self):
        mp = self._mp()
        if not mp:
            return
        out = filedialog.asksaveasfilename(
            title="Save capability list", parent=self.root,
            defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("All", "*.*")])
        out = _norm(out)
        if not out:
            return
        self._run(["/Image:" + mp, "/Get-Capabilities", f"/LogPath:{out}"],
                  "Save Capability List")

    def remove_capability(self):
        mp = self._mp()
        if not mp:
            return
        cap = simpledialog.askstring("Capability Name",
            "Enter capability name to remove:", parent=self.root)
        if not cap:
            return
        self._run(["/Image:" + mp, "/Remove-Capability", f"/CapabilityName:{cap}"],
                  "Remove Capability")

    # ── DRIVERS ──────────────────────────────────────────────────────────

    def add_driver(self):
        mp = self._mp()
        if not mp:
            return
        choice = messagebox.askyesno("Driver Source",
            "Yes = single .inf file\nNo = folder of drivers (recursive)",
            parent=self.root)
        if choice:
            drv = _ask_file(self.root, "Select driver .inf",
                            [("INF files", "*.inf"), ("All", "*.*")])
            if not drv:
                return
            args = ["/Image:" + mp, "/Add-Driver", f"/Driver:{drv}"]
        else:
            folder = _ask_dir(self.root, "Select driver folder")
            if not folder:
                return
            args = ["/Image:" + mp, "/Add-Driver", f"/Driver:{folder}", "/Recurse"]
        self._run(args, "Add Driver")

    def get_drivers(self):
        mp = self._mp()
        if not mp:
            return
        self._run(["/Image:" + mp, "/Get-Drivers"], "Get Drivers")

    def save_drivers(self):
        mp = self._mp()
        if not mp:
            return
        out = filedialog.asksaveasfilename(
            title="Save driver list", parent=self.root,
            defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("All", "*.*")])
        out = _norm(out)
        if not out:
            return
        self._run(["/Image:" + mp, "/Get-Drivers", f"/LogPath:{out}"],
                  "Save Driver List")

    def remove_driver(self):
        mp = self._mp()
        if not mp:
            return
        drv = simpledialog.askstring("Driver Name",
            "Enter OEM driver name (e.g. oem0.inf) to remove:", parent=self.root)
        if not drv:
            return
        self._run(["/Image:" + mp, "/Remove-Driver", f"/Driver:{drv}"],
                  "Remove Driver")

    # ── WINDOWS PE ───────────────────────────────────────────────────────

    def pe_get_config(self):
        mp = self._mp()
        if not mp:
            return
        self._run(["/Image:" + mp, "/Get-PESettings"], "WinPE: Get Configuration")

    def pe_set_profile(self):
        mp = self._mp()
        if not mp:
            return
        prof = simpledialog.askstring("PE Profile",
            "Enter WinPE profile (WinPE-Setup, WinPE-Scripting, etc.):",
            parent=self.root)
        if not prof:
            return
        self._run(["/Image:" + mp, "/Apply-Profiles", f"/Profile:{prof}"],
                  "WinPE: Set Profile")

    def pe_set_target_path(self):
        mp = self._mp()
        if not mp:
            return
        tp = simpledialog.askstring("Target Path",
            "Enter WinPE target path:", parent=self.root)
        if not tp:
            return
        self._run(["/Image:" + mp, "/Set-TargetPath", f"/TargetPath:{tp}"],
                  "WinPE: Set Target Path")

    def pe_set_scratch_space(self):
        mp = self._mp()
        if not mp:
            return
        sz = simpledialog.askstring("Scratch Space",
            "Enter scratch space in MB (32, 64, 128, 256, 512):",
            parent=self.root)
        if not sz:
            return
        self._run(["/Image:" + mp, "/Set-ScratchSpace", f"/ScratchSpace:{sz}"],
                  "WinPE: Set Scratch Space")

    # ── DEPLOYMENT ────────────────────────────────────────────────────────

    def _pick_disk(self, prompt="Select target disk"):
        dlg = DiskPickerDialog(self.root, prompt)
        self.root.wait_window(dlg)
        return dlg.chosen_disk  # e.g. "0"

    def _usb_worker(self, disk_num, img_path, idx, scheme="MBR", label="WINPE"):
        """Background worker: format USB, apply WIM."""
        win = OutputWindow(self.root, "USB Creation", f"Disk {disk_num}", app=self)

        def _worker():
            try:
                self.root.after(0, lambda: win.append(f"[1/4] Partitioning disk {disk_num} ({scheme})…\n"))
                if scheme == "GPT":
                    cmds = [
                        f"select disk {disk_num}",
                        "clean", "convert gpt",
                        "create partition efi size=200",
                        "format fs=fat32 quick label=EFI",
                        "assign letter=S",
                        "create partition primary",
                        f"format fs=ntfs quick label={label}",
                        "assign letter=W",
                        "exit",
                    ]
                else:
                    cmds = [
                        f"select disk {disk_num}",
                        "clean", "create partition primary",
                        f"format fs=ntfs quick label={label}",
                        "active", "assign letter=W",
                        "exit",
                    ]
                out, err = _run_diskpart(cmds)
                self.root.after(0, lambda: win.append(out + "\n"))

                self.root.after(0, lambda: win.append("[2/4] Applying image…\n"))
                proc = subprocess.Popen(
                    [DISM_EXE, "/Apply-Image",
                     f"/ImageFile:{img_path}", f"/index:{idx}", "/ApplyDir:W:\\"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                for raw in proc.stdout:
                    line = raw.decode("utf-8", errors="replace")
                    self.root.after(0, lambda l=line: win.append(l))
                proc.wait()

                self.root.after(0, lambda: win.append("[3/4] Installing bootloader…\n"))
                bc = subprocess.run(
                    [BCDBOOT, "W:\\Windows", "/s", "W:\\", "/f",
                     "UEFI" if scheme == "GPT" else "BIOS"],
                    capture_output=True, text=True)
                self.root.after(0, lambda: win.append(bc.stdout + bc.stderr + "\n"))
                self.root.after(0, lambda: win.append("[4/4] Done! USB is bootable.\n"))
            except Exception as exc:
                self.root.after(0, lambda: win.append(f"ERROR: {exc}\n"))
            self.root.after(0, win.mark_done)

        threading.Thread(target=_worker, daemon=True).start()

    def usb_create_bootable(self):
        """BIOS+UEFI combined bootable USB."""
        img = _ask_file(self.root, "Select WIM/ESD image",
                        [("Image files", "*.wim *.esd"), ("All", "*.*")])
        if not img:
            return
        picker = IndexPickerDialog(self.root, img)
        self.root.wait_window(picker)
        if not picker.chosen_index:
            return
        disk_num = self._pick_disk("Select USB drive (ALL DATA WILL BE ERASED!)")
        if disk_num is None:
            return
        if not messagebox.askyesno("Confirm FORMAT",
                f"ALL DATA on Disk {disk_num} will be erased!\nContinue?",
                parent=self.root):
            return
        scheme = "GPT"
        self._usb_worker(disk_num, img, picker.chosen_index, scheme)

    def usb_create_uefi(self):
        """UEFI-only FAT32 USB."""
        img = _ask_file(self.root, "Select WIM/ESD image",
                        [("Image files", "*.wim *.esd"), ("All", "*.*")])
        if not img:
            return
        picker = IndexPickerDialog(self.root, img)
        self.root.wait_window(picker)
        if not picker.chosen_index:
            return
        disk_num = self._pick_disk("Select USB drive – UEFI only (ALL DATA ERASED!)")
        if disk_num is None:
            return
        if not messagebox.askyesno("Confirm",
                f"Disk {disk_num} will be wiped for UEFI boot. Continue?",
                parent=self.root):
            return
        self._usb_worker(disk_num, img, picker.chosen_index, "GPT", "WINUEFI")

    def usb_apply_wim(self):
        """Apply WIM to an already-formatted USB."""
        img = _ask_file(self.root, "Select WIM/ESD image",
                        [("Image files", "*.wim *.esd"), ("All", "*.*")])
        if not img:
            return
        picker = IndexPickerDialog(self.root, img)
        self.root.wait_window(picker)
        if not picker.chosen_index:
            return
        dst = _ask_dir(self.root, "Select USB root directory")
        if not dst:
            return
        self._run(["/Apply-Image", f"/ImageFile:{img}",
                   f"/index:{picker.chosen_index}", f"/ApplyDir:{dst}"],
                  "Apply WIM to USB")

    def usb_format(self):
        """Just format USB, no image apply."""
        disk_num = self._pick_disk("Select USB to format (ALL DATA ERASED!)")
        if disk_num is None:
            return
        fs = simpledialog.askstring("Filesystem", "Filesystem? (NTFS / FAT32):",
                                    parent=self.root) or "NTFS"
        label = simpledialog.askstring("Label", "Volume label:", parent=self.root) or "USB"
        if not messagebox.askyesno("Confirm", f"Format disk {disk_num} as {fs}?",
                                   parent=self.root):
            return
        cmds = [
            f"select disk {disk_num}",
            "clean", "create partition primary",
            f"format fs={fs.lower()} quick label={label}",
            "active", "assign", "exit",
        ]
        self._run_diskpart_gui(cmds, "Format USB")

    def deploy_apply_to_disk(self):
        """Apply image to a bare-metal physical disk partition."""
        img = _ask_file(self.root, "Select WIM/ESD image",
                        [("Image files", "*.wim *.esd"), ("All", "*.*")])
        if not img:
            return
        picker = IndexPickerDialog(self.root, img)
        self.root.wait_window(picker)
        if not picker.chosen_index:
            return
        disk_num = self._pick_disk("Select TARGET disk (will be partitioned!)")
        if disk_num is None:
            return
        scheme = "GPT" if messagebox.askyesno("Partition Scheme",
            "Yes = GPT (UEFI, modern)\nNo = MBR (Legacy BIOS)",
            parent=self.root) else "MBR"
        if not messagebox.askyesno("Confirm",
                f"Disk {disk_num} will be wiped and Windows installed ({scheme}).\n"
                "ALL DATA LOST. Continue?", parent=self.root):
            return
        win = OutputWindow(self.root, "Deploy to Disk", f"Disk {disk_num} / {scheme}", app=self)

        def _worker():
            try:
                self.root.after(0, lambda: win.append(f"[1/4] Partitioning disk {disk_num} {scheme}…\n"))
                if scheme == "GPT":
                    cmds = [
                        f"select disk {disk_num}", "clean", "convert gpt",
                        "create partition efi size=260",
                        "format fs=fat32 quick label=System", "assign letter=S",
                        "create partition msr size=16",
                        "create partition primary",
                        "format fs=ntfs quick label=Windows", "assign letter=W",
                        "exit",
                    ]
                else:
                    cmds = [
                        f"select disk {disk_num}", "clean",
                        "create partition primary size=500",
                        "format fs=ntfs quick label=System", "active", "assign letter=S",
                        "create partition primary",
                        "format fs=ntfs quick label=Windows", "assign letter=W",
                        "exit",
                    ]
                out, _ = _run_diskpart(cmds)
                self.root.after(0, lambda: win.append(out + "\n"))

                self.root.after(0, lambda: win.append("[2/4] Applying image…\n"))
                proc = subprocess.Popen(
                    [DISM_EXE, "/Apply-Image",
                     f"/ImageFile:{img}", f"/index:{picker.chosen_index}",
                     "/ApplyDir:W:\\"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                for raw in proc.stdout:
                    line = raw.decode("utf-8", errors="replace")
                    self.root.after(0, lambda l=line: win.append(l))
                proc.wait()

                self.root.after(0, lambda: win.append("[3/4] Installing bootloader…\n"))
                boot_drive = "S:\\" if scheme == "GPT" else "S:\\"
                bc = subprocess.run(
                    [BCDBOOT, "W:\\Windows", "/s", boot_drive, "/f",
                     "UEFI" if scheme == "GPT" else "BIOS"],
                    capture_output=True, text=True)
                self.root.after(0, lambda: win.append(bc.stdout + bc.stderr + "\n"))
                self.root.after(0, lambda: win.append("[4/4] Deployment complete!\n"))
            except Exception as exc:
                self.root.after(0, lambda: win.append(f"ERROR: {exc}\n"))
            self.root.after(0, win.mark_done)

        threading.Thread(target=_worker, daemon=True).start()

    def deploy_partition_gpt(self):
        disk_num = self._pick_disk("Select disk to partition as GPT (UEFI)")
        if disk_num is None:
            return
        if not messagebox.askyesno("Confirm GPT Partition",
                f"Disk {disk_num} will be wiped and partitioned GPT. Continue?",
                parent=self.root):
            return
        cmds = [
            f"select disk {disk_num}", "clean", "convert gpt",
            "create partition efi size=260",
            "format fs=fat32 quick label=System", "assign letter=S",
            "create partition msr size=16",
            "create partition primary",
            "format fs=ntfs quick label=Windows", "assign letter=W",
            "exit",
        ]
        self._run_diskpart_gui(cmds, "Partition Disk (GPT)")

    def deploy_partition_mbr(self):
        disk_num = self._pick_disk("Select disk to partition as MBR (Legacy BIOS)")
        if disk_num is None:
            return
        if not messagebox.askyesno("Confirm MBR Partition",
                f"Disk {disk_num} will be wiped and partitioned MBR. Continue?",
                parent=self.root):
            return
        cmds = [
            f"select disk {disk_num}", "clean",
            "create partition primary size=500",
            "format fs=ntfs quick label=System", "active", "assign letter=S",
            "create partition primary",
            "format fs=ntfs quick label=Windows", "assign letter=W",
            "exit",
        ]
        self._run_diskpart_gui(cmds, "Partition Disk (MBR)")

    def deploy_bcdboot(self):
        win_dir = _ask_dir(self.root, "Select Windows directory (e.g. W:\\Windows)")
        if not win_dir:
            return
        sys_drv = simpledialog.askstring("System Drive",
            "System/EFI partition letter (e.g. S):", parent=self.root) or "S"
        fw = "UEFI" if messagebox.askyesno("Firmware", "Yes=UEFI  No=BIOS",
                                           parent=self.root) else "BIOS"
        self._run_cmd(BCDBOOT,
            [win_dir, "/s", sys_drv + ":\\", "/f", fw],
            "Install Bootloader (bcdboot)")

    def net_copy_to_share(self):
        img = _ask_file(self.root, "Select image to copy",
                        [("Image files", "*.wim *.esd *.iso"), ("All", "*.*")])
        if not img:
            return
        share = simpledialog.askstring("Network Share",
            "Enter UNC share path (e.g. \\\\SERVER\\Images):", parent=self.root)
        if not share:
            return
        self._run_robocopy(os.path.dirname(img), share,
                           [os.path.basename(img)], "Copy to Network Share")

    def net_copy_to_nas(self):
        img = _ask_file(self.root, "Select image to copy",
                        [("Image files", "*.wim *.esd *.iso"), ("All", "*.*")])
        if not img:
            return
        nas_path = simpledialog.askstring("NAS Path",
            "Enter UNC or mapped path (e.g. \\\\NAS\\Backup):", parent=self.root)
        if not nas_path:
            return
        user = simpledialog.askstring("NAS Username (optional)",
            "Username (leave blank to skip):", parent=self.root)
        win = OutputWindow(self.root, "Copy to NAS", f"→ {nas_path}", app=self)

        def _worker():
            try:
                if user:
                    pwd_input = simpledialog.askstring("NAS Password", "Password:",
                                                       parent=self.root, show="*")
                    net = subprocess.run(
                        ["net", "use", nas_path, f"/user:{user}", pwd_input or ""],
                        capture_output=True, text=True)
                    self.root.after(0, lambda: win.append(net.stdout + net.stderr + "\n"))
                proc = subprocess.Popen(
                    [ROBOCOPY, os.path.dirname(img), nas_path,
                     os.path.basename(img), "/Z", "/NP", "/ETA"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                for raw in proc.stdout:
                    line = raw.decode("utf-8", errors="replace")
                    self.root.after(0, lambda l=line: win.append(l))
                proc.wait()
                self.root.after(0, lambda: win.append("Done!\n"))
            except Exception as exc:
                self.root.after(0, lambda: win.append(f"ERROR: {exc}\n"))
            self.root.after(0, win.mark_done)

        threading.Thread(target=_worker, daemon=True).start()

    def net_apply_from_share(self):
        share_img = simpledialog.askstring("Network Image",
            "Enter full UNC path to WIM (e.g. \\\\SERVER\\Images\\install.wim):",
            parent=self.root)
        if not share_img:
            return
        share_img = _norm(share_img)
        picker = IndexPickerDialog(self.root, share_img)
        self.root.wait_window(picker)
        if not picker.chosen_index:
            return
        dst = _ask_dir(self.root, "Select apply destination")
        if not dst:
            return
        self._run(["/Apply-Image", f"/ImageFile:{share_img}",
                   f"/index:{picker.chosen_index}", f"/ApplyDir:{dst}"],
                  "Apply Image from Network Share")

    def net_map_drive(self):
        share = simpledialog.askstring("Map Drive",
            "Enter UNC path (e.g. \\\\SERVER\\Share):", parent=self.root)
        if not share:
            return
        letter = simpledialog.askstring("Drive Letter",
            "Drive letter to assign (e.g. Z):", parent=self.root) or "Z"
        user = simpledialog.askstring("Username (optional)", "Username:", parent=self.root)
        win = OutputWindow(self.root, "Map Network Drive", f"{letter}: → {share}", app=self)

        def _worker():
            try:
                cmd = ["net", "use", f"{letter}:", share]
                if user:
                    pwd = simpledialog.askstring("Password", "Password:",
                                                 parent=self.root, show="*")
                    cmd += [f"/user:{user}", pwd or ""]
                result = subprocess.run(cmd, capture_output=True, text=True)
                self.root.after(0, lambda: win.append(result.stdout + result.stderr))
            except Exception as exc:
                self.root.after(0, lambda: win.append(f"ERROR: {exc}\n"))
            self.root.after(0, win.mark_done)

        threading.Thread(target=_worker, daemon=True).start()

    def net_disconnect_drive(self):
        letter = simpledialog.askstring("Disconnect Drive",
            "Drive letter to disconnect (e.g. Z):", parent=self.root)
        if not letter:
            return
        win = OutputWindow(self.root, "Disconnect Drive", f"net use {letter}: /delete", app=self)

        def _worker():
            try:
                result = subprocess.run(["net", "use", f"{letter}:", "/delete"],
                                        capture_output=True, text=True)
                self.root.after(0, lambda: win.append(result.stdout + result.stderr))
            except Exception as exc:
                self.root.after(0, lambda: win.append(f"ERROR: {exc}\n"))
            self.root.after(0, win.mark_done)

        threading.Thread(target=_worker, daemon=True).start()

    def wds_upload_image(self):
        img = _ask_file(self.root, "Select image for WDS",
                        [("WIM", "*.wim"), ("All", "*.*")])
        if not img:
            return
        grp = simpledialog.askstring("Image Group",
            "WDS image group name (e.g. Windows11):", parent=self.root) or "Default"
        self._run_cmd(WDSUTIL,
            ["/Add-Image", f"/ImageFile:{img}", "/ImageType:Install",
             f"/ImageGroup:{grp}"],
            "WDS: Upload Image")

    def wds_list_images(self):
        self._run_cmd(WDSUTIL, ["/Get-AllImages", "/Show:All"], "WDS: List Images")

    def wds_remove_image(self):
        name = simpledialog.askstring("Image Name",
            "Enter WDS image name to remove:", parent=self.root)
        if not name:
            return
        grp = simpledialog.askstring("Image Group",
            "Image group name:", parent=self.root) or "Default"
        self._run_cmd(WDSUTIL,
            ["/Remove-Image", f"/ImageName:{name}",
             "/ImageType:Install", f"/ImageGroup:{grp}"],
            "WDS: Remove Image")

    def wds_gen_pxe(self):
        messagebox.showinfo("WDS PXE Info",
            "To configure PXE boot:\n\n"
            "1. Install Windows Deployment Services role (Server Manager)\n"
            "2. Run: wdsutil /Initialize-Server /RemInst:C:\\RemoteInstall\n"
            "3. Run: wdsutil /Set-Server /AnswerClients:All\n"
            "4. Add boot image: wdsutil /Add-Image /ImageFile:boot.wim /ImageType:Boot\n"
            "5. Add install image: wdsutil /Add-Image /ImageFile:install.wim /ImageType:Install\n"
            "6. Ensure DHCP options 66 (TFTP server) and 67 (bootfile) are set\n\n"
            "Clients must be set to PXE boot in BIOS/UEFI.",
            parent=self.root)

    def wds_server_info(self):
        self._run_cmd(WDSUTIL, ["/Get-Server", "/Show:All"], "WDS: Server Info")

    def sysprep_oobe(self):
        if not messagebox.askyesno("Sysprep OOBE",
                "Run Sysprep /oobe /generalize /shutdown?\n\n"
                "This will generalize the OS and shut down. Continue?",
                parent=self.root):
            return
        self._run_cmd(SYSPREP,
            ["/oobe", "/generalize", "/shutdown"],
            "Sysprep: OOBE + Generalize")

    def sysprep_oobe_reboot(self):
        if not messagebox.askyesno("Sysprep OOBE Reboot",
                "Run Sysprep /oobe /generalize /reboot?\n\nContinue?",
                parent=self.root):
            return
        self._run_cmd(SYSPREP,
            ["/oobe", "/generalize", "/reboot"],
            "Sysprep: OOBE + Reboot")

    def sysprep_audit(self):
        if not messagebox.askyesno("Sysprep Audit",
                "Run Sysprep /audit /reboot?\n\nContinue?",
                parent=self.root):
            return
        self._run_cmd(SYSPREP, ["/audit", "/reboot"], "Sysprep: Audit Mode")

    def inject_unattend(self):
        mp = self._mp()
        if not mp:
            return
        xml = _ask_file(self.root, "Select unattend.xml",
                        [("XML", "*.xml"), ("All", "*.*")])
        if not xml:
            return
        self._run(["/Image:" + mp, "/Apply-Unattend:" + xml],
                  "Inject Unattend.xml")

    def set_edition(self):
        mp = self._mp()
        if not mp:
            return
        editions = {
            "Home": "Core", "Home N": "CoreN",
            "Professional": "Professional", "Professional N": "ProfessionalN",
            "Enterprise": "Enterprise", "Enterprise N": "EnterpriseN",
            "Education": "Education",
        }
        dlg = tk.Toplevel(self.root)
        dlg.title("Set Windows Edition")
        dlg.grab_set()
        tk.Label(dlg, text="Select target edition:").pack(padx=10, pady=5)
        choice_var = tk.StringVar(value="Professional")
        lb = tk.Listbox(dlg, listvariable=tk.StringVar(value=list(editions.keys())),
                        height=8, selectmode="single")
        lb.pack(padx=10, pady=5, fill="both", expand=True)
        lb.select_set(2)

        def _ok():
            sel = lb.curselection()
            if sel:
                ed_name = lb.get(sel[0])
                ed_id = editions[ed_name]
                dlg.destroy()
                self._run(["/Image:" + mp, "/Set-Edition:" + ed_id],
                          f"Set Edition: {ed_name}")
            else:
                dlg.destroy()

        tk.Button(dlg, text="Set Edition", command=_ok).pack(pady=5)

    def set_product_key(self):
        mp = self._mp()
        if not mp:
            return
        key = simpledialog.askstring("Product Key",
            "Enter product key (XXXXX-XXXXX-XXXXX-XXXXX-XXXXX):",
            parent=self.root)
        if not key:
            return
        self._run(["/Image:" + mp, "/Set-ProductKey:" + key], "Set Product Key")

    # ── EXPORT & CONVERT ─────────────────────────────────────────────────

    def _vhd_worker(self, img, idx, out_path, vhd_type="vhd", size_mb=40960):
        win = OutputWindow(self.root, f"Create {vhd_type.upper()}", out_path, app=self)

        def _worker():
            try:
                self.root.after(0, lambda: win.append(f"[1/3] Creating {vhd_type.upper()} ({size_mb} MB)…\n"))
                cmds = [
                    f"create vdisk file={out_path} maximum={size_mb} type=fixed",
                    f"select vdisk file={out_path}",
                    "attach vdisk",
                    "create partition primary",
                    "format fs=ntfs quick label=Windows",
                    "assign letter=V",
                    "exit",
                ]
                out, _ = _run_diskpart(cmds)
                self.root.after(0, lambda: win.append(out + "\n"))

                self.root.after(0, lambda: win.append("[2/3] Applying image to VHD…\n"))
                proc = subprocess.Popen(
                    [DISM_EXE, "/Apply-Image",
                     f"/ImageFile:{img}", f"/index:{idx}", "/ApplyDir:V:\\"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                for raw in proc.stdout:
                    line = raw.decode("utf-8", errors="replace")
                    self.root.after(0, lambda l=line: win.append(l))
                proc.wait()

                self.root.after(0, lambda: win.append("[3/3] Installing bootloader in VHD…\n"))
                bc = subprocess.run(
                    [BCDBOOT, "V:\\Windows", "/s", "V:\\", "/f", "ALL"],
                    capture_output=True, text=True)
                self.root.after(0, lambda: win.append(bc.stdout + bc.stderr + "\n"))

                detach_cmds = [
                    f"select vdisk file={out_path}",
                    "detach vdisk", "exit",
                ]
                _run_diskpart(detach_cmds)
                self.root.after(0, lambda: win.append(f"VHD ready: {out_path}\n"))
            except Exception as exc:
                self.root.after(0, lambda: win.append(f"ERROR: {exc}\n"))
            self.root.after(0, win.mark_done)

        threading.Thread(target=_worker, daemon=True).start()

    def export_to_wim(self):
        src = _ask_file(self.root, "Select source image",
                        [("Image files", "*.wim *.esd"), ("All", "*.*")])
        if not src:
            return
        picker = IndexPickerDialog(self.root, src)
        self.root.wait_window(picker)
        if not picker.chosen_index:
            return
        dst = filedialog.asksaveasfilename(
            title="Save as WIM", parent=self.root,
            defaultextension=".wim", filetypes=[("WIM", "*.wim"), ("All", "*.*")])
        dst = _norm(dst)
        if not dst:
            return
        compress = simpledialog.askstring("Compression",
            "Compression type (None / Fast / Max / Recovery):",
            parent=self.root) or "Fast"
        self._run(["/Export-Image", f"/SourceImageFile:{src}",
                   f"/SourceIndex:{picker.chosen_index}",
                   f"/DestinationImageFile:{dst}", f"/Compress:{compress}"],
                  "Export to WIM")

    def export_to_esd(self):
        src = _ask_file(self.root, "Select source WIM",
                        [("WIM", "*.wim"), ("All", "*.*")])
        if not src:
            return
        picker = IndexPickerDialog(self.root, src)
        self.root.wait_window(picker)
        if not picker.chosen_index:
            return
        dst = filedialog.asksaveasfilename(
            title="Save as ESD", parent=self.root,
            defaultextension=".esd", filetypes=[("ESD", "*.esd"), ("All", "*.*")])
        dst = _norm(dst)
        if not dst:
            return
        self._run(["/Export-Image", f"/SourceImageFile:{src}",
                   f"/SourceIndex:{picker.chosen_index}",
                   f"/DestinationImageFile:{dst}", "/Compress:Recovery"],
                  "Export to ESD")

    def export_all_indexes(self):
        src = _ask_file(self.root, "Select source WIM",
                        [("WIM", "*.wim"), ("All", "*.*")])
        if not src:
            return
        dst_dir = _ask_dir(self.root, "Select output folder")
        if not dst_dir:
            return
        win = OutputWindow(self.root, "Export All Indexes", src, app=self)

        def _worker():
            try:
                result = subprocess.run(
                    [DISM_EXE, "/Get-ImageInfo", f"/ImageFile:{src}"],
                    capture_output=True)
                info_text = result.stdout.decode("utf-8", errors="replace")
                indexes = _parse_image_info(info_text)
                self.root.after(0, lambda: win.append(f"Found {len(indexes)} index(es)\n"))
                for item in indexes:
                    idx = item.get("index", "1")
                    name = item.get("name", f"Index_{idx}").replace(" ", "_")
                    out_file = os.path.join(dst_dir, f"{name}.wim")
                    self.root.after(0, lambda n=name: win.append(f"Exporting {n}…\n"))
                    proc = subprocess.Popen(
                        [DISM_EXE, "/Export-Image",
                         f"/SourceImageFile:{src}", f"/SourceIndex:{idx}",
                         f"/DestinationImageFile:{out_file}", "/Compress:Fast"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    for raw in proc.stdout:
                        line = raw.decode("utf-8", errors="replace")
                        self.root.after(0, lambda l=line: win.append(l))
                    proc.wait()
                self.root.after(0, lambda: win.append("All indexes exported!\n"))
            except Exception as exc:
                self.root.after(0, lambda: win.append(f"ERROR: {exc}\n"))
            self.root.after(0, win.mark_done)

        threading.Thread(target=_worker, daemon=True).start()

    def optimize_wim(self):
        img = _ask_file(self.root, "Select WIM to optimize",
                        [("WIM", "*.wim"), ("All", "*.*")])
        if not img:
            return
        self._run(["/Export-Image", f"/SourceImageFile:{img}",
                   "/SourceIndex:*",
                   f"/DestinationImageFile:{img}.opt.wim",
                   "/Compress:Fast", "/CheckIntegrity"],
                  "Optimize WIM")

    def split_wim(self):
        src = _ask_file(self.root, "Select WIM to split",
                        [("WIM", "*.wim"), ("All", "*.*")])
        if not src:
            return
        out_dir = _ask_dir(self.root, "Select output folder for .swm parts")
        if not out_dir:
            return
        size = simpledialog.askstring("Part Size",
            "Max size per part in MB (e.g. 4000 for FAT32):",
            parent=self.root) or "4000"
        base_name = os.path.splitext(os.path.basename(src))[0]
        swm_out = os.path.join(out_dir, base_name + ".swm")
        self._run(["/Split-Image", f"/ImageFile:{src}",
                   f"/SWMFile:{swm_out}", f"/FileSize:{size}"],
                  "Split WIM")

    def merge_swm(self):
        swm_dir = _ask_dir(self.root, "Select folder containing .swm parts")
        if not swm_dir:
            return
        first_swm = _ask_file(self.root, "Select first .swm part",
                               [("SWM", "*.swm"), ("All", "*.*")])
        if not first_swm:
            return
        dst = filedialog.asksaveasfilename(
            title="Save merged WIM", parent=self.root,
            defaultextension=".wim", filetypes=[("WIM", "*.wim"), ("All", "*.*")])
        dst = _norm(dst)
        if not dst:
            return
        self._run(["/Export-Image", f"/SourceImageFile:{first_swm}",
                   "/SourceIndex:1", f"/DestinationImageFile:{dst}",
                   f"/SwmFile:{swm_dir}\\*.swm", "/Compress:Fast"],
                  "Merge SWM Parts")

    def append_to_wim(self):
        src = _ask_file(self.root, "Select source WIM to append from",
                        [("WIM", "*.wim"), ("All", "*.*")])
        if not src:
            return
        picker = IndexPickerDialog(self.root, src)
        self.root.wait_window(picker)
        if not picker.chosen_index:
            return
        dst = _ask_file(self.root, "Select destination WIM (will be appended to)",
                        [("WIM", "*.wim"), ("All", "*.*")])
        if not dst:
            return
        self._run(["/Export-Image", f"/SourceImageFile:{src}",
                   f"/SourceIndex:{picker.chosen_index}",
                   f"/DestinationImageFile:{dst}", "/Compress:Fast"],
                  "Append to WIM")

    def convert_to_vhd(self):
        img = _ask_file(self.root, "Select source WIM/ESD",
                        [("Image files", "*.wim *.esd"), ("All", "*.*")])
        if not img:
            return
        picker = IndexPickerDialog(self.root, img)
        self.root.wait_window(picker)
        if not picker.chosen_index:
            return
        dst = filedialog.asksaveasfilename(
            title="Save as VHD", parent=self.root,
            defaultextension=".vhd", filetypes=[("VHD", "*.vhd"), ("All", "*.*")])
        dst = _norm(dst)
        if not dst:
            return
        sz = simpledialog.askstring("VHD Size", "VHD size in MB (e.g. 40960 = 40GB):",
                                    parent=self.root) or "40960"
        self._vhd_worker(img, picker.chosen_index, dst, "vhd", int(sz))

    def convert_to_vhdx(self):
        img = _ask_file(self.root, "Select source WIM/ESD",
                        [("Image files", "*.wim *.esd"), ("All", "*.*")])
        if not img:
            return
        picker = IndexPickerDialog(self.root, img)
        self.root.wait_window(picker)
        if not picker.chosen_index:
            return
        dst = filedialog.asksaveasfilename(
            title="Save as VHDX", parent=self.root,
            defaultextension=".vhdx", filetypes=[("VHDX", "*.vhdx"), ("All", "*.*")])
        dst = _norm(dst)
        if not dst:
            return
        sz = simpledialog.askstring("VHDX Size", "VHDX size in MB (e.g. 61440 = 60GB):",
                                    parent=self.root) or "61440"
        self._vhd_worker(img, picker.chosen_index, dst, "vhdx", int(sz))

    def mount_vhd(self):
        vhd = _ask_file(self.root, "Select VHD/VHDX to mount",
                        [("Virtual Disk", "*.vhd *.vhdx"), ("All", "*.*")])
        if not vhd:
            return
        ro = messagebox.askyesno("Read-Only?", "Mount read-only?", parent=self.root)
        cmds = [f"select vdisk file={vhd}"]
        if ro:
            cmds.append("attach vdisk readonly")
        else:
            cmds.append("attach vdisk")
        cmds.append("exit")
        self._run_diskpart_gui(cmds, "Mount VHD/VHDX")

    def get_current_edition(self):
        self._run(["/Online", "/Get-CurrentEdition"], "Get Current Edition")

    def cleanup_mountpoints(self):
        if not messagebox.askyesno("Cleanup All Mountpoints",
                "Clean up all orphaned DISM mount points?\n"
                "This removes stale entries that can't be remounted.",
                parent=self.root):
            return
        self._run(["/Cleanup-Mountpoints"], "Cleanup All Mountpoints")

    # ── DISK TOOLS ────────────────────────────────────────────────────────

    def disk_list(self):
        self._run_diskpart_gui(["list disk", "exit"], "Disk List")

    def disk_list_partitions(self):
        disk_num = self._pick_disk("Select disk to list partitions")
        if disk_num is None:
            return
        self._run_diskpart_gui([f"select disk {disk_num}", "list partition", "exit"],
                               f"Partitions: Disk {disk_num}")

    def disk_list_volumes(self):
        self._run_diskpart_gui(["list volume", "exit"], "Volume List")

    def disk_detail(self):
        disk_num = self._pick_disk("Select disk for details")
        if disk_num is None:
            return
        self._run_diskpart_gui([f"select disk {disk_num}", "detail disk", "exit"],
                               f"Disk {disk_num} Details")

    def disk_format_partition(self):
        disk_num = self._pick_disk("Select disk")
        if disk_num is None:
            return
        part_num = simpledialog.askstring("Partition Number",
            "Enter partition number to format:", parent=self.root)
        if not part_num:
            return
        fs = simpledialog.askstring("Filesystem", "Filesystem (NTFS/FAT32/exFAT):",
                                    parent=self.root) or "NTFS"
        label = simpledialog.askstring("Label", "Volume label (optional):",
                                       parent=self.root) or "DATA"
        if not messagebox.askyesno("Confirm Format",
                f"Format Disk {disk_num} Partition {part_num} as {fs}?\n"
                "ALL DATA WILL BE LOST!", parent=self.root):
            return
        self._run_diskpart_gui([
            f"select disk {disk_num}",
            f"select partition {part_num}",
            f"format fs={fs.lower()} quick label={label}",
            "exit",
        ], "Format Partition")

    def disk_assign_letter(self):
        disk_num = self._pick_disk("Select disk")
        if disk_num is None:
            return
        part_num = simpledialog.askstring("Partition", "Partition number:", parent=self.root)
        if not part_num:
            return
        letter = simpledialog.askstring("Drive Letter", "Letter to assign (e.g. E):",
                                        parent=self.root)
        if not letter:
            return
        self._run_diskpart_gui([
            f"select disk {disk_num}",
            f"select partition {part_num}",
            f"assign letter={letter}",
            "exit",
        ], "Assign Drive Letter")

    def disk_remove_letter(self):
        letter = simpledialog.askstring("Remove Letter",
            "Drive letter to remove (e.g. E):", parent=self.root)
        if not letter:
            return
        self._run_diskpart_gui([
            f"select volume {letter}",
            "remove letter=" + letter,
            "exit",
        ], "Remove Drive Letter")

    def disk_set_active(self):
        disk_num = self._pick_disk("Select disk")
        if disk_num is None:
            return
        part_num = simpledialog.askstring("Partition",
            "Partition number to set active:", parent=self.root)
        if not part_num:
            return
        self._run_diskpart_gui([
            f"select disk {disk_num}",
            f"select partition {part_num}",
            "active",
            "exit",
        ], "Set Active Partition")

    def disk_run_script(self):
        script_file = _ask_file(self.root, "Select diskpart script (.txt)",
                                [("Text", "*.txt"), ("All", "*.*")])
        if not script_file:
            return
        win = OutputWindow(self.root, "Run Diskpart Script", script_file, app=self)

        def _worker():
            try:
                proc = subprocess.Popen(
                    [DISKPART, "/s", script_file],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                for raw in proc.stdout:
                    line = raw.decode("utf-8", errors="replace")
                    self.root.after(0, lambda l=line: win.append(l))
                proc.wait()
            except Exception as exc:
                self.root.after(0, lambda: win.append(f"ERROR: {exc}\n"))
            self.root.after(0, win.mark_done)

        threading.Thread(target=_worker, daemon=True).start()

    def disk_open_console(self):
        messagebox.showinfo("Diskpart Console",
            "Opening diskpart in a separate admin console window.\n"
            "Type 'exit' when done.",
            parent=self.root)
        subprocess.Popen(["cmd.exe", "/c", "start", "diskpart"])

    # ── LOG & DIAGNOSTICS ────────────────────────────────────────────────

    def open_dism_log(self):
        if not os.path.exists(DISM_LOG):
            messagebox.showinfo("Log Not Found",
                f"DISM log not found at:\n{DISM_LOG}\n\n"
                "Run a DISM operation first.",
                parent=self.root)
            return
        win = OutputWindow(self.root, "DISM Log", DISM_LOG, app=self)
        try:
            with open(DISM_LOG, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as exc:
            content = f"Could not read log: {exc}"
        win.append(content)
        win.mark_done()

    def refresh_log(self):
        if not hasattr(self, "_log_text"):
            return
        try:
            if os.path.exists(DISM_LOG):
                with open(DISM_LOG, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            else:
                content = f"Log not found: {DISM_LOG}\nRun a DISM operation first.\n"
            self._log_text.configure(state="normal")
            self._log_text.delete("1.0", "end")
            for line in content.splitlines(keepends=True):
                lo = line.lower()
                if any(w in lo for w in ("error", "fail", "0x8")):
                    tag = "err"
                elif "warning" in lo:
                    tag = "warn"
                elif any(w in lo for w in ("success", "complete")):
                    tag = "ok"
                else:
                    tag = ""
                self._log_text.insert("end", line, tag)
            self._log_text.see("end")
            self._log_text.configure(state="disabled")
        except Exception:
            pass

    def clear_dism_log(self):
        if not messagebox.askyesno("Clear DISM Log",
                f"Delete DISM log at:\n{DISM_LOG}\n\nContinue?",
                parent=self.root):
            return
        try:
            if os.path.exists(DISM_LOG):
                os.remove(DISM_LOG)
            messagebox.showinfo("Done", "DISM log cleared.", parent=self.root)
        except Exception as exc:
            messagebox.showerror("Error", str(exc), parent=self.root)

    def check_online_health(self):
        self._run(["/Online", "/Cleanup-Image", "/CheckHealth"],
                  "Check Online Health")

    def analyse_online_health(self):
        self._run(["/Online", "/Cleanup-Image", "/ScanHealth"],
                  "Analyse Online Health (ScanHealth)")


    # ── Feature batch helpers (called by FeatureManagerDialog) ───────────
    def _batch_enable_features(self, features: list, enable_deps: bool = True):
        mp = _norm(self.mount_point.get().strip())
        if not mp:
            return
        if len(features) == 1:
            args = [f"/Image:{mp}", "/Enable-Feature",
                    f"/FeatureName:{features[0]}"]
            if enable_deps:
                args.append("/All")
            self._run(args, f"Enable: {features[0]}")
            return
        win = OutputWindow(self.root, "Enable Features (batch)",
                           f"{len(features)} feature(s)", app=self)

        def _worker():
            for i, feat in enumerate(features, 1):
                self.root.after(0, lambda f=feat, n=i, t=len(features):
                               win.append(f"\n[{n}/{t}] Enabling: {f}\n"))
                args = [DISM_EXE, f"/Image:{mp}", "/Enable-Feature",
                        f"/FeatureName:{feat}"]
                if enable_deps:
                    args.append("/All")
                proc = subprocess.Popen(
                    args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                for raw in proc.stdout:
                    line = raw.decode("utf-8", errors="replace")
                    self.root.after(0, lambda l=line: win.append(l))
                proc.wait()
            self.root.after(0, lambda: win.append("\nAll enable operations complete.\n"))
            self.root.after(0, win.mark_done)

        threading.Thread(target=_worker, daemon=True).start()

    def _batch_disable_features(self, features: list):
        mp = _norm(self.mount_point.get().strip())
        if not mp:
            return
        if len(features) == 1:
            self._run([f"/Image:{mp}", "/Disable-Feature",
                       f"/FeatureName:{features[0]}"],
                      f"Disable: {features[0]}")
            return
        win = OutputWindow(self.root, "Disable Features (batch)",
                           f"{len(features)} feature(s)", app=self)

        def _worker():
            for i, feat in enumerate(features, 1):
                self.root.after(0, lambda f=feat, n=i, t=len(features):
                               win.append(f"\n[{n}/{t}] Disabling: {f}\n"))
                proc = subprocess.Popen(
                    [DISM_EXE, f"/Image:{mp}", "/Disable-Feature",
                     f"/FeatureName:{feat}"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                for raw in proc.stdout:
                    line = raw.decode("utf-8", errors="replace")
                    self.root.after(0, lambda l=line: win.append(l))
                proc.wait()
            self.root.after(0, lambda: win.append("\nAll disable operations complete.\n"))
            self.root.after(0, win.mark_done)

        threading.Thread(target=_worker, daemon=True).start()

    # ── Keyboard shortcuts ────────────────────────────────────────────────
    def _bind_shortcuts(self):
        self.root.bind("<Control-m>", lambda e: self.mount_image())
        self.root.bind("<Control-u>", lambda e: self.commit_unmount_image())
        self.root.bind("<Control-d>", lambda e: self.unmount_image_discard())
        self.root.bind("<Control-l>", lambda e: self.list_mounted_images())
        self.root.bind("<Control-k>", lambda e: self.check_mount_status())
        self.root.bind("<F5>",        lambda e: self.refresh_log())

    # ── Auto-detect mounted images on startup ─────────────────────────────
    def _auto_detect_mounts(self):
        mounts = _get_mounted_images()
        if not mounts:
            return
        mp_list = list(mounts.keys())
        if not self.mount_point.get() and mp_list:
            mp = mp_list[0]
            info = mounts[mp]
            self.mount_point.set(mp)
            self._mount_dot.configure(fg=FG_GREEN)
            wim_name = os.path.basename(info.get("wim", "?"))
            self.status_var.set(
                f"Auto-detected: {wim_name} [idx {info.get('index','?')}] @ {mp}")
        elif mp_list:
            self._mount_dot.configure(fg=FG_YELLOW)

    def _set_mount_dot(self, mounted: bool):
        clr = FG_GREEN if mounted else FG_DIM
        self.root.after(0, lambda: self._mount_dot.configure(fg=clr))

    # ── History tab ───────────────────────────────────────────────────────
    def _build_tab_history(self, nb):
        p = tk.Frame(nb, bg=BG_DARK)
        nb.add(p, text="  🕐  History  ")
        p.columnconfigure(0, weight=1)
        p.rowconfigure(1, weight=1)

        btn_row = tk.Frame(p, bg=BG_FRAME)
        btn_row.grid(row=0, column=0, sticky="ew")
        _mk_btn(btn_row, "Clear History",   self._clear_history).pack(side="left", padx=4, pady=4)
        _mk_btn(btn_row, "Copy All",        self._copy_history).pack(side="left", padx=4, pady=4)
        _mk_btn(btn_row, "Recent Images…",  self._show_recent_images).pack(side="right", padx=4, pady=4)
        _mk_btn(btn_row, "Recent Mounts…",  self._show_recent_mounts).pack(side="right", padx=4, pady=4)

        self._hist_text = scrolledtext.ScrolledText(
            p, bg="#0d0d0d", fg=FG_TEXT, font=("Consolas", 8),
            wrap="word", relief="flat", bd=0,
            selectbackground="#1e3a1e")
        self._hist_text.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        self._hist_text.tag_configure("ts",  foreground=FG_DIM)
        self._hist_text.tag_configure("cmd", foreground=FG_YELLOW)
        self._hist_text.tag_configure("ok",  foreground=FG_GREEN)
        self._hist_text.tag_configure("err", foreground=FG_RED)

    def _log_history(self, cmd_line: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._cmd_history.append({"ts": ts, "cmd": cmd_line})
        if hasattr(self, "_hist_text"):
            self._hist_text.configure(state="normal")
            self._hist_text.insert("end", f"[{ts}] ", "ts")
            self._hist_text.insert("end", cmd_line + "\n", "cmd")
            self._hist_text.see("end")
            self._hist_text.configure(state="disabled")

    def _mark_hist_done(self, ok: bool):
        if not hasattr(self, "_hist_text"):
            return
        tag = "ok" if ok else "err"
        label = "  ✓ done\n" if ok else "  ✗ failed\n"
        self._hist_text.configure(state="normal")
        self._hist_text.insert("end", label, tag)
        self._hist_text.see("end")
        self._hist_text.configure(state="disabled")

    def _clear_history(self):
        self._cmd_history.clear()
        if hasattr(self, "_hist_text"):
            self._hist_text.configure(state="normal")
            self._hist_text.delete("1.0", "end")
            self._hist_text.configure(state="disabled")

    def _copy_history(self):
        lines = "\n".join(f"[{h['ts']}] {h['cmd']}" for h in self._cmd_history)
        self.root.clipboard_clear()
        self.root.clipboard_append(lines)

    def _show_recent_images(self):
        imgs = self._recent.get("images", [])
        if not imgs:
            messagebox.showinfo("No Recent Images", "No recently used image files.", parent=self.root)
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Recent Images")
        dlg.geometry("680x300")
        dlg.configure(bg=BG_DARK)
        dlg.grab_set()
        tk.Label(dlg, text="Double-click or select + Mount to mount a recent image:",
                 bg=BG_DARK, fg=FG_TEXT, font=("Segoe UI", 9)).pack(fill="x", padx=10, pady=6)
        lb = tk.Listbox(dlg, bg="#1a1a1a", fg=FG_TEXT, font=("Consolas", 8),
                        selectbackground="#1e4a1e", relief="flat", bd=0)
        lb.pack(fill="both", expand=True, padx=8, pady=4)
        for img in imgs:
            lb.insert("end", img)
        def _use(_e=None):
            sel = lb.curselection()
            if not sel:
                return
            img = lb.get(sel[0])
            dlg.destroy()
            mp = _ask_dir(self.root, "Select (empty) mount point directory")
            if not mp:
                return
            picker = IndexPickerDialog(self.root, img)
            self.root.wait_window(picker)
            if picker.chosen_index:
                self.mount_point.set(mp)
                self._run(["/Mount-Wim", f"/WimFile:{img}",
                           f"/index:{picker.chosen_index}", f"/MountDir:{mp}"],
                          f"Mount Image (recent) [idx {picker.chosen_index}]")
        lb.bind("<Double-1>", _use)
        br = tk.Frame(dlg, bg=BG_DARK)
        br.pack(fill="x", padx=8, pady=6)
        _mk_btn(br, "Mount Selected", _use).pack(side="right", padx=4)
        _mk_btn(br, "Cancel", dlg.destroy).pack(side="right", padx=4)

    def _show_recent_mounts(self):
        mps = self._recent.get("mounts", [])
        if not mps:
            messagebox.showinfo("No Recent Mounts", "No recently used mount points.", parent=self.root)
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Recent Mount Points")
        dlg.geometry("580x260")
        dlg.configure(bg=BG_DARK)
        dlg.grab_set()
        tk.Label(dlg, text="Select a mount point to set it as the active mount point:",
                 bg=BG_DARK, fg=FG_TEXT, font=("Segoe UI", 9)).pack(fill="x", padx=10, pady=6)
        lb = tk.Listbox(dlg, bg="#1a1a1a", fg=FG_TEXT, font=("Consolas", 8),
                        selectbackground="#1e4a1e", relief="flat", bd=0)
        lb.pack(fill="both", expand=True, padx=8, pady=4)
        for mp in mps:
            lb.insert("end", mp)
        def _use(_e=None):
            sel = lb.curselection()
            if sel:
                self.mount_point.set(lb.get(sel[0]))
                dlg.destroy()
        lb.bind("<Double-1>", _use)
        br = tk.Frame(dlg, bg=BG_DARK)
        br.pack(fill="x", padx=8, pady=6)
        _mk_btn(br, "Use Selected", _use).pack(side="right", padx=4)
        _mk_btn(br, "Cancel", dlg.destroy).pack(side="right", padx=4)

    # ── Batch & Bulk operations ───────────────────────────────────────────
    def batch_add_packages(self):
        mp = self._mp()
        if not mp:
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Batch Install Packages")
        dlg.geometry("700x440")
        dlg.configure(bg=BG_DARK)
        dlg.grab_set()
        tk.Label(dlg, text="Select .cab / .msu packages — they will be installed in order:",
                 bg=BG_DARK, fg=FG_TEXT, font=("Segoe UI", 9)).pack(fill="x", padx=10, pady=6)
        frm = tk.Frame(dlg, bg=BG_DARK)
        frm.pack(fill="both", expand=True, padx=8, pady=4)
        vsb = ttk.Scrollbar(frm, orient="vertical")
        lb = tk.Listbox(frm, bg="#1a1a1a", fg=FG_TEXT, font=("Consolas", 8),
                        selectbackground="#1e4a1e", relief="flat", bd=0,
                        selectmode="extended", yscrollcommand=vsb.set)
        vsb.configure(command=lb.yview)
        lb.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        def _add():
            files = filedialog.askopenfilenames(
                title="Select packages", parent=dlg,
                filetypes=[("Packages", "*.cab *.msu"), ("All", "*.*")])
            for f in files:
                lb.insert("end", _norm(f))
        def _remove():
            for i in reversed(lb.curselection()):
                lb.delete(i)
        def _run_batch():
            pkgs = list(lb.get(0, "end"))
            if not pkgs:
                messagebox.showwarning("Empty List", "Add at least one package.", parent=dlg)
                return
            dlg.destroy()
            win = OutputWindow(self.root, "Batch Install Packages",
                               f"{len(pkgs)} package(s) → {mp}", app=self)
            def _worker():
                for i, pkg in enumerate(pkgs, 1):
                    self.root.after(0, lambda p=pkg, n=i, t=len(pkgs):
                                   win.append(f"\n[{n}/{t}] Installing: {os.path.basename(p)}\n"))
                    proc = subprocess.Popen(
                        [DISM_EXE, f"/Image:{mp}", "/Add-Package",
                         f"/PackagePath:{pkg}"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    for raw in proc.stdout:
                        line = raw.decode("utf-8", errors="replace")
                        self.root.after(0, lambda l=line: win.append(l))
                    proc.wait()
                self.root.after(0, lambda: win.append("\nAll packages processed.\n"))
                self.root.after(0, win.mark_done)
            threading.Thread(target=_worker, daemon=True).start()
        br = tk.Frame(dlg, bg=BG_DARK)
        br.pack(fill="x", padx=8, pady=6)
        _mk_btn(br, "Add Files…",  _add).pack(side="left", padx=4)
        _mk_btn(br, "Remove",      _remove).pack(side="left", padx=4)
        _mk_btn(br, "Install All", _run_batch).pack(side="right", padx=4)
        _mk_btn(br, "Cancel",      dlg.destroy).pack(side="right", padx=4)

    def bulk_remove_appx(self):
        mp = self._mp()
        if not mp:
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Bulk Remove AppX Packages")
        dlg.geometry("760x520")
        dlg.configure(bg=BG_DARK)
        dlg.grab_set()
        status_lbl = tk.Label(dlg, text="Querying provisioned packages — please wait…",
                              bg=BG_DARK, fg=FG_YELLOW, font=("Segoe UI", 9))
        status_lbl.pack(fill="x", padx=10, pady=6)
        frm = tk.Frame(dlg, bg=BG_DARK)
        frm.pack(fill="both", expand=True, padx=8, pady=4)
        vsb = ttk.Scrollbar(frm, orient="vertical")
        lb = tk.Listbox(frm, bg="#1a1a1a", fg=FG_TEXT, font=("Consolas", 8),
                        selectbackground="#1e4a1e", relief="flat", bd=0,
                        selectmode="extended", yscrollcommand=vsb.set)
        vsb.configure(command=lb.yview)
        lb.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        def _load():
            r = subprocess.run(
                [DISM_EXE, f"/Image:{mp}", "/Get-ProvisionedAppxPackages"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW)
            out = r.stdout.decode("utf-8", errors="replace")
            packages = []
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("PackageName :"):
                    packages.append(line.split(":", 1)[1].strip())
            dlg.after(0, lambda: _populate(sorted(packages)))

        def _populate(packages):
            lb.delete(0, "end")
            for p in packages:
                lb.insert("end", p)
            status_lbl.configure(
                text=f"{len(packages)} provisioned package(s) — Ctrl+A to select all, then Remove Selected",
                fg=FG_GREEN if packages else FG_RED)

        threading.Thread(target=_load, daemon=True).start()

        def _remove_selected():
            selected = [lb.get(i) for i in lb.curselection()]
            if not selected:
                messagebox.showwarning("No Selection", "Select packages to remove.", parent=dlg)
                return
            if not messagebox.askyesno("Confirm Removal",
                    f"Remove {len(selected)} package(s) from the image?\n\nThis cannot be undone.",
                    parent=dlg):
                return
            dlg.destroy()
            win = OutputWindow(self.root, "Bulk Remove AppX",
                               f"{len(selected)} package(s)", app=self)
            def _worker():
                for i, pkg in enumerate(selected, 1):
                    self.root.after(0, lambda p=pkg, n=i, t=len(selected):
                                   win.append(f"\n[{n}/{t}] Removing: {p}\n"))
                    proc = subprocess.Popen(
                        [DISM_EXE, f"/Image:{mp}",
                         "/Remove-ProvisionedAppxPackage", f"/PackageName:{pkg}"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    for raw in proc.stdout:
                        line = raw.decode("utf-8", errors="replace")
                        self.root.after(0, lambda l=line: win.append(l))
                    proc.wait()
                self.root.after(0, lambda: win.append("\nAll removals complete.\n"))
                self.root.after(0, win.mark_done)
            threading.Thread(target=_worker, daemon=True).start()

        lb.bind("<Control-a>", lambda e: lb.select_set(0, "end"))
        br = tk.Frame(dlg, bg=BG_DARK)
        br.pack(fill="x", padx=8, pady=6)
        _mk_btn(br, "Select All",      lambda: lb.select_set(0, "end")).pack(side="left", padx=4)
        _mk_btn(br, "Deselect All",    lambda: lb.selection_clear(0, "end")).pack(side="left", padx=4)
        _mk_btn(br, "Remove Selected", _remove_selected).pack(side="right", padx=4)
        _mk_btn(br, "Cancel",          dlg.destroy).pack(side="right", padx=4)

    # ── WIM Integrity ─────────────────────────────────────────────────────
    def check_wim_integrity(self):
        img = _ask_file(self.root, "Select WIM/ESD to verify",
                        [("Image files", "*.wim *.esd"), ("All", "*.*")])
        if not img:
            return
        self._run(["/Get-ImageInfo", f"/ImageFile:{img}", "/CheckIntegrity"],
                  "Check WIM Integrity")

    # ── Online health ─────────────────────────────────────────────────────
    def restore_online_health(self):
        if not messagebox.askyesno("Restore Online Health",
                "Run DISM /Online /Cleanup-Image /RestoreHealth?\n\n"
                "Repairs the Windows component store from Windows Update.\n"
                "Requires internet access — may take 10–30 minutes.",
                parent=self.root):
            return
        self._run(["/Online", "/Cleanup-Image", "/RestoreHealth"],
                  "Restore Online Health (Windows Update)")

    # ── Registry hive operations ──────────────────────────────────────────
    def load_reg_hive(self):
        mp = self._mp()
        if not mp:
            return
        hive_choices = [
            ("SYSTEM",   "Windows\\System32\\config\\SYSTEM"),
            ("SOFTWARE", "Windows\\System32\\config\\SOFTWARE"),
            ("DEFAULT",  "Windows\\System32\\config\\DEFAULT"),
            ("SAM",      "Windows\\System32\\config\\SAM"),
            ("SECURITY", "Windows\\System32\\config\\SECURITY"),
            ("NTUSER.DAT (Default User)", "Users\\Default\\NTUSER.DAT"),
        ]
        dlg = tk.Toplevel(self.root)
        dlg.title("Load Offline Registry Hive")
        dlg.geometry("520x300")
        dlg.configure(bg=BG_DARK)
        dlg.grab_set()
        tk.Label(dlg,
                 text="Select a hive to load from the mounted image into HKLM for editing:",
                 bg=BG_DARK, fg=FG_TEXT, font=("Segoe UI", 9),
                 wraplength=480, justify="left").pack(fill="x", padx=10, pady=8)
        lb = tk.Listbox(dlg, bg="#1a1a1a", fg=FG_TEXT, font=("Consolas", 9),
                        selectbackground="#1e4a1e", relief="flat", bd=0)
        lb.pack(fill="both", expand=True, padx=8, pady=4)
        for name, _ in hive_choices:
            lb.insert("end", name)
        lb.select_set(0)
        def _load():
            sel = lb.curselection()
            if not sel:
                return
            name, rel_path = hive_choices[sel[0]]
            key_name = f"OFFLINE_{name.split()[0]}"
            hive_path = os.path.join(mp, rel_path)
            dlg.destroy()
            win = OutputWindow(self.root, f"Load Hive: {name}",
                               f"reg load HKLM\\{key_name} …", app=self)
            def _worker():
                result = subprocess.run(
                    ["reg", "load", f"HKLM\\{key_name}", hive_path],
                    capture_output=True, text=True)
                msg = result.stdout + result.stderr
                suffix = (f"\nHive loaded as HKLM\\{key_name}\n"
                          "Use regedit to browse/edit it.\n"
                          "Run 'Unload registry hive' before unmounting.\n"
                          if result.returncode == 0
                          else "\nFailed to load hive — check path and permissions.\n")
                self.root.after(0, lambda: win.append(msg + suffix))
                self.root.after(0, win.mark_done)
            threading.Thread(target=_worker, daemon=True).start()
        br = tk.Frame(dlg, bg=BG_DARK)
        br.pack(fill="x", padx=8, pady=8)
        _mk_btn(br, "Load Hive", _load).pack(side="right", padx=4)
        _mk_btn(br, "Cancel",    dlg.destroy).pack(side="right", padx=4)

    def unload_reg_hive(self):
        key = simpledialog.askstring("Unload Registry Hive",
            "Key name to unload (e.g. OFFLINE_SYSTEM — the part after HKLM\\):",
            parent=self.root)
        if not key:
            return
        win = OutputWindow(self.root, "Unload Registry Hive",
                           f"reg unload HKLM\\{key}", app=self)
        def _worker():
            result = subprocess.run(
                ["reg", "unload", f"HKLM\\{key}"],
                capture_output=True, text=True)
            self.root.after(0, lambda: win.append(result.stdout + result.stderr))
            self.root.after(0, win.mark_done)
        threading.Thread(target=_worker, daemon=True).start()

    # ── ISO creation ──────────────────────────────────────────────────────
    def create_iso(self):
        if not OSCDIMG or not os.path.isfile(OSCDIMG):
            messagebox.showwarning("oscdimg not found",
                "oscdimg.exe could not be found automatically.\n\n"
                "It is part of the Windows ADK — Deployment Tools component.\n\n"
                "How to get it:\n"
                "  1. Go to microsoft.com and search 'Download Windows ADK'\n"
                "  2. Run the installer and tick only 'Deployment Tools'\n"
                "  3. Default install path:\n"
                "     C:\\Program Files (x86)\\Windows Kits\\10\\\n"
                "       Assessment and Deployment Kit\\\n"
                "       Deployment Tools\\amd64\\Oscdimg\\oscdimg.exe",
                parent=self.root)
            return
        src = _ask_dir(self.root, "Select source folder (Windows files to package)")
        if not src:
            return
        out = filedialog.asksaveasfilename(
            title="Save ISO as", parent=self.root,
            defaultextension=".iso",
            filetypes=[("ISO image", "*.iso"), ("All", "*.*")])
        out = _norm(out)
        if not out:
            return
        osc_dir = os.path.dirname(OSCDIMG)
        boot    = os.path.join(osc_dir, "etfsboot.com")
        efi     = os.path.join(osc_dir, "efisys.bin")
        if os.path.isfile(boot) and os.path.isfile(efi):
            bootdata = f"2#p0,e,b{boot}#pEF,e,b{efi}"
            args = ["-m", "-o", "-u2", "-udfver102", f"-bootdata:{bootdata}", src, out]
        else:
            args = ["-m", "-o", "-u2", src, out]
        self._run_cmd(OSCDIMG, args, "Create Bootable ISO")

    # ── Aliases for UI button callbacks ──────────────────────────────────
    def get_package_info(self):    self.get_packages()
    def save_package_info(self):   self.save_packages()
    def get_feature_info(self):    self.get_features()
    def save_feature_info(self):   self.save_features()
    def get_appx_info(self):       self.get_appx_packages()
    def save_appx_info(self):      self.save_appx_packages()
    def get_capability_info(self): self.get_capabilities()
    def save_capability_info(self): self.save_capabilities()
    def get_driver_info(self):     self.get_drivers()
    def save_driver_info(self):    self.save_drivers()
    def pe_save_config(self):      self.pe_get_config()


if __name__ == "__main__":
    import tkinter as tk
    root = tk.Tk()
    DismTool(root)
    root.mainloop()
