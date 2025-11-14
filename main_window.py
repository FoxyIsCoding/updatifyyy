#!/usr/bin/env python3

import os
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango

from dialogs.about import show_about_dialog
from dialogs.changes import on_view_changes_quick
from dialogs.details import show_repo_info_dialog
from dialogs.logs import show_logs_dialog
from dialogs.settings import show_settings_dialog
from helpers.ansi import insert_ansi_formatted
from style.css import get_css
from widgets.console import SetupConsole

# -------------------------------------------------------------------
# App metadata
# -------------------------------------------------------------------
APP_ID = "com.foxy.illogical-updots"
APP_TITLE = "illogical-updots"

# -------------------------------------------------------------------
# Settings file paths
# -------------------------------------------------------------------
SETTINGS_DIR = os.path.join(os.path.expanduser("~"), ".config", "illogical-updots")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")


# -------------------------------------------------------------------
# Load settings (with defaults)
# -------------------------------------------------------------------
def _load_settings() -> dict:
    """
    Load persisted settings from disk, applying defaults for missing keys.

    Returns:
        dict: Settings merged with defaults. Unknown keys in the file are ignored.

    Error handling:
        - Any exception while parsing or reading the file is swallowed
          and defaults are returned (robust against file corruption).
    """
    data = {
        "repo_path": "",
        "auto_refresh_seconds": 60,
        "detached_console": False,  # Use external console window instead of embedded
        "installer_mode": "auto",  # auto / full / files-only
        "use_pty": True,  # Preserve color & interactive prompts
        "force_color_env": True,  # Force color env vars for spawned processes
        "send_notifications": True,  # Desktop notifications on completion
        "log_max_lines": 5000,  # Trim log buffer (0 = unlimited)
        "changes_lazy_load": True,  # Lazy load commit list
        "post_script_path": "",  # Optional script executed after install
        "show_details_button": True,  # Show small details link under banner
    }
    try:
        if os.path.isfile(SETTINGS_FILE):
            import json

            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            # Merge only known keys
            data.update({k: v for k, v in loaded.items() if k in data})
    except Exception:
        pass
    return data


# -------------------------------------------------------------------
# Save settings atomically
# -------------------------------------------------------------------
def _save_settings(data: dict) -> None:
    """
    Persist settings to disk atomically (write temp file then replace).

    Args:
        data: Dictionary of settings to persist.

    Implementation details:
        - Writes to a temporary file then replaces the target to avoid corruption
          on partial writes.
        - Silent on errors (non-critical).
    """
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        import json

        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except Exception:
        pass


SETTINGS = _load_settings()


# -------------------------------------------------------------------
# Detect initial repo path (fallback if missing)
# -------------------------------------------------------------------
def _detect_initial_repo_path() -> str:
    """
    Determine repository path to operate on.

    Precedence:
        1. Use stored 'repo_path' if it exists and is a directory.
        2. Fall back to '~/.cache/dots-hyprland' if present (auto-detected).
        3. Otherwise return empty string (signals UI error state).

    Side effects:
        - If fallback is used, update settings and persist immediately.
    """
    p = str(SETTINGS.get("repo_path") or "").strip()
    if p and os.path.isdir(p):
        return p
    fallback = os.path.expanduser("~/.cache/dots-hyprland")
    if os.path.isdir(fallback):
        SETTINGS["repo_path"] = fallback
        _save_settings(SETTINGS)
        return fallback
    return ""


REPO_PATH = _detect_initial_repo_path()
AUTO_REFRESH_SECONDS = int(SETTINGS.get("auto_refresh_seconds", 60))


# -------------------------------------------------------------------
# Repo status structure
# -------------------------------------------------------------------
@dataclass
class RepoStatus:
    """
    Snapshot of repository state used by UI logic.

    Attributes:
        ok (bool): Whether the repository is valid and accessible.
        repo_path (str): Path to the repository.
        branch (str|None): Current HEAD branch name.
        upstream (str|None): Upstream tracking reference (e.g. origin/main).
        behind (int): Number of commits local is behind upstream.
        ahead (int): Number of commits local is ahead of upstream.
        dirty (int): Count of modified/untracked files (`git status --porcelain`).
        fetch_error (str|None): Any error message from `git fetch`.
        error (str|None): Fatal error (invalid path / not a repo).
    """

    ok: bool
    repo_path: str
    branch: Optional[str] = None
    upstream: Optional[str] = None
    behind: int = 0
    ahead: int = 0
    dirty: int = 0
    fetch_error: Optional[str] = None
    error: Optional[str] = None

    @property
    def has_updates(self) -> bool:
        """
        Returns:
            bool: True if the repository is valid and there are upstream commits
                  not yet pulled locally (behind > 0).
        """
        return self.ok and self.behind > 0


# -------------------------------------------------------------------
# Git helpers
# -------------------------------------------------------------------
def run_git(args: list[str], cwd: str, timeout: int = 15) -> Tuple[int, str, str]:
    """
    Run a git command and capture stdout/stderr.

    Args:
        args: Arguments after 'git'.
        cwd: Working directory (repository root).
        timeout: Seconds before process is killed.

    Returns:
        (returncode, stdout, stderr)

    Resilience:
        - On any exception returns (1, "", str(exc)).
    """
    try:
        cp = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        return cp.returncode, cp.stdout, cp.stderr
    except Exception as exc:
        return 1, "", str(exc)


def get_branch(cwd: str) -> Optional[str]:
    """
    Get current branch name.

    Returns:
        str|None: Branch name or None if detached or error.
    """
    rc, out, _ = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return out.strip() if rc == 0 else None


def get_upstream(cwd: str, branch: Optional[str]) -> Optional[str]:
    """
    Determine upstream remote reference for current branch.

    Strategy:
        - Try rev-parse @{u} which resolves tracking reference.
        - If that fails and branch is known, assume 'origin/<branch>'.

    Returns:
        str|None: Upstream or None if not found.
    """
    rc, out, _ = run_git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd
    )
    if rc == 0:
        return out.strip()
    if branch:
        return f"origin/{branch}"
    return None


def get_dirty_count(cwd: str) -> int:
    """
    Count modified/untracked files.

    Returns:
        int: Number of non-empty lines in `git status --porcelain`.
             Zero if error.
    """
    rc, out, _ = run_git(["status", "--porcelain"], cwd)
    if rc != 0:
        return 0
    return len([ln for ln in out.splitlines() if ln.strip()])


def check_repo_status(repo_path: str) -> RepoStatus:
    """
    Build a RepoStatus describing current repository update condition.

    Workflow:
        1. Validate path & presence of .git.
        2. Run 'git fetch --all --prune' (non-fatal if fails).
        3. Determine branch & upstream.
        4. Compute behind/ahead counts via rev-list comparisons.
        5. Count dirty files.

    Returns:
        RepoStatus: Complete snapshot (ok=False if invalid).
    """
    if not os.path.isdir(repo_path):
        return RepoStatus(
            ok=False, repo_path=repo_path, error="Repository path not found"
        )
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        return RepoStatus(ok=False, repo_path=repo_path, error="Not a git repository")

    fetch_error = None
    rc, _out, err = run_git(["fetch", "--all", "--prune"], repo_path)
    if rc != 0:
        fetch_error = (err or "fetch failed").strip()

    branch = get_branch(repo_path)
    upstream = get_upstream(repo_path, branch)

    behind = 0
    ahead = 0
    if upstream:
        # Count commits present in upstream but not local
        rc_b, out_b, _ = run_git(
            ["rev-list", "--count", f"HEAD..{upstream}"], repo_path
        )
        if rc_b == 0:
            try:
                behind = int(out_b.strip() or "0")
            except ValueError:
                behind = 0
        # Count commits present locally but not upstream
        rc_a, out_a, _ = run_git(
            ["rev-list", "--count", f"{upstream}..HEAD"], repo_path
        )
        if rc_a == 0:
            try:
                ahead = int(out_a.strip() or "0")
            except ValueError:
                ahead = 0

    dirty = get_dirty_count(repo_path)

    return RepoStatus(
        ok=True,
        repo_path=repo_path,
        branch=branch,
        upstream=upstream,
        behind=behind,
        ahead=ahead,
        dirty=dirty,
        fetch_error=fetch_error,
    )


# -------------------------------------------------------------------
# Main Window
# -------------------------------------------------------------------
class MainWindow(Gtk.ApplicationWindow):
    """
    Primary GTK Application window.

    UI Components:
        - Header bar: Refresh, Update, View changes, Menu (Settings / Logs / About).
        - Status banner indicating update availability.
        - Console area (revealer) for streaming installer output.
        - Error message panel (revealer) for transient errors/warnings.

    Lifecycle:
        - On init: sets up UI, triggers first status refresh, schedules auto refresh.
        - User actions: update triggers git pull and runs installer; refresh rescans repo.
    """

    def __init__(self, app: Gtk.Application) -> None:
        """
        Initialize window controls and kick off first status check.

        Args:
            app: The Gtk.Application instance owning this window.

        Implementation notes:
            - Icon variants are searched across common system paths plus repo assets.
            - All long-running tasks (git, installer) are performed in background threads.
        """
        super().__init__(application=app, title=APP_TITLE)
        self.set_default_size(520, 280)
        self.set_border_width(0)

        # Try to load icons (theme + fallbacks)
        try:
            self.set_icon_name("illogical-updots")
        except Exception:
            pass
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            candidates = [
                "/usr/share/icons/hicolor/256x256/apps/illogical-updots.png",
                "/usr/share/icons/hicolor/128x128/apps/illogical-updots.png",
                "/usr/share/icons/hicolor/64x64/apps/illogical-updots.png",
                "/usr/share/icons/hicolor/48x48/apps/illogical-updots.png",
                "/usr/share/icons/hicolor/32x32/apps/illogical-updots.png",
                "/usr/share/icons/hicolor/16x16/apps/illogical-updots.png",
                "/usr/share/pixmaps/illogical-updots.png",
                os.path.join(base_dir, ".github", "assets", "logo.png"),
                os.path.join(base_dir, "assets", "logo.png"),
            ]
            src = None
            for p in candidates:
                if os.path.isfile(p):
                    src = p
                    break
            if src:
                try:
                    pixbuf = GdkPixbuf.Pixbuf.new_from_file(src)
                    sizes = [16, 24, 32, 48, 64, 96, 128, 256]
                    icon_list = []
                    for s in sizes:
                        try:
                            icon_list.append(
                                pixbuf.scale_simple(s, s, GdkPixbuf.InterpType.BILINEAR)
                            )
                        except Exception:
                            continue
                    if icon_list:
                        self.set_icon_list(icon_list)
                except Exception:
                    # Fallback single icon attempt
                    try:
                        self.set_icon_from_file(src)
                    except Exception:
                        pass
        except Exception:
            pass

        # Header bar
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.props.title = APP_TITLE
        self.header_bar = hb
        self.header_bar.props.subtitle = REPO_PATH
        self.set_titlebar(hb)

        # Refresh button
        self.refresh_btn = Gtk.Button.new_from_icon_name(
            "view-refresh", Gtk.IconSize.BUTTON
        )
        self.refresh_btn.set_tooltip_text("Refresh status")
        self.refresh_btn.connect("clicked", self.on_refresh_clicked)
        hb.pack_start(self.refresh_btn)

        # Update button (changes label based on state)
        self.update_btn = Gtk.Button(label="Update")  # triggers pull + installer
        self.update_btn.connect("clicked", self.on_update_clicked)

        # View changes button
        self.view_btn = Gtk.Button(label="View changes")
        self.view_btn.set_tooltip_text("View commits to be pulled")
        self.view_btn.connect(
            "clicked", lambda _btn: on_view_changes_quick(self, run_git)
        )

        # Menu
        menu = Gtk.Menu()
        mi_settings = Gtk.MenuItem(label="Settings")
        mi_settings.connect("activate", self.on_settings_clicked)
        menu.append(mi_settings)

        mi_logs = Gtk.MenuItem(label="Git Logs")
        mi_logs.connect("activate", self.on_logs_clicked)
        menu.append(mi_logs)

        mi_about = Gtk.MenuItem(label="About")
        mi_about.connect("activate", self.on_about_clicked)
        menu.append(mi_about)
        menu.show_all()

        menu_btn = Gtk.MenuButton()
        menu_btn.set_tooltip_text("Menu")
        menu_btn.set_popup(menu)
        menu_btn.set_image(
            Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON)
        )

        hb.pack_end(menu_btn)
        hb.pack_end(self.view_btn)
        hb.pack_end(self.update_btn)

        # Outer layout container
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(outer)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_border_width(16)
        outer.pack_start(content, True, True, 0)

        # Banner: large primary label for status
        self.primary_label = Gtk.Label()
        self.primary_label.set_xalign(0.5)
        self.primary_label.set_yalign(0.5)
        self.primary_label.set_use_markup(True)
        self.primary_label.get_style_context().add_class("status-banner")
        self.primary_label.set_line_wrap(True)
        self.primary_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.primary_label.set_markup(
            "<span size='xx-large' weight='bold'>Checking repository status…</span>"
        )
        self.primary_label.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.primary_label.connect("button-press-event", self._on_banner_clicked)

        banner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        banner_box.set_hexpand(True)
        banner_box.set_vexpand(True)
        banner_box.pack_start(self.primary_label, True, True, 0)

        # Small details button (conditionally shown)
        self.small_info_btn = Gtk.Button(label="")
        self.small_info_btn.set_relief(Gtk.ReliefStyle.NONE)
        try:
            self.small_info_btn.get_style_context().add_class("tiny-link")
        except Exception:
            pass
        self.small_info_btn.set_halign(Gtk.Align.CENTER)
        self.small_info_btn.connect("clicked", lambda _b: self._show_repo_info_dialog())
        self.small_info_btn.hide()
        banner_box.pack_start(self.small_info_btn, False, False, 0)

        content.pack_start(banner_box, True, True, 0)

        # Details label intentionally omitted for minimal design
        self.details_label = None

        # Spinner + hint label row
        spin_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.spinner = Gtk.Spinner()
        spin_box.pack_start(self.spinner, False, False, 0)
        self.status_hint = Gtk.Label(label="")
        self.status_hint.set_xalign(0.0)
        spin_box.pack_start(self.status_hint, False, False, 0)
        content.pack_start(spin_box, False, False, 0)

        # Log console revealer (hidden until used)
        self.log_revealer = Gtk.Revealer()
        self.log_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.log_revealer.set_reveal_child(False)

        log_frame = Gtk.Frame()
        log_frame.set_shadow_type(Gtk.ShadowType.IN)
        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        log_box.set_border_width(6)
        log_frame.add(log_box)

        # Log header
        log_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        log_title = Gtk.Label(label="Console")
        log_title.set_xalign(0.0)
        log_header.pack_start(log_title, True, True, 0)

        self.log_clear_btn = Gtk.Button.new_from_icon_name(
            "edit-clear-symbolic", Gtk.IconSize.SMALL_TOOLBAR
        )
        self.log_clear_btn.set_tooltip_text("Clear console")
        self.log_clear_btn.connect("clicked", lambda _b: self._clear_log_view())
        log_header.pack_end(self.log_clear_btn, False, False, 0)

        self.log_hide_btn = Gtk.Button.new_from_icon_name(
            "go-up-symbolic", Gtk.IconSize.SMALL_TOOLBAR
        )
        self.log_hide_btn.set_tooltip_text("Hide console")
        self.log_hide_btn.connect(
            "clicked", lambda _b: self.log_revealer.set_reveal_child(False)
        )
        log_header.pack_end(self.log_hide_btn, False, False, 0)

        log_box.pack_start(log_header, False, False, 0)

        # Log view (TextView)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_monospace(True)
        self._init_log_css()
        self.log_buf = self.log_view.get_buffer()

        log_sw = Gtk.ScrolledWindow()
        log_sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        log_sw.set_min_content_height(320)
        log_sw.add(self.log_view)
        log_box.pack_start(log_sw, True, True, 0)

        # Console input controls (for interactive installer prompts)
        self.log_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.log_input_entry = Gtk.Entry()
        self.log_input_entry.set_placeholder_text("Type input (Enter to send)")
        self.log_input_entry.connect("activate", self._on_log_send)
        self.log_controls.pack_start(self.log_input_entry, True, True, 0)

        for label, payload in [("Y", "y\n"), ("N", "n\n"), ("Enter", "\n")]:
            btn = Gtk.Button(label=label)
            btn.connect("clicked", lambda _b, t=payload: self._send_to_proc(t))
            self.log_controls.pack_start(btn, False, False, 0)

        ctrlc_btn = Gtk.Button(label="Ctrl+C")
        ctrlc_btn.connect("clicked", self._on_log_ctrl_c)
        self.log_controls.pack_start(ctrlc_btn, False, False, 0)
        log_box.pack_start(self.log_controls, False, False, 0)

        self.log_view.connect("key-press-event", self._on_log_key_press)

        self.log_revealer.add(log_frame)
        outer.pack_start(self.log_revealer, False, False, 0)

        # Error panel (collapsible)
        self.error_revealer = Gtk.Revealer()
        self.error_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.error_revealer.set_reveal_child(False)

        error_frame = Gtk.Frame()
        error_frame.set_shadow_type(Gtk.ShadowType.IN)
        error_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        error_box.set_border_width(8)
        self.error_icon = Gtk.Image.new_from_icon_name(
            "dialog-error-symbolic", Gtk.IconSize.MENU
        )
        self.error_label = Gtk.Label(xalign=0.0)
        self.error_label.set_line_wrap(True)
        self.error_label.set_max_width_chars(80)
        error_box.pack_start(self.error_icon, False, False, 0)
        error_box.pack_start(self.error_label, True, True, 0)
        error_frame.add(error_box)
        self.error_revealer.add(error_frame)
        outer.pack_end(self.error_revealer, False, False, 0)

        self.show_all()
        self.connect("key-press-event", self._on_key_press)

        # State tracking
        self._status: Optional[RepoStatus] = None
        self._update_logs: list[tuple[str, str, str]] = []
        self._busy(False, "")
        self._current_proc = None
        self._sudo_keepalive_stop = None  # (placeholder for future enhancement)
        self._sudo_keepalive_thread = None

        # Initial and periodic refresh
        self.refresh_status()
        GLib.timeout_add_seconds(AUTO_REFRESH_SECONDS, self._auto_refresh)

    # Wrapper helpers delegating to shared functions (allow reuse)
    def _init_log_css(self) -> None:
        """Apply CSS styles to the embedded console text view."""
        _init_log_css(self)

    def _append_log(self, text: str) -> None:
        """Append text (with ANSI formatting) to the console buffer."""
        _append_log(self, text)

    def _clear_log_view(self) -> None:
        """Clear the entire console log buffer."""
        _clear_log_view(self)

    # Show message in error panel
    def _show_message(self, msg_type: Gtk.MessageType, message: str) -> None:
        """
        Display a transient message in the error panel.

        Args:
            msg_type: GTK message type (INFO / WARNING / ERROR).
            message: Text to display; hides panel if empty.
        """
        icon = (
            "dialog-error-symbolic"
            if msg_type in (Gtk.MessageType.ERROR, Gtk.MessageType.WARNING)
            else "dialog-information-symbolic"
        )
        try:
            self.error_icon.set_from_icon_name(icon, Gtk.IconSize.MENU)
        except Exception:
            pass
        self.error_label.set_text(message or "")
        self.error_revealer.set_reveal_child(bool(message))

    # Add log entry to internal structured list (for history dialogs)
    def _add_log(self, event: str, summary: str, details: str) -> None:
        """
        Record an update-related event to internal log list.

        Args:
            event: Short event category.
            summary: Human summary.
            details: Extended multiline detail (optional).
        """
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self._update_logs.append(
            (ts, event, summary + ("\n" + details if details else ""))
        )

    # Disabled: no sudo keepalive / pre-auth (future extension placeholders)
    def _start_sudo_keepalive(self) -> None:
        """Placeholder for future sudo keepalive logic (currently unused)."""
        return

    def _ensure_sudo_pre_auth(self) -> None:
        """Placeholder for pre-auth workflow (currently unused)."""
        return

    def _patch_setup_for_polkit(self, repo_path: str) -> None:
        """Placeholder to patch setup script for polkit (currently unused)."""
        return

    # Send input to running process (pty or pipe)
    def _send_to_proc(self, text: str) -> None:
        """
        Send raw input text to the active installer process.

        Supports:
            - PTY master write (preferred when using PTY).
            - Fallback: write to stdin file descriptor.

        Args:
            text: Input (include trailing newline if needed).
        """
        p = getattr(self, "_current_proc", None)
        master_fd = getattr(p, "_pty_master_fd", None) if p else None
        if p and (master_fd is not None or getattr(p, "stdin", None)):
            try:
                if master_fd is not None:
                    os.write(master_fd, text.encode("utf-8", "replace"))
                else:
                    os.write(p.stdin.fileno(), text.encode("utf-8", "replace"))
                self._append_log(f"[sent] {text}")
            except Exception as ex:
                self._append_log(f"[send error] {ex}\n")

    def _on_log_send(self, _entry: Gtk.Entry) -> None:
        """Handle Enter key in input entry: send text to process and clear field."""
        entry = getattr(self, "log_input_entry", None)
        if not entry:
            return
        txt = entry.get_text()
        if txt and not txt.endswith("\n"):
            txt += "\n"
        if txt:
            self._send_to_proc(txt)
        entry.set_text("")

    def _on_log_ctrl_c(self, _btn: Gtk.Button) -> None:
        """
        Send SIGINT to the current process (simulate Ctrl+C).
        Useful for canceling interactive prompts.
        """
        p = getattr(self, "_current_proc", None)
        if p:
            try:
                import signal

                p.send_signal(signal.SIGINT)
                self._append_log("[signal] SIGINT sent\n")
            except Exception as ex:
                self._append_log(f"[ctrl-c error] {ex}\n")

    def _on_log_key_press(self, _widget, event) -> bool:
        """
        Key press handler when focus is in log view.

        Quick shortcuts for y/n/newline to accelerate interactive prompts
        without needing to focus the entry.
        """
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._send_to_proc("\n")
            return True
        if event.keyval in (Gdk.KEY_y, Gdk.KEY_Y):
            self._send_to_proc("y\n")
            return True
        if event.keyval in (Gdk.KEY_n, Gdk.KEY_N):
            self._send_to_proc("n\n")
            return True
        return False

    # Run installer commands either embedded or detached
    def _run_installer_common(
        self, test_mode: bool = False, commands: Optional[list[list[str]]] = None
    ) -> None:
        """
        Execute setup installer sequence in either:
            - Detached external console (if settings enable).
            - Embedded console with streamed output.

        Args:
            test_mode: If True, indicates dry-run style execution (affects banners).
            commands: Explicit command sequences (each a list of argv).
        """
        repo_path = self._status.repo_path if self._status else REPO_PATH
        setup_path = os.path.join(repo_path, "setup")
        detached = bool(SETTINGS.get("detached_console", False))
        cmds = commands or [["./setup", "install"]]

        if detached:
            # Launch in external SetupConsole widget
            if not (os.path.isfile(setup_path) and os.access(setup_path, os.X_OK)):
                self._show_message(
                    Gtk.MessageType.INFO, "No executable './setup' found."
                )
                return
            import shlex

            chained = " && ".join(shlex.join(c) for c in cmds)
            title = "Installer (test)" if test_mode else "Installer"
            console = SetupConsole(self, title=title)
            console.present()
            console.run_process(
                ["fish", "-lc", chained],
                cwd=repo_path,
                on_finished=lambda: (
                    self.refresh_status(),
                    (not test_mode and self._run_post_script_if_configured()),
                ),
            )
            return

        # Embedded flow
        lr = getattr(self, "log_revealer", None)
        if lr:
            lr.set_reveal_child(True)
        self._append_log(
            "\n=== INSTALLER START ({}) ===\n".format("TEST" if test_mode else "NORMAL")
        )
        self._busy(
            True, "Running installer..." if test_mode else "Updating & installing..."
        )

        def work():
            """Background thread performing sequential installer commands."""
            success = False
            if os.path.isfile(setup_path) and os.access(setup_path, os.X_OK):
                for cmd in cmds:
                    try:
                        self._append_log(f"$ {' '.join(cmd)}\n")
                        p = _spawn_setup_install(
                            repo_path,
                            lambda m: self._append_log(str(m)),
                            extra_args=cmd[1:],
                            capture_stdout=True,
                            auto_input_seq=[],
                            use_pty=bool(SETTINGS.get("use_pty", True)),
                        )
                        self._current_proc = p
                        if p and p.stdout:
                            # Stream line-by-line (real-time)
                            for line in iter(p.stdout.readline, ""):
                                if not line:
                                    break
                                self._append_log(str(line))
                            rc = p.wait()
                            self._append_log(f"[exit {rc}]\n")
                            self._current_proc = None
                            if rc != 0:
                                success = False
                                break
                            success = True
                        else:
                            # Fallback path: run via fish if setup not exec-wrapped
                            fallback_cmd = ["fish"] + cmd
                            self._append_log(f"[fallback] {' '.join(fallback_cmd)}\n")
                            env = dict(os.environ)
                            if bool(SETTINGS.get("force_color_env", True)):
                                env.update(
                                    {
                                        "TERM": "xterm-256color",
                                        "FORCE_COLOR": "1",
                                        "CLICOLOR": "1",
                                        "CLICOLOR_FORCE": "1",
                                    }
                                )
                                env.pop("NO_COLOR", None)
                            p2 = subprocess.Popen(
                                fallback_cmd,
                                cwd=repo_path,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                stdin=subprocess.PIPE,
                                text=True,
                                encoding="utf-8",
                                errors="replace",
                                bufsize=1,
                                env=env,
                            )
                            self._current_proc = p2
                            assert p2.stdout is not None
                            for line in iter(p2.stdout.readline, ""):
                                if not line:
                                    break
                                self._append_log(str(line))
                            rc2 = p2.wait()
                            self._append_log(f"[exit {rc2}]\n")
                            self._current_proc = None
                            if rc2 != 0:
                                success = False
                                break
                            success = True
                    except Exception as ex:
                        self._append_log(f"[error] {ex}\n")
                        success = False
                        break
            else:
                self._append_log("No executable './setup' found. Nothing to run.\n")

            def done():
                """Finalize UI state after installer thread completes."""
                self._busy(False, "")
                title = "Installer (test mode)" if test_mode else "Installer"
                status_msg = (
                    f"{title} completed successfully"
                    if success
                    else f"{title} finished with errors"
                )
                self._add_log(title, status_msg, "")
                if success and not test_mode:
                    self._post_update_prompt()

            GLib.idle_add(done)

        threading.Thread(target=work, daemon=True).start()

    # Best effort polkit keep auth (ignored errors)
    def _ensure_polkit_keep_auth(self) -> None:
        """
        Attempt to install a polkit rule that grants persistent admin keep auth
        for the current user or wheel/sudo groups.

        Non-fatal:
            - Exceptions are swallowed; absence of polkit rule simply means
              password prompts may reappear.
        """
        try:
            import shlex
            import subprocess

            user = os.getlogin()
            rule_path = "/etc/polkit-1/rules.d/90-illogical-updots-keepauth.rules"
            rule_content = f"""// illogical-updots persistent auth rule
polkit.addRule(function(action, subject) {{
    if (subject.user == "{user}" || subject.isInGroup("wheel") || subject.isInGroup("sudo")) {{
        return {{ result: polkit.Result.AUTH_ADMIN_KEEP }};
    }}
}});
"""
            need_write = True
            try:
                with open(rule_path, "r", encoding="utf-8") as f:
                    existing = f.read()
                if (
                    "illogical-updots persistent auth rule" in existing
                    and user in existing
                ):
                    need_write = False
            except Exception:
                need_write = True
            if not need_write:
                return
            cmd = f"cat > {shlex.quote(rule_path)} <<'EOF'\n{rule_content}\nEOF\nchmod 644 {shlex.quote(rule_path)}"
            if shutil.which("pkexec"):
                subprocess.run(
                    ["pkexec", "fish", "-c", cmd],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            else:
                subprocess.run(
                    ["sudo", "fish", "-c", cmd],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
        except Exception:
            pass

    # Plan install commands based on mode
    def _plan_install_commands(self) -> list[list[str]]:
        """
        Determine which installer commands to run based on installer_mode.

        Modes:
            full        -> ['./setup', 'install']
            auto        -> Begin with files-only unless user chooses full later.
            files-only  -> ['./setup', 'install-files']

        Returns:
            list[list[str]]: A list of command argument vectors.
        """
        mode = str(SETTINGS.get("installer_mode", "auto"))
        if mode == "full":
            self._append_log("Installer mode: full install.\n")
            return [["./setup", "install"]]
        if mode == "auto":
            self._append_log("Installer mode: auto (pending decision).\n")
            return [["./setup", "install-files"]]
        self._append_log("Installer mode: files-only.\n")
        return [["./setup", "install-files"]]

    # Check for merge/rebase/cherry-pick conflicts
    def _check_and_handle_unmerged_conflicts(self, repo_path: str) -> bool:
        """
        Detect in-progress merge/rebase/cherry-pick operations or unmerged files.

        If conflicts found:
            - Presents user dialog summarizing where conflicts exist.
            - Offers option to abort operations (merge/rebase/cherry-pick) and continue.
            - Aborts update if user cancels or abort fails to clean state.

        Returns:
            bool: True if safe to proceed; False if canceled or unresolved.
        """
        rc_u, out_u, _ = run_git(["diff", "--name-only", "--diff-filter=U"], repo_path)
        unmerged_files = [
            ln for ln in (out_u.splitlines() if rc_u == 0 else []) if ln.strip()
        ]

        # Determine in-progress states via sentinel refs
        merge_in_progress = (
            subprocess.run(
                ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                cwd=repo_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            ).returncode
            == 0
        )
        cherry_in_progress = (
            subprocess.run(
                ["git", "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD"],
                cwd=repo_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            ).returncode
            == 0
        )
        rebase_in_progress = any(
            os.path.isdir(os.path.join(repo_path, ".git", d))
            for d in ("rebase-apply", "rebase-merge")
        )

        if not (
            unmerged_files
            or merge_in_progress
            or rebase_in_progress
            or cherry_in_progress
        ):
            return True

        # Build dialog message
        msg = "Unresolved merge/rebase detected.\n\n"
        if unmerged_files:
            msg += f"Unmerged files: {len(unmerged_files)}\n"
        if merge_in_progress:
            msg += "A merge is in progress.\n"
        if rebase_in_progress:
            msg += "A rebase is in progress.\n"
        if cherry_in_progress:
            msg += "A cherry-pick is in progress.\n"
        msg += "\nAbort the in-progress operation(s) and continue?"

        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text="Conflicts detected",
        )
        dialog.format_secondary_text(msg)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Abort and continue", Gtk.ResponseType.OK)
        resp = dialog.run()
        dialog.destroy()
        if resp != Gtk.ResponseType.OK:
            return False

        # Attempt aborts
        ok = True
        if merge_in_progress:
            self._append_log("[git] merge --abort\n")
            r = subprocess.run(
                ["git", "merge", "--abort"],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                ok = False
                self._append_log(f"[git error] merge --abort: {r.stderr}\n")
        if rebase_in_progress:
            self._append_log("[git] rebase --abort\n")
            r = subprocess.run(
                ["git", "rebase", "--abort"],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                ok = False
                self._append_log(f"[git error] rebase --abort: {r.stderr}\n")
        if cherry_in_progress:
            self._append_log("[git] cherry-pick --abort\n")
            r = subprocess.run(
                ["git", "cherry-pick", "--abort"],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                ok = False
                self._append_log(f"[git error] cherry-pick --abort: {r.stderr}\n")

        # Verify no unmerged remain
        rc_u2, out_u2, _ = run_git(
            ["diff", "--name-only", "--diff-filter=U"], repo_path
        )
        still_unmerged = [
            ln for ln in (out_u2.splitlines() if rc_u2 == 0 else []) if ln.strip()
        ]
        if still_unmerged:
            ok = False
            self._append_log(
                "[git] Unmerged files still present after abort; canceling update.\n"
            )
            self._show_message(
                Gtk.MessageType.ERROR,
                "Unmerged files remain after abort. Resolve conflicts manually before updating.",
            )
        return ok

    # Tray icon support (minimal)
    def _ensure_tray_icon(self):
        """
        Lazily create a status tray icon (if supported) used for full install
        when window hides (e.g. launching kitty). Exceptions are ignored.
        """
        if getattr(self, "_tray_icon", None):
            return
        try:
            icon = Gtk.StatusIcon.new_from_icon_name("illogical-updots")
            icon.set_tooltip_text(APP_TITLE)
            icon.connect("activate", lambda _i: self._restore_from_tray())
            self._tray_icon = icon
        except Exception:
            pass

    def _restore_from_tray(self):
        """Re-show window and hide tray icon after background operation completes."""
        try:
            self.show_all()
            if getattr(self, "_tray_icon", None):
                self._tray_icon.set_visible(False)
        except Exception:
            pass

    # Decide full install in auto mode based on package-related changes
    def _auto_mode_decide_full(self, repo_path: str) -> bool:
        """
        In 'auto' mode attempt to detect if a FULL install is warranted:
            - Look for changes affecting 'sdata' or 'dist-arch' between HEAD and upstream.
            - If upstream unknown, fallback to checking local status for those paths.
            - Prompt user to choose full vs files-only when such changes are found.

        Returns:
            bool: True if user selects full install, False otherwise.
        """
        upstream = None
        try:
            st = getattr(self, "_status", None)
            branch = st.branch if st and st.branch else get_branch(repo_path)
            upstream = (
                st.upstream if st and st.upstream else get_upstream(repo_path, branch)
            )
        except Exception:
            upstream = None
        try:
            run_git(["fetch", "--all", "--prune"], repo_path)
        except Exception:
            pass
        changed = False
        if upstream:
            for rel in ["sdata", "dist-arch"]:
                rc, out, _ = run_git(
                    ["diff", "--name-only", f"HEAD..{upstream}", "--", rel], repo_path
                )
                if rc == 0 and any(line.strip() for line in out.splitlines()):
                    changed = True
                    break
        if not changed and not upstream:
            for rel in ["sdata", "dist-arch"]:
                rc, out, _ = run_git(["status", "--porcelain", "--", rel], repo_path)
                if rc == 0 and any(line.strip() for line in out.splitlines()):
                    changed = True
                    break
        if not changed:
            return False

        dlg = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text="Package-related changes detected",
        )
        dlg.format_secondary_text(
            "Incoming changes found in sdata/dist-arch.\nRun FULL installation?\n\nYes = full (./setup install)\nNo = minimal (./setup install-files)"
        )
        dlg.add_button("No (files-only)", Gtk.ResponseType.NO)
        dlg.add_button("Yes (full)", Gtk.ResponseType.YES)
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.YES

    # Trigger update flow without a pull (test shortcut)
    def _run_update_without_pull(self) -> None:
        """Convenience shortcut to start update workflow directly (Ctrl+I)."""
        self.on_update_clicked(None)

    def _on_key_press(self, _widget, event) -> bool:
        """Global key handler: Ctrl+I to trigger internal update without pull prompt."""
        if event.state & Gdk.ModifierType.CONTROL_MASK and event.keyval in (
            Gdk.KEY_i,
            Gdk.KEY_I,
        ):
            self._run_update_without_pull()
            return True
        return False

    def _auto_refresh(self) -> bool:
        """Timer callback to refresh status. Always returns True to keep repeating."""
        self.refresh_status()
        return True

    def _busy(self, is_busy: bool, hint: str) -> None:
        """
        Set busy UI state:
            - Disables refresh/update buttons.
            - Shows spinner & hint text.
            - Controls sensitivity based on presence of updates.

        Args:
            is_busy: Whether an operation is in progress.
            hint: Short descriptive text for status row.
        """
        self.refresh_btn.set_sensitive(not is_busy)
        can_update = (
            not is_busy and self._status is not None and self._status.has_updates
        )
        self.update_btn.set_sensitive(can_update)
        if hasattr(self, "view_btn"):
            self.view_btn.set_sensitive(can_update)
        if is_busy:
            self.spinner.start()
        else:
            self.spinner.stop()
        self.status_hint.set_text(hint or "")

    def _apply_update_button_style(self) -> None:
        """
        Adjust update button appearance:
            - Adds 'suggested-action' style when updates available.
            - Changes label/tooltip for up-to-date state.
        """
        ctx = self.update_btn.get_style_context()
        self.update_btn.set_sensitive(True)
        if self._status and self._status.has_updates:
            if not ctx.has_class("suggested-action"):
                ctx.add_class("suggested-action")
            self.update_btn.set_label("Update")
            self.update_btn.set_tooltip_text("Pull latest updates from upstream")
        else:
            if ctx.has_class("suggested-action"):
                ctx.remove_class("suggested-action")
            self.update_btn.set_label("Up to date")
            self.update_btn.set_tooltip_text(
                "Re-run install (files-only) even if up to date"
            )

    def _set_labels_for_status(self, st: RepoStatus) -> None:
        """
        Set banner markup & optionally details based on RepoStatus.

        Error handling:
            - If repository invalid, show red banner.
            - If fetch produced warning, show in error panel.
        """
        if not st.ok:
            self.primary_label.set_markup(
                "<span size='xx-large' weight='bold' foreground='red'>Repository error</span>"
            )
            if self.details_label:
                self.details_label.set_text(st.error or "Unknown error")
            return

        if st.fetch_error:
            try:
                self.error_icon.set_from_icon_name(
                    "dialog-warning-symbolic", Gtk.IconSize.MENU
                )
            except Exception:
                pass
            self.error_label.set_text(f"Fetch warning: {st.fetch_error}")
            self.error_revealer.set_reveal_child(True)

        ctx = self.primary_label.get_style_context()
        ctx.remove_class("status-up")
        ctx.remove_class("status-ok")
        ctx.remove_class("status-err")

        if st.behind > 0:
            ctx.add_class("status-up")
            self.primary_label.set_markup(
                f"<span size='xx-large' weight='bold'>Updates available</span>\n"
                f"<span size='large'>{st.behind} new commit(s) to pull</span>"
            )
            if (
                hasattr(self, "small_info_btn")
                and self.small_info_btn
                and bool(SETTINGS.get("show_details_button", True))
            ):
                self.small_info_btn.set_label("Details…")
                self.small_info_btn.show()
            elif hasattr(self, "small_info_btn") and self.small_info_btn:
                self.small_info_btn.hide()
        else:
            self.primary_label.set_markup(
                "<span size='xx-large' weight='bold'>Up to date</span>"
            )
            if hasattr(self, "small_info_btn") and self.small_info_btn:
                self.small_info_btn.hide()

        branch = st.branch or "(unknown)"
        upstream = st.upstream or "(no upstream)"
        changes = (
            f"{st.dirty} file(s) changed locally"
            if st.dirty > 0
            else "Working tree clean"
        )
        ahead = f"{st.ahead} ahead" if st.ahead > 0 else "not ahead"
        behind = f"{st.behind} behind" if st.behind > 0 else "not behind"

        details = [
            f"Repo: {st.repo_path}",
            f"Branch: {branch}",
            f"Upstream: {upstream}",
            f"Status: {changes}",
            f"Sync: {ahead}, {behind}",
        ]
        if self.details_label:
            self.details_label.set_text("\n".join(details))

    def refresh_status(self) -> None:
        """
        Start asynchronous refresh of repository status.

        Spawns a thread to avoid blocking UI. On completion,
        _finish_refresh is scheduled on main loop.
        """

        def refresh_work():
            st = check_repo_status(REPO_PATH)
            GLib.idle_add(self._finish_refresh, st)

        if self._status is None:
            self._busy(True, "Checking for updates...")
        else:
            self._busy(True, "Refreshing...")
        threading.Thread(target=refresh_work, daemon=True).start()

    def _finish_refresh(self, st: RepoStatus) -> None:
        """
        Apply refreshed status to UI (main loop).

        Args:
            st: Newly computed RepoStatus instance.
        """
        self._status = st
        self._set_labels_for_status(st)
        self._apply_update_button_style()
        if hasattr(self, "view_btn"):
            can_view = bool(self._status and self._status.has_updates)
            self.view_btn.set_sensitive(can_view)
            self.view_btn.set_tooltip_text(
                "View commits to be pulled" if can_view else "No updates available"
            )
        self._busy(False, "")

    def _on_banner_clicked(self, _widget, _event) -> bool:
        """
        When banner clicked:
            - If updates exist, open changes dialog.
            - Otherwise show repository info.
        """
        st = getattr(self, "_status", None)
        if st and st.has_updates:
            on_view_changes_quick(self, run_git)
        else:
            self._show_repo_info_dialog()
        return True

    def _show_repo_info_dialog(self) -> None:
        """Open dialog with repository details (branch/upstream/etc)."""
        show_repo_info_dialog(self, run_git)

    def on_refresh_clicked(self, _btn: Gtk.Button) -> None:
        """Refresh button handler."""
        self.refresh_status()

    def on_logs_clicked(self, _btn: Gtk.Button) -> None:
        """Open log history dialog."""
        self._show_logs_dialog()

    def on_settings_clicked(self, _btn: Gtk.Button) -> None:
        """Open settings dialog for adjusting installer behavior & preferences."""
        self._show_settings_dialog()

    def on_about_clicked(self, _item) -> None:
        """Show about dialog (credits/version)."""
        self._show_about_dialog()

    def _show_about_dialog(self) -> None:
        """Internal wrapper for about dialog creation."""
        show_about_dialog(self, APP_TITLE, REPO_PATH, SETTINGS)

    def _show_logs_dialog(self) -> None:
        """Internal wrapper to show aggregated update events."""
        show_logs_dialog(self)

    def _show_settings_dialog(self) -> None:
        """Internal wrapper for settings dialog invocation."""
        show_settings_dialog(
            self, SETTINGS, REPO_PATH, AUTO_REFRESH_SECONDS, _save_settings
        )

    def on_update_clicked(self, _btn: Gtk.Button) -> None:
        """
        Update button handler:
            - Ensures repo status.
            - Decides full vs files-only for 'auto' mode (user prompt).
            - Checks for unresolved conflicts and offers abort sequence.
            - Performs git pull (rebase + autostash).
            - Runs installer as needed.
        """
        if not self._status:
            self.refresh_status()
            return
        repo_path = self._status.repo_path

        mode = str(SETTINGS.get("installer_mode", "auto"))
        if mode == "auto":
            full = self._auto_mode_decide_full(repo_path)
            self._auto_mode_choice = "full" if full else "files-only"

        if not self._check_and_handle_unmerged_conflicts(repo_path):
            self._append_log(
                "[update] Aborted due to unresolved merge/rebase or user cancel.\n"
            )
            return

        self._ensure_console_open()
        self._busy(True, "Updating...")

        def stream(cmd: list[str], cwd: str) -> int:
            """
            Helper to run a command and stream output lines to the log.

            Returns:
                int: Process exit code.
            """
            self._append_log(f"$ {' '.join(cmd)}\n")
            try:
                p = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                assert p.stdout is not None
                for line in iter(p.stdout.readline, ""):
                    if not line:
                        break
                    self._append_log(str(line))
                rc = p.wait()
                self._append_log(f"[exit {rc}]\n")
                return rc
            except Exception as ex:
                self._append_log(f"[error] {ex}\n")
                return 1

        def update_work():
            """Background thread logic for pulling and installing."""
            stashed = False
            if self._status and self._status.dirty > 0:
                self._append_log("Stashing local changes...\n")
                subprocess.run(
                    [
                        "git",
                        "stash",
                        "push",
                        "--include-untracked",
                        "-m",
                        "illogical-updots-auto",
                    ],
                    cwd=repo_path,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                stashed = True

            plan_cmds = self._plan_install_commands()
            pull = subprocess.run(
                ["git", "pull", "--rebase", "--autostash", "--stat"],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            success = pull.returncode == 0

            if success and stashed:
                self._append_log("Restoring stash...\n")
                subprocess.run(
                    ["git", "stash", "pop"],
                    cwd=repo_path,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )

            mode_local = str(SETTINGS.get("installer_mode", "auto"))
            full = False
            if mode_local == "auto":
                full = getattr(self, "_auto_mode_choice", "files-only") == "full"
                self._auto_mode_choice = None
            elif mode_local == "full":
                full = True
            if mode_local in ("auto", "full"):
                if full:
                    # If kitty exists, launch full install externally and minimize UI
                    if shutil.which("kitty") is not None:

                        def _prep_tray():
                            self._ensure_tray_icon()
                            try:
                                if getattr(self, "_tray_icon", None):
                                    self._tray_icon.set_visible(True)
                            except Exception:
                                pass
                            try:
                                self.hide()
                            except Exception:
                                pass
                            self._busy(False, "")
                            return False

                        GLib.idle_add(_prep_tray)

                        def _kitty_work2():
                            try:
                                subprocess.Popen(
                                    [
                                        "kitty",
                                        "-e",
                                        "fish",
                                        "-lc",
                                        f"cd {shlex.quote(repo_path)} && ./setup install",
                                    ]
                                ).wait()
                            except Exception as ex:
                                msg = f"Failed to launch kitty: {ex}"
                                GLib.idle_add(
                                    lambda: (
                                        self._show_message(Gtk.MessageType.ERROR, msg),
                                        False,
                                    )
                                )
                            finally:
                                GLib.idle_add(
                                    lambda: (
                                        self._restore_from_tray(),
                                        self.refresh_status(),
                                        False,
                                    )
                                )

                        threading.Thread(target=_kitty_work2, daemon=True).start()
                        return
                    else:
                        plan_cmds = [["./setup", "install"]]
                else:
                    plan_cmds = [["./setup", "install-files"]]

            setup_path = os.path.join(repo_path, "setup")
            if os.path.isfile(setup_path) and os.access(setup_path, os.X_OK):
                self._append_log("Running installer...\n")
                if "plan_cmds" not in locals() or not plan_cmds:
                    plan_cmds = self._plan_install_commands()
                extra_args = plan_cmds[0][1:]
                try:
                    p = _spawn_setup_install(
                        repo_path,
                        lambda m: self._append_log(str(m)),
                        extra_args=extra_args,
                        capture_stdout=True,
                        auto_input_seq=[],
                        use_pty=bool(SETTINGS.get("use_pty", True)),
                    )
                    self._current_proc = p
                    out = getattr(p, "stdout", None)
                    if p and out is not None:
                        for line in iter(out.readline, ""):
                            if not line:
                                break
                            self._append_log(str(line))
                        rc = p.wait()
                        self._append_log(f"[installer exit {rc}]\n")
                        self._current_proc = None
                        if rc != 0 and "install-files" in extra_args:
                            # Fallback to full install if minimal fails
                            self._append_log("[fallback] Retrying with 'install'...\n")
                            p2 = _spawn_setup_install(
                                repo_path,
                                lambda m: self._append_log(str(m)),
                                extra_args=["install"],
                                capture_stdout=True,
                                auto_input_seq=[],
                                use_pty=bool(SETTINGS.get("use_pty", True)),
                            )
                            self._current_proc = p2
                            out2 = getattr(p2, "stdout", None)
                            if p2 and out2 is not None:
                                for line in iter(out2.readline, ""):
                                    if not line:
                                        break
                                    self._append_log(str(line))
                                rc2 = p2.wait()
                                self._append_log(f"[installer exit {rc2}]\n")
                            self._current_proc = None
                    else:
                        self._append_log("[warn] Installer spawn returned no stdout.\n")
                except Exception as ex:
                    self._append_log(f"[installer error] {ex}\n")
            else:
                self._append_log("No executable './setup' found. Skipping installer.\n")

            GLib.idle_add(
                lambda: self._finish_update(success, pull.stdout, pull.stderr)
            )

        threading.Thread(target=update_work, daemon=True).start()

    def _finish_update(self, success: bool, stdout: str, stderr: str) -> None:
        """
        Finalize update workflow:
            - Reset busy state.
            - Hide console if open.
            - Send desktop notification if enabled.
            - Kick off post-install script if success.
        """
        self._busy(False, "")
        title = "Update complete" if success else "Update failed"
        details = stdout + ("\n" + stderr if stderr else "")
        self._add_log(title, title, details)
        if getattr(self, "log_revealer", None):
            self.log_revealer.set_reveal_child(False)
        self.refresh_status()
        if bool(SETTINGS.get("send_notifications", True)):
            try:
                app = self.get_application()
                if isinstance(app, Gio.Application):
                    notif = Gio.Notification.new(title)
                    notif.set_body("Update succeeded." if success else "Update failed.")
                    app.send_notification("illogical-updots-update", notif)
            except Exception:
                pass
        if success:
            self._run_post_script_if_configured()

    def _run_post_script_if_configured(self) -> None:
        """
        Execute optional post-install script defined in settings.

        Behavior:
            - If path missing or is a directory -> logs error.
            - If executable -> runs directly; else runs through fish.
            - Streams output into console, sends notification on completion.
        """
        path = str(SETTINGS.get("post_script_path") or "").strip()
        if not path:
            return
        self._ensure_console_open()
        self._append_log("\n=== POST-INSTALL SCRIPT ===\n")

        def work():
            try:
                if not os.path.exists(path):
                    self._append_log(
                        f"[post-script error] path does not exist: {path}\n"
                    )
                    return
                if os.path.isdir(path):
                    self._append_log(
                        f"[post-script error] path is a directory: {path}\n"
                    )
                    return
                if os.access(path, os.X_OK):
                    cmd_str = f"exec {shlex.quote(path)}"
                else:
                    cmd_str = f"exec fish {shlex.quote(path)}"
                    self._append_log(
                        "[post-script] script not executable; running via fish interpreter\n"
                    )
                self._append_log(f"$ fish -lc {shlex.quote(cmd_str)}\n")
                p = subprocess.Popen(
                    ["fish", "-lc", cmd_str],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                assert p.stdout is not None
                for line in iter(p.stdout.readline, ""):
                    if not line:
                        break
                    self._append_log(str(line))
                rc = p.wait()
                self._append_log(f"[post-script exit {rc}]\n")
                if bool(SETTINGS.get("send_notifications", True)):
                    try:
                        app = self.get_application()
                        if isinstance(app, Gio.Application):
                            n = Gio.Notification.new("Post script finished")
                            n.set_body(
                                "Exit code 0 (success)"
                                if rc == 0
                                else f"Exit code {rc} (errors)"
                            )
                            app.send_notification("illogical-updots-post-script", n)
                    except Exception:
                        pass
            except Exception as ex:
                self._append_log(f"[post-script error] {ex}\n")

        threading.Thread(target=work, daemon=True).start()

    def _ensure_polkit_agent(self) -> None:
        """Placeholder: not needed (application runs unprivileged)."""
        return

    def _ensure_console_open(self, desired_height: int = 320) -> None:
        """
        Ensure console is visible and input controls are shown.

        Args:
            desired_height: Reserved for future dynamic sizing.
        """
        rev = getattr(self, "log_revealer", None)
        if not rev:
            return
        try:
            rev.set_reveal_child(True)
            if hasattr(self, "log_controls") and self.log_controls:
                self.log_controls.show_all()
        except Exception:
            pass

    def toggle_console(self) -> None:
        """Toggle visibility of console revealer."""
        rev = getattr(self, "log_revealer", None)
        if not rev:
            return
        rev.set_reveal_child(not rev.get_reveal_child())

    def run_install_external(self) -> None:
        """
        Launch external SetupConsole running full install command:
            ./setup install

        Validates executable presence first.
        """
        setup_path = os.path.join(REPO_PATH, "setup")
        if not (os.path.isfile(setup_path) and os.access(setup_path, os.X_OK)):
            self._show_message(Gtk.MessageType.INFO, "No executable './setup' found.")
            return
        console = SetupConsole(self, title="Installer (setup install)")
        console.present()
        console.run_process(
            ["./setup", "install"],
            cwd=REPO_PATH,
            on_finished=self._run_post_script_if_configured,
        )


# -------------------------------------------------------------------
# Shared helper functions (non-method versions for reuse)
# -------------------------------------------------------------------
def _init_log_css(self):
    """
    Apply application CSS to log view. Safe to call multiple times.

    Adds 'log-view' style class to text view for targeted styling.
    """
    css = get_css()
    try:
        provider = Gtk.CssProvider()
        provider.load_from_data(css.encode("utf-8"))
        screen = Gdk.Screen.get_default()
        if screen:
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        if hasattr(self, "log_view"):
            self.log_view.get_style_context().add_class("log-view")
    except Exception:
        pass


def _append_log(self, text: str):
    """
    Append text to log buffer with ANSI formatting translation.

    Implementation details:
        - If widget unrealized, just inserts plain text (scrolling deferred).
        - Maintains scroll at end when visible.
        - Enforces optional line trim limit from settings.
        - Thread-safe via GLib.idle_add when called off main thread.
    """

    def do_append():
        if not hasattr(self, "log_buf") or not hasattr(self, "log_view"):
            return False
        buf = self.log_buf
        lv = self.log_view
        try:
            if not lv.get_realized():
                buf.insert(buf.get_end_iter(), text)
                return False
            buf.get_char_count()
            try:
                insert_ansi_formatted(buf, text)
            except Exception:
                buf.insert(buf.get_end_iter(), text)
            end_offset = buf.get_char_count()
            if lv.get_visible() and lv.get_realized():
                end_it = buf.get_iter_at_offset(end_offset)
                mark = buf.create_mark(None, end_it, False)
                lv.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
            try:
                limit = int(SETTINGS.get("log_max_lines", 0))
                if limit and buf.get_line_count() > limit:
                    start_it = buf.get_start_iter()
                    end_it = buf.get_iter_at_line(buf.get_line_count() - limit)
                    buf.delete(start_it, end_it)
            except Exception:
                pass
        except Exception:
            pass
        return False

    try:
        if threading.current_thread() is threading.main_thread():
            do_append()
        else:
            GLib.idle_add(do_append)
    except Exception:
        pass


def _clear_log_view(self):
    """
    Clear console log buffer (thread-safe).
    Uses idle_add if invoked off main thread.
    """

    def do_clear():
        if hasattr(self, "log_buf"):
            try:
                self.log_buf.set_text("")
            except Exception:
                pass
        return False

    try:
        if threading.current_thread() is threading.main_thread():
            do_clear()
        else:
            GLib.idle_add(do_clear)
    except Exception:
        pass


def _spawn_setup_install(
    repo_path: str,
    logger,
    extra_args: list[str] | None = None,
    capture_stdout: bool = True,
    auto_input_seq: list[str] | None = None,
    use_pty: bool = True,
):
    """
    Spawn the setup installer with progressive fallbacks and optional PTY.

    Strategy:
        1. Try './setup' directly.
        2. Fallback to 'fish ./setup'.
        3. Fallback to 'sh ./setup'.

    PTY usage:
        - When enabled, opens a pseudo terminal to preserve color + interactive prompts.
        - Wraps PTY reads in a custom PTYStdout class delivering line-based reads.

    Auto input:
        - If auto_input_seq provided, sends specified items plus final 'yesforall'.
        - Otherwise always sends 'yesforall'.

    Args:
        repo_path: Path to repo containing setup script.
        logger: Callable accepting message lines.
        extra_args: Additional arguments after './setup'.
        capture_stdout: Whether to capture/stream stdout/stderr.
        auto_input_seq: Optional sequence of string inputs to feed.
        use_pty: Attempt PTY; fallback to pipe on failure.

    Returns:
        subprocess.Popen | None: Process object with p.stdout producing lines.
    """
    import errno
    import io
    import pty

    extra_args = extra_args or []
    base_cmds = [
        ["./setup"] + extra_args,
        ["fish", "./setup"] + extra_args,
        ["sh", "./setup"] + extra_args,
    ]

    def _env():
        """
        Build environment ensuring color-friendly variables.
        Terminates any 'NO_COLOR' to force colorized output.
        """
        env = dict(os.environ)
        env.update(
            {
                "FORCE_COLOR": "1",
                "CLICOLOR": "1",
                "CLICOLOR_FORCE": "1",
                "TERM": "xterm-256color",
            }
        )
        env.pop("NO_COLOR", None)
        return env

    for cmd in base_cmds:
        try:
            master_fd, slave_fd = None, None
            if use_pty:
                try:
                    master_fd, slave_fd = pty.openpty()
                except Exception as ex:
                    logger(f"[pty-warn] failed to open pty: {ex}; fallback no-pty\n")
                    master_fd = slave_fd = None
                    use_pty = False

            if use_pty and master_fd is not None and slave_fd is not None:
                p = subprocess.Popen(
                    cmd,
                    cwd=repo_path,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    env=_env(),
                    close_fds=True,
                )
                try:
                    os.close(slave_fd)
                except Exception:
                    pass
                master_file = os.fdopen(master_fd, "rb", buffering=0)
                text_stream = io.TextIOWrapper(
                    master_file, encoding="utf-8", errors="replace", newline="\n"
                )

                class PTYStdout:
                    """
                    Simple line-buffered reader for PTY streams.

                    Collects characters until newline encountered; returns lines preserving newline.
                    """

                    def __init__(self, stream):
                        self._stream = stream
                        self._buffer = ""

                    def readline(self):
                        while True:
                            chunk = self._stream.read(1)
                            if not chunk:
                                if self._buffer:
                                    out = self._buffer
                                    self._buffer = ""
                                    return out
                                return ""
                            self._buffer += chunk
                            if "\n" in self._buffer:
                                line, rest = self._buffer.split("\n", 1)
                                self._buffer = rest
                                return line + "\n"

                p.stdout = PTYStdout(text_stream)  # type: ignore[attr-defined]
                p._pty_master_fd = master_fd  # type: ignore[attr-defined]
                logger(f"[spawn/pty] {' '.join(cmd)}\n")
            else:
                p = subprocess.Popen(
                    cmd,
                    cwd=repo_path,
                    stdout=subprocess.PIPE if capture_stdout else None,
                    stderr=subprocess.STDOUT if capture_stdout else None,
                    stdin=subprocess.PIPE,
                    universal_newlines=True,
                    bufsize=1,
                    env=_env(),
                )
                logger(f"[spawn] {' '.join(cmd)}\n")

            if auto_input_seq:
                # Feed specified items then yesforall
                def _feed():
                    import time as _t

                    master_fd_local = getattr(p, "_pty_master_fd", None)
                    pipe = p.stdin if master_fd_local is None else None
                    if master_fd_local is None and not pipe:
                        logger(
                            "[auto-input] stdin unavailable; aborting auto sequence\n"
                        )
                        return
                    _t.sleep(0.2)
                    for item in auto_input_seq:
                        try:
                            if master_fd_local is not None:
                                os.write(
                                    master_fd_local, item.encode("utf-8", "replace")
                                )
                            else:
                                if pipe is None or getattr(pipe, "closed", False):
                                    logger("[auto-input] stdin closed; stopping\n")
                                    break
                                os.write(pipe.fileno(), item.encode("utf-8", "replace"))
                            logger(f"[auto-input] {repr(item)}\n")
                        except Exception as _ex:
                            logger(f"[auto-input-error] {_ex}\n")
                            break
                        _t.sleep(0.25)
                    try:
                        yesforall = "yesforall\n"
                        if master_fd_local is not None:
                            os.write(
                                master_fd_local, yesforall.encode("utf-8", "replace")
                            )
                        elif pipe:
                            os.write(
                                pipe.fileno(), yesforall.encode("utf-8", "replace")
                            )
                        logger(f"[auto-input] {repr(yesforall)}\n")
                    except Exception as _ex:
                        logger(f"[auto-input-error] {_ex}\n")

                threading.Thread(target=_feed, daemon=True).start()
            else:
                # Always send baseline 'yesforall' to allow unattended flows
                def _feed_yesforall():
                    import time as _t

                    _t.sleep(0.3)
                    master_fd_local = getattr(p, "_pty_master_fd", None)
                    pipe = p.stdin if master_fd_local is None else None
                    try:
                        msg = "yesforall\n"
                        if master_fd_local is not None:
                            os.write(master_fd_local, msg.encode("utf-8", "replace"))
                        elif pipe:
                            os.write(pipe.fileno(), msg.encode("utf-8", "replace"))
                        logger(f"[auto-input] {repr(msg)}\n")
                    except Exception as _ex:
                        logger(f"[auto-input-error] {_ex}\n")

                threading.Thread(target=_feed_yesforall, daemon=True).start()

            return p
        except OSError as ex:
            if ex.errno == errno.ENOEXEC:
                logger(
                    f"[warn] Exec format error with {' '.join(cmd)}; trying fallback...\n"
                )
                continue
            logger(f"[error] {ex}\n")
            return None
        except Exception as ex:
            logger(f"[error] {ex}\n")
            return None
    logger("[error] All setup execution fallbacks failed.\n")
    return None


def launch_install_external(repo_path: str) -> None:
    """
    Launch full installer in an external terminal emulator.

    Tries known terminals in order; first success returns immediately.
    Falls back to running directly if none found.

    Args:
        repo_path: Path to repository containing setup script.
    """
    terminals = [
        ("kitty", ["kitty", "-e"]),
        ("alacritty", ["alacritty", "-e"]),
        ("gnome-terminal", ["gnome-terminal", "--"]),
        ("xterm", ["xterm", "-e"]),
        ("konsole", ["konsole", "-e"]),
        ("foot", ["foot", "sh", "-c"]),
    ]
    cmd = ["./setup", "install"]
    for name, base in terminals:
        if shutil.which(name):
            full = base + [
                "sh",
                "-c",
                f"cd {shlex.quote(repo_path)} && {shlex.quote(cmd[0])} {cmd[1]}",
            ]
            try:
                subprocess.Popen(full)
                return
            except Exception:
                continue
    subprocess.Popen(cmd, cwd=repo_path)
