"""Anime Tracker — nyaa.si + qBittorrent"""
from __future__ import annotations
import json, os, sys, time, threading, logging, subprocess
from datetime import datetime, timedelta
from urllib.parse import quote

import customtkinter as ctk

BASE_DIR      = os.path.dirname(os.path.abspath(sys.executable if getattr(sys,"frozen",False) else __file__))
DATA_PATH     = os.path.join(BASE_DIR, "shows.json")
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")
LOG_PATH      = os.path.join(BASE_DIR, "tracker.log")

logging.basicConfig(filename=LOG_PATH, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

DAYS = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

# "default_app" hands the magnet link to whatever torrent client is registered
# with Windows — works with qBittorrent, Deluge, Transmission, uTorrent, etc.
# "qbittorrent" adds silently via the Web UI API (supports category/save path).
DEFAULT_SETTINGS = {
    "download_mode": "default_app",
    "qbit_host": "http://localhost:8080",
    "qbit_user": "admin",
    "qbit_pass": "",
    "qbit_save_path": "",
    "qbit_category": "Anime",
    "window_geometry": "1020x420",
}

# Last col minsize tuned so all buttons fit without overflow
COLS = [
    ("Show Name",     155, 0),
    ("Schedule",      108, 0),
    ("Group",          52, 0),
    ("Res",            46, 0),
    ("Uploaded",      105, 0),
    ("Latest Episode",  0, 1),
    ("",              230, 0),
]

_save_lock = threading.Lock()

# ── Data helpers ───────────────────────────────────────────────────────────
def load_shows():
    if os.path.exists(DATA_PATH):
        try:
            with open(DATA_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_shows(shows):
    with _save_lock:
        with open(DATA_PATH, "w") as f:
            json.dump(shows, f, indent=2)

def load_settings():
    s = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH) as f:
                s.update(json.load(f))
        except Exception:
            pass
    return s

def save_settings(s):
    with open(SETTINGS_PATH, "w") as f:
        json.dump(s, f, indent=2)

def empty_show(name="", day="Monday", check_time="18:00", resolution="1080p", group="ASW"):
    return {"name": name, "day": day, "check_time": check_time,
            "resolution": resolution, "group": group,
            "nyaa_time": None, "latest_title": None,
            "latest_link": None, "latest_magnet": None, "status": "Not checked"}

def add_hours(day: str, t: str, h: int):
    base = datetime.strptime(t, "%H:%M")
    s    = base + timedelta(hours=h)
    off  = 0 if s.date() == base.date() else 1
    return DAYS[(DAYS.index(day) + off) % 7], s.strftime("%H:%M")

def _magnet(e) -> str:
    h = e.get("nyaa_infohash", "")
    if h:
        dn = quote(e.get("title", ""))
        return (f"magnet:?xt=urn:btih:{h}&dn={dn}"
                "&tr=http%3A%2F%2Fnyaa.tracker.wf%3A7777%2Fannounce"
                "&tr=udp%3A%2F%2Fopen.stealth.si%3A80%2Fannounce"
                "&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce")
    return e.get("link", "")

def search_nyaa(show_name: str, resolution="1080p", group="ASW"):
    """Returns a list of results, [] when nothing matched,
    or None when nyaa.si was unreachable (network/parse failure)."""
    import feedparser
    q   = f"{group} {show_name}" + (f" {resolution}" if resolution.lower() != "any" else "")
    url = f"https://nyaa.si/?page=rss&q={quote(q)}&c=1_2&f=0"
    try:
        feed = feedparser.parse(url)
        if getattr(feed, "bozo", False) and not feed.entries:
            logging.error("search_nyaa unreachable: %s", getattr(feed, "bozo_exception", ""))
            return None
        results = []
        for e in feed.entries:
            pub = e.get("published_parsed")
            ts  = ""
            if pub:
                utc = datetime(*pub[:6])
                loc = utc + timedelta(seconds=-(time.timezone if not time.daylight else time.altzone))
                ts  = loc.strftime("%b %d  %H:%M")
            results.append({"title": e.get("title",""), "link": e.get("link",""),
                            "nyaa_infohash": e.get("nyaa_infohash",""),
                            "nyaa_time": ts, "magnet": _magnet(e)})
        return results
    except Exception as ex:
        logging.error("search_nyaa: %s", ex)
        return None


def apply_result(show: dict, results) -> bool:
    """Update show from search results. Returns True if a NEW episode appeared."""
    if results is None:
        show["status"] = "Network error"
        return False
    if not results:
        show["status"] = "Not found"
        return False
    top = results[0]
    is_new = top["title"] != show.get("downloaded_title") and top["title"] != show.get("latest_title")
    show.update({"latest_title": top["title"], "latest_link": top["link"],
                 "latest_magnet": top["magnet"], "nyaa_time": top["nyaa_time"],
                 "status": "Found"})
    return is_new

# ── qBittorrent ────────────────────────────────────────────────────────────
_QBIT_PATHS = [
    r"C:\Program Files\qBittorrent\qbittorrent.exe",
    r"C:\Program Files (x86)\qBittorrent\qbittorrent.exe",
    os.path.join(os.environ.get("LOCALAPPDATA",""), "Programs","qBittorrent","qbittorrent.exe"),
    os.path.join(os.environ.get("APPDATA",""), "qBittorrent","qbittorrent.exe"),
]

def launch_qbit(settings: dict) -> bool:
    host = settings.get("qbit_host","")
    if "localhost" not in host and "127.0.0.1" not in host:
        return False
    for p in _QBIT_PATHS:
        if os.path.exists(p):
            subprocess.Popen([p])
            return True
    try:
        r = subprocess.check_output(["where","qbittorrent"], text=True,
                                    stderr=subprocess.DEVNULL).strip()
        if r:
            subprocess.Popen([r.splitlines()[0]])
            return True
    except Exception:
        pass
    return False

def qbit_add(link: str, settings: dict) -> str:
    import requests
    host = settings.get("qbit_host","http://localhost:8080").rstrip("/")
    sess = requests.Session()
    try:
        r = sess.post(f"{host}/api/v2/auth/login",
                      data={"username": settings.get("qbit_user",""),
                            "password": settings.get("qbit_pass","")}, timeout=5)
        if r.text.strip() not in ("Ok.","Ok"):
            return f"Login failed: {r.text.strip()}"
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return "connection_error"
    except Exception as ex:
        return str(ex)
    data = {"urls": link}
    if settings.get("qbit_save_path"): data["savepath"] = settings["qbit_save_path"]
    if settings.get("qbit_category"):  data["category"]  = settings["qbit_category"]
    try:
        r2 = sess.post(f"{host}/api/v2/torrents/add", data=data, timeout=10)
        return "" if r2.status_code == 200 else f"HTTP {r2.status_code}"
    except Exception as ex:
        return str(ex)

def _prewarm():
    try:
        import feedparser  # noqa
        import requests    # noqa
    except Exception:
        pass


# ── GUI ────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

def _ep_color(status: str) -> str:
    if status == "Found":         return "#4ec94e"
    if status == "Not checked":   return "gray55"
    if status == "Network error": return "#e0b050"
    return "#e08080"

def _ep_text(show: dict) -> str:
    title = show.get("latest_title")
    if not title:
        return show.get("status", "")
    if show.get("status") == "Network error":
        return f"⚠ nyaa.si unreachable — last known: {title}"
    if title != show.get("downloaded_title"):
        return f"● NEW   {title}"
    return f"✓ {title}"


class ConfirmDialog(ctk.CTkToplevel):
    def __init__(self, master, msg: str, on_confirm=None):
        super().__init__(master)
        self.title("Confirm")
        self.resizable(False, False)
        self.grab_set()
        self.bind("<Escape>", lambda e: self.destroy())
        ctk.CTkLabel(self, text=msg, font=ctk.CTkFont(size=13),
                     wraplength=300).pack(padx=24, pady=(20,12))
        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack(padx=24, pady=(0,16))
        def _ok():
            self.destroy()
            if on_confirm: on_confirm()
        ctk.CTkButton(bf, text="Remove", width=90, fg_color="#8b2020",
                      hover_color="#a03030", command=_ok).pack(side="left", padx=6)
        ctk.CTkButton(bf, text="Cancel", width=90, fg_color="gray30",
                      hover_color="gray40", command=self.destroy).pack(side="left", padx=6)
        self.update_idletasks()
        x = master.winfo_x() + (master.winfo_width()  - self.winfo_width())  // 2
        y = master.winfo_y() + (master.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")


class AddShowDialog(ctk.CTkToplevel):
    def __init__(self, master, show=None, on_save=None):
        super().__init__(master)
        self.title("Edit Show" if show else "Add Show")
        self.resizable(False, False)
        self.on_save = on_save
        self.grab_set()
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Return>",  lambda e: self._save())
        p = {"padx":12, "pady":6}
        rows = [("Show Name:", "_name", None, 290),
                ("Day:",        "_day",  DAYS,  160),
                ("Time (HH:MM):","_time",None,  100),
                ("Resolution:", "_res",  ["1080p","720p","480p","any"], 120),
                ("Group:",      "_grp",  None,  160)]
        for i,(lbl,attr,opts,w) in enumerate(rows):
            ctk.CTkLabel(self, text=lbl).grid(row=i, column=0, sticky="w", **p)
            if opts:
                widget = ctk.CTkComboBox(self, values=opts, width=w)
                widget.set((show or {}).get(attr.strip("_").replace("grp","group")
                            .replace("res","resolution").replace("day","day")
                            .replace("time","check_time").replace("name","name"), opts[0]))
            else:
                widget = ctk.CTkEntry(self, width=w)
                key    = {"_name":"name","_time":"check_time","_grp":"group"}.get(attr, attr.strip("_"))
                widget.insert(0, (show or {}).get(key,"ASW" if key=="group" else
                              ("18:00" if key=="check_time" else "")))
            widget.grid(row=i, column=1, sticky="w", **p)
            setattr(self, attr, widget)
        if show: self._name.delete(0,"end"); self._name.insert(0, show.get("name",""))
        self._name.focus()
        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.grid(row=len(rows), column=0, columnspan=2, pady=12)
        ctk.CTkButton(bf, text="Save",   width=100, command=self._save).pack(side="left", padx=6)
        ctk.CTkButton(bf, text="Cancel", width=80, fg_color="gray30",
                      hover_color="gray40", command=self.destroy).pack(side="left", padx=6)

    def _save(self):
        name = self._name.get().strip()
        t    = self._time.get().strip()
        if not name:
            self._name.configure(border_color="red"); return
        try: datetime.strptime(t, "%H:%M")
        except ValueError: t = "18:00"
        if self.on_save:
            self.on_save({"name": name, "day": self._day.get(), "check_time": t,
                          "resolution": self._res.get(),
                          "group": self._grp.get().strip() or "ASW"})
        self.destroy()


MODE_LABELS = {
    "default_app": "Default torrent app  (works with any client)",
    "qbittorrent": "qBittorrent Web UI  (silent add, category, save path)",
}
MODE_VALUES = {v: k for k, v in MODE_LABELS.items()}
MODE_VALUES["magnet"] = "default_app"  # legacy value


class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, master, settings, on_save=None):
        super().__init__(master)
        self.title("Settings")
        self.resizable(False, False)
        self.on_save = on_save
        self.grab_set()
        self.bind("<Escape>", lambda e: self.destroy())
        p = {"padx":12, "pady":6}

        ctk.CTkLabel(self, text="Send downloads to:").grid(row=0, column=0, sticky="w", **p)
        self._mode = ctk.CTkComboBox(self, values=list(MODE_LABELS.values()), width=340,
                                     command=lambda _: self._toggle_qbit())
        cur = settings.get("download_mode", "default_app")
        cur = MODE_VALUES.get(cur, cur) if cur not in MODE_LABELS else cur
        self._mode.set(MODE_LABELS.get(cur, MODE_LABELS["default_app"]))
        self._mode.grid(row=0, column=1, sticky="w", **p)

        self._hint = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=11),
                                  text_color="gray55", wraplength=460, justify="left")
        self._hint.grid(row=1, column=0, columnspan=2, sticky="w", padx=12)

        qbit_fields = [("Web UI address","qbit_host"), ("Username","qbit_user"),
                       ("Password","qbit_pass"), ("Save files to (optional)","qbit_save_path"),
                       ("Category (optional)","qbit_category")]
        self._vars, self._qbit_rows = {}, []
        for i,(lbl,key) in enumerate(qbit_fields, start=2):
            l = ctk.CTkLabel(self, text=lbl+":")
            l.grid(row=i, column=0, sticky="w", **p)
            w = ctk.CTkEntry(self, width=280, show="*" if key == "qbit_pass" else "")
            w.insert(0, settings.get(key,""))
            w.grid(row=i, column=1, sticky="w", **p)
            self._vars[key] = w
            self._qbit_rows.append((l, w))

        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.grid(row=2+len(qbit_fields), column=0, columnspan=2, pady=12)
        ctk.CTkButton(bf, text="Save",   width=100, command=self._save).pack(side="left", padx=6)
        ctk.CTkButton(bf, text="Cancel", width=80, fg_color="gray30",
                      hover_color="gray40", command=self.destroy).pack(side="left", padx=6)
        self._toggle_qbit()

    def _toggle_qbit(self):
        is_qbit = MODE_VALUES.get(self._mode.get()) == "qbittorrent"
        for l, w in self._qbit_rows:
            l.configure(text_color=("gray10","gray90") if is_qbit else "gray40")
            w.configure(state="normal" if is_qbit else "disabled")
        self._hint.configure(text=
            "Adds torrents silently via qBittorrent's API. Enable it first: qBittorrent → "
            "Tools → Options → Web UI → check \"Web User Interface\"."
            if is_qbit else
            "Download opens the magnet link with whatever torrent client is installed — "
            "qBittorrent, Deluge, Transmission, uTorrent… No setup needed.")

    def _save(self):
        data = {k: w.get() for k, w in self._vars.items()}
        data["download_mode"] = MODE_VALUES.get(self._mode.get(), "default_app")
        if self.on_save: self.on_save(data)
        self.destroy()


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Anime Tracker — nyaa.si")
        self.geometry("1020x420")
        self.minsize(860, 280)
        self._shows        = []
        self._settings     = {}
        self._wrap_job     = None
        self._row_widgets  = []
        self._ep_labels    = []
        self._sched        = None
        self._checking     = False
        self._status_timer = None
        self._build_ui()
        self.after(1, self._init_data)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Control-n>", lambda e: self._add_show())
        self.bind("<F5>",        lambda e: self._check_all())

    def _init_data(self):
        import schedule as _s
        self._sched    = _s
        self._shows    = load_shows()
        self._settings = load_settings()
        geo = self._settings.get("window_geometry","")
        if geo:
            try: self.geometry(geo)
            except Exception: pass
        self._full_rebuild()
        self._start_scheduler()
        threading.Thread(target=_prewarm, daemon=True).start()
        # Auto-refresh on launch so the table never shows stale results
        # (also covers any schedule that fired while the app was closed)
        if self._shows:
            self.after(1200, self._check_all)

    # ── UI ─────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=12, pady=(10,4))
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(top, text="Anime Tracker",
                     font=ctk.CTkFont(size=20, weight="bold")).grid(row=0, column=0, sticky="w")
        self._next_lbl = ctk.CTkLabel(top, text="", font=ctk.CTkFont(size=11), text_color="gray55")
        self._next_lbl.grid(row=1, column=0, sticky="w")
        btns = ctk.CTkFrame(top, fg_color="transparent")
        btns.grid(row=0, column=1, rowspan=2, sticky="e")
        ctk.CTkButton(btns, text="＋ Add Show", width=110,
                      command=self._add_show).pack(side="left", padx=3)
        self._check_all_btn = ctk.CTkButton(btns, text="Check All", width=90,
            fg_color="#2a7a2a", hover_color="#3a9a3a", command=self._check_all)
        self._check_all_btn.pack(side="left", padx=3)
        ctk.CTkButton(btns, text="Settings", width=82, fg_color="gray30",
                      hover_color="gray40", command=self._open_settings).pack(side="left", padx=3)

        self._scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._scroll.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0,2))
        self._scroll.bind("<Configure>", self._queue_wrap)

        for ci,(lbl,minsz,expand) in enumerate(COLS):
            self._scroll.grid_columnconfigure(ci, minsize=minsz or 80, weight=1 if expand else 0)
            ctk.CTkLabel(self._scroll, text=lbl, font=ctk.CTkFont(size=11, weight="bold"),
                         anchor="w", text_color="gray55"
                         ).grid(row=0, column=ci, sticky="ew", padx=(4,2), pady=(2,0))
        ctk.CTkFrame(self._scroll, height=1, fg_color="gray35").grid(
            row=1, column=0, columnspan=len(COLS), sticky="ew", padx=2, pady=(0,2))

        self._status_bar = ctk.CTkLabel(self, text="Loading…",
                                        font=ctk.CTkFont(size=10), text_color="gray45", anchor="w")
        self._status_bar.grid(row=2, column=0, sticky="ew", padx=14, pady=(2,6))

    # ── Table ──────────────────────────────────────────────────────────────
    def _full_rebuild(self):
        for rw in self._row_widgets:
            for w in rw.values():
                if hasattr(w, "destroy"): w.destroy()
        self._row_widgets.clear()
        self._ep_labels.clear()
        if not self._shows:
            lbl = ctk.CTkLabel(self._scroll,
                               text="No shows — press  Ctrl+N  or  ＋ Add Show  to begin.",
                               text_color="gray50", font=ctk.CTkFont(size=12))
            lbl.grid(row=2, column=0, columnspan=len(COLS), pady=40)
            self._row_widgets.append({"_empty": lbl})
            self._set_status("Ready", auto_clear=False)
            return
        for i,show in enumerate(self._shows):
            self._create_row(i, show)
        self._update_next_check()
        self._set_status("Ready", auto_clear=False)

    def _create_row(self, i: int, show: dict):
        r, rw = i + 2, {}

        nl = ctk.CTkLabel(self._scroll, text=show.get("name",""),
                          anchor="w", font=ctk.CTkFont(size=12), cursor="hand2")
        nl.grid(row=r, column=0, sticky="ew", padx=(4,2), pady=1)
        nl.bind("<Double-Button-1>", lambda e, n=i: self._edit_show(n))
        rw["name"] = nl

        day, t = show.get("day",""), show.get("check_time","")
        fd, ft  = add_hours(day, t, 10)
        sl = ctk.CTkLabel(self._scroll, text=f"{day[:3]} {t}  /  +{fd[:3]} {ft}",
                          anchor="w", font=ctk.CTkFont(size=10), text_color="gray65")
        sl.grid(row=r, column=1, sticky="ew", padx=(4,2), pady=1)
        rw["sched"] = sl

        for key,col in [("group",2),("resolution",3)]:
            lbl = ctk.CTkLabel(self._scroll, text=show.get(key,""),
                               anchor="w", font=ctk.CTkFont(size=11))
            lbl.grid(row=r, column=col, sticky="ew", padx=(4,2), pady=1)
            rw[key] = lbl

        ul = ctk.CTkLabel(self._scroll, text=show.get("nyaa_time") or "—",
                          anchor="w", font=ctk.CTkFont(size=11))
        ul.grid(row=r, column=4, sticky="ew", padx=(4,2), pady=1)
        rw["uploaded"] = ul

        status = show.get("status","")
        # Large initial wraplength so text doesn't wrap before layout settles
        el = ctk.CTkLabel(self._scroll, text=_ep_text(show), anchor="w",
                          font=ctk.CTkFont(size=11), text_color=_ep_color(status),
                          wraplength=800, justify="left")
        el.grid(row=r, column=5, sticky="ew", padx=(4,2), pady=1)
        rw["ep"] = el
        self._ep_labels.append(el)

        # Buttons — all in one frame, no invisible spacer widgets
        bf = ctk.CTkFrame(self._scroll, fg_color="transparent")
        bf.grid(row=r, column=6, sticky="e", padx=(2,4), pady=1)

        ck = ctk.CTkButton(bf, text="Check",    width=58,
                           command=lambda n=i: self._check_one(n))
        ck.pack(side="left", padx=2)
        dl = ctk.CTkButton(bf, text="Download", width=78,
                           fg_color="#2a5fa5", hover_color="#3a6fb5",
                           command=lambda n=i: self._download(n))
        dl.pack(side="left", padx=2)
        ctk.CTkButton(bf, text="Edit", width=48,
                      fg_color="gray30", hover_color="gray40",
                      command=lambda n=i: self._edit_show(n)).pack(side="left", padx=2)
        # Extra left padding on ✕ creates visual gap without invisible frame
        ctk.CTkButton(bf, text="✕", width=28,
                      fg_color="#6b1515", hover_color="#8b2020",
                      command=lambda n=i: self._confirm_remove(n)).pack(side="left", padx=(8,2))

        rw["btns"] = bf; rw["check_btn"] = ck; rw["dl_btn"] = dl
        self._row_widgets.append(rw)
        if show.get("latest_title") and show.get("latest_title") == show.get("downloaded_title"):
            self._set_dl_state(i, "sent")

    def _update_row_display(self, i: int, show: dict):
        if i >= len(self._row_widgets) or "_empty" in self._row_widgets[i]:
            return
        rw = self._row_widgets[i]
        rw["uploaded"].configure(text=show.get("nyaa_time") or "—")
        rw["ep"].configure(text=_ep_text(show),
                           text_color=_ep_color(show.get("status","")))
        if show.get("latest_title"):
            self._set_dl_state(i, "sent" if show.get("latest_title") == show.get("downloaded_title")
                               else "normal")

    def _set_row_checking(self, i: int, checking: bool):
        if i >= len(self._row_widgets) or "_empty" in self._row_widgets[i]:
            return
        rw = self._row_widgets[i]
        if checking:
            rw["ep"].configure(text="Checking…", text_color="gray45")
            rw["check_btn"].configure(state="disabled", text="…")
        else:
            self._update_row_display(i, self._shows[i] if i < len(self._shows) else {})
            rw["check_btn"].configure(state="normal", text="Check")

    # ── Wrap debounce ──────────────────────────────────────────────────────
    def _queue_wrap(self, _=None):
        if self._wrap_job: self.after_cancel(self._wrap_job)
        self._wrap_job = self.after(80, self._do_wrap)

    def _do_wrap(self):
        self._wrap_job = None
        for lbl in self._ep_labels:
            w = lbl.winfo_width()
            if w > 20: lbl.configure(wraplength=w - 6)

    # ── Status bar ─────────────────────────────────────────────────────────
    def _set_status(self, msg: str, auto_clear: bool = True):
        if self._status_timer: self.after_cancel(self._status_timer); self._status_timer = None
        self._status_bar.configure(text=msg)
        if auto_clear:
            self._status_timer = self.after(5000, lambda: self._set_status("Ready", False))

    # ── Next check ─────────────────────────────────────────────────────────
    def _update_next_check(self):
        if not self._sched: return
        jobs = self._sched.get_jobs()
        nxt  = min((j.next_run for j in jobs if j.next_run), default=None) if jobs else None
        self._next_lbl.configure(
            text=f"Next check:  {nxt.strftime('%A  %H:%M')}" if nxt
            else ("No shows scheduled." if not self._shows else ""))

    # ── Download ───────────────────────────────────────────────────────────
    def _download(self, idx: int):
        if idx >= len(self._shows): return
        show   = self._shows[idx]
        magnet = show.get("latest_magnet") or show.get("latest_link")
        if not magnet:
            self._set_status("No episode found — press Check first.")
            return
        settings = self._settings
        if settings.get("download_mode") != "qbittorrent":  # default_app (or legacy "magnet")
            try:
                os.startfile(magnet)  # opens whatever client handles magnet: links
                self._dl_result(idx, show,
                                f"↓ Sent to your torrent app: {show.get('latest_title','')}", ok=True)
            except OSError:
                self._set_status("No torrent app handles magnet links — install one "
                                 "(e.g. qBittorrent) or set qBittorrent Web UI in Settings.")
            return
        self._set_status("Connecting to qBittorrent…", False)
        self._set_dl_state(idx, "sending")
        def do_dl():
            err = qbit_add(magnet, settings)
            if err == "connection_error":
                self.after(0, lambda: self._set_status("qBittorrent not running — launching…", False))
                if not launch_qbit(settings):
                    self.after(0, lambda: self._dl_result(idx, show,
                        "Couldn't find qBittorrent. Open it manually then click Download.", ok=False))
                    return
                for _ in range(20):
                    time.sleep(1)
                    err = qbit_add(magnet, settings)
                    if err != "connection_error":
                        break
                else:
                    self.after(0, lambda: self._dl_result(idx, show,
                        "qBittorrent opened but Web UI not responding. Enable it in Preferences › Web UI.",
                        ok=False))
                    return
            if err:
                self.after(0, lambda e=err: self._dl_result(idx, show, f"qBit error: {e}", ok=False))
            else:
                self.after(0, lambda: self._dl_result(
                    idx, show, f"↓ Sent to qBittorrent: {show.get('latest_title','')}", ok=True))
        threading.Thread(target=do_dl, daemon=True).start()

    def _set_dl_state(self, idx: int, state: str):
        if idx >= len(self._row_widgets) or "_empty" in self._row_widgets[idx]:
            return
        btn = self._row_widgets[idx]["dl_btn"]
        if state == "sending":
            btn.configure(state="disabled", text="Sending…")
        elif state == "sent":
            btn.configure(state="normal", text="✓ Sent", fg_color="#2a7a2a", hover_color="#3a9a3a")
        else:
            btn.configure(state="normal", text="Download", fg_color="#2a5fa5", hover_color="#3a6fb5")

    def _dl_result(self, idx: int, show: dict, msg: str, ok: bool):
        if ok:
            show["downloaded_title"] = show.get("latest_title")
            save_shows(self._shows)
            self._update_row_display(idx, show)
            self._set_dl_state(idx, "sent")
        else:
            self._set_dl_state(idx, "normal")
        self._set_status(msg)

    # ── Check one ──────────────────────────────────────────────────────────
    def _check_one(self, idx: int):
        if idx >= len(self._shows): return
        show = self._shows[idx]
        self._set_status(f"Checking {show['name']}…", False)
        self._set_row_checking(idx, True)
        def run():
            results = search_nyaa(show["name"], show.get("resolution","1080p"),
                                  show.get("group","ASW"))
            is_new = apply_result(show, results)
            save_shows(self._shows)
            self.after(0, lambda: self._set_row_checking(idx, False))
            if results is None:
                label = "⚠ nyaa.si unreachable — check your connection"
            elif results:
                label = ("● New episode" if is_new else "✓ Found") + f": {show['name']}"
            else:
                label = f"✗ Not found: {show['name']}"
            self.after(0, lambda: self._set_status(label))
        threading.Thread(target=run, daemon=True).start()

    # ── Check all ──────────────────────────────────────────────────────────
    def _check_all(self):
        if self._checking or not self._shows: return
        self._checking = True
        total = len(self._shows)
        self._check_all_btn.configure(state="disabled", text="Checking…")
        for i in range(total): self._set_row_checking(i, True)
        def run():
            found, new, neterr = 0, 0, False
            for n, show in enumerate(list(self._shows)):
                self.after(0, lambda n=n: self._set_status(f"Checking {n+1}/{total}…", False))
                results = search_nyaa(show["name"], show.get("resolution","1080p"),
                                      show.get("group","ASW"))
                if apply_result(show, results): new += 1
                if results: found += 1
                if results is None: neterr = True
                self.after(0, lambda n=n: self._set_row_checking(n, False))
            save_shows(self._shows)
            self._checking = False
            if neterr:
                msg = "⚠ nyaa.si unreachable — check your connection"
            else:
                msg = f"✓ Done — {found}/{total} found" + (f", {new} new" if new else "") + "."
            self.after(0, lambda: self._check_all_btn.configure(state="normal", text="Check All"))
            self.after(0, lambda: self._set_status(msg))
            self.after(0, self._update_next_check)
        threading.Thread(target=run, daemon=True).start()

    # ── Scheduled check ────────────────────────────────────────────────────
    def _scheduled_check(self, show: dict):
        results = search_nyaa(show["name"], show.get("resolution","1080p"),
                              show.get("group","ASW"))
        is_new = apply_result(show, results)
        save_shows(self._shows)
        try:
            idx = self._shows.index(show)
            self.after(0, lambda: self._update_row_display(idx, show))
            if is_new:
                self.after(0, lambda: self._set_status(
                    f"● New episode: {show.get('latest_title','')}", auto_clear=False))
        except ValueError: pass
        self.after(0, self._update_next_check)

    # ── CRUD ───────────────────────────────────────────────────────────────
    def _add_show(self):
        def on_save(data):
            self._shows.append(empty_show(**data))
            save_shows(self._shows); self._full_rebuild(); self._reschedule()
        AddShowDialog(self, on_save=on_save)

    def _edit_show(self, idx: int):
        if idx >= len(self._shows): return
        def on_save(data):
            self._shows[idx].update(data)
            save_shows(self._shows); self._full_rebuild(); self._reschedule()
        AddShowDialog(self, show=self._shows[idx], on_save=on_save)

    def _confirm_remove(self, idx: int):
        if idx >= len(self._shows): return
        ConfirmDialog(self, f'Remove "{self._shows[idx].get("name","")}"?',
                      on_confirm=lambda: self._remove_show(idx))

    def _remove_show(self, idx: int):
        if idx >= len(self._shows): return
        self._shows.pop(idx)
        save_shows(self._shows); self._full_rebuild(); self._reschedule()

    def _open_settings(self):
        def on_save(data):
            self._settings.update(data); save_settings(self._settings)
        SettingsDialog(self, self._settings, on_save=on_save)

    # ── Scheduler ──────────────────────────────────────────────────────────
    def _start_scheduler(self):
        self._reschedule()
        threading.Thread(target=self._run_scheduler, daemon=True).start()

    def _run_scheduler(self):
        while True:
            self._sched.run_pending()
            time.sleep(30)

    def _reschedule(self):
        if not self._sched: return
        self._sched.clear()
        for show in self._shows:
            day, t = show.get("day","Monday"), show.get("check_time","18:00")
            getattr(self._sched.every(), day.lower()).at(t).do(self._scheduled_check, show)
            fd, ft = add_hours(day, t, 10)
            getattr(self._sched.every(), fd.lower()).at(ft).do(self._scheduled_check, show)
        self._update_next_check()

    def _on_close(self):
        self._settings["window_geometry"] = self.geometry()
        save_settings(self._settings)
        save_shows(self._shows)
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
