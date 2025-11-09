#!/usr/bin/env python3
"""
GTK3 app that checks ~/dots-hyprland for git updates and shows a nice UI.

- If updates are available (local branch is behind its upstream), the Update button
  becomes blue and clickable.
- If no updates are available, the Update button is disabled (grey).
- You can refresh manually or wait for the periodic automatic refresh.

Requirements:
- Python 3
- GTK3 and PyGObject (python3-gi, gir1.2-gtk-3.0)
- git installed and available on PATH
"""

import os

# removed unused import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import (
    Gdk,  # noqa: E402  # type: ignore
    GdkPixbuf,  # noqa: E402  # type: ignore
    Gio,  # noqa: E402  # type: ignore
    GLib,  # noqa: E402  # type: ignore
    Gtk,  # noqa: E402  # type: ignore
    Pango,  # noqa: E402  # type: ignore
)

APP_ID = "com.example.illogical-updots"
APP_TITLE = "illogical-updots"
# Settings (persisted)
SETTINGS_DIR = os.path.join(os.path.expanduser("~"), ".config", "illogical-updots")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")


def _load_settings() -> dict:
    data = {
        "repo_path": os.path.expanduser("~/dots-hyprland"),
        "auto_refresh_seconds": 60,
        "detached_console": False,  # run installer in separate window
        "installer_mode": "files-only",  # "files-only" or "full"
        "use_pty": True,  # PTY for embedded console
        "force_color_env": True,  # force TERM/CLICOLOR env for color
        "send_notifications": True,  # desktop notifications on finish
        "log_max_lines": 5000,  # trim logs to this many lines (0 to disable)
        "changes_lazy_load": True,  # lazy load commits with animations
        "post_script_path": "",  # bash script to run after installer (no root)
        "show_details_button": True,  # show 'Details…' button under banner
    }
    try:
        if os.path.isfile(SETTINGS_FILE):
            import json

            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            # Only allow known keys
            data.update({k: v for k, v in loaded.items() if k in data})
    except Exception:
        pass
    return data


def _save_settings(data: dict) -> None:
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
# Ensure repo path is always a string; fallback to default if missing or None
REPO_PATH = str(SETTINGS.get("repo_path") or os.path.expanduser("~/dots-hyprland"))
AUTO_REFRESH_SECONDS = int(SETTINGS.get("auto_refresh_seconds", 60))


@dataclass
class RepoStatus:
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
        # We only consider "updates available" when behind > 0 (remote has new commits)
        return self.ok and self.behind > 0


def run_git(args: list[str], cwd: str, timeout: int = 15) -> Tuple[int, str, str]:
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
    rc, out, _ = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return out.strip() if rc == 0 else None


def get_upstream(cwd: str, branch: Optional[str]) -> Optional[str]:
    # Try an explicit upstream ref; fall back to origin/<branch>
    rc, out, _ = run_git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd
    )
    if rc == 0:
        return out.strip()
    if branch:
        # Fallback assumption
        return f"origin/{branch}"
    return None


def get_dirty_count(cwd: str) -> int:
    rc, out, _ = run_git(["status", "--porcelain"], cwd)
    if rc != 0:
        return 0
    return len([ln for ln in out.splitlines() if ln.strip()])


def check_repo_status(repo_path: str) -> RepoStatus:
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
        rc_b, out_b, _ = run_git(
            ["rev-list", "--count", f"HEAD..{upstream}"], repo_path
        )
        if rc_b == 0:
            try:
                behind = int(out_b.strip() or "0")
            except ValueError:
                behind = 0
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


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application) -> None:
        super().__init__(application=app, title=APP_TITLE)
        self.set_default_size(520, 280)
        self.set_border_width(0)

        # HeaderBar
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.props.title = APP_TITLE
        self.header_bar = hb
        self.header_bar.props.subtitle = REPO_PATH
        self.set_titlebar(hb)

        # Refresh button on the left (start)
        self.refresh_btn = Gtk.Button.new_from_icon_name(
            "view-refresh", Gtk.IconSize.BUTTON
        )
        self.refresh_btn.set_tooltip_text("Refresh status")
        self.refresh_btn.connect("clicked", self.on_refresh_clicked)
        hb.pack_start(self.refresh_btn)

        # Update button on the right (end)
        self.update_btn = Gtk.Button(label="Update")
        # We'll toggle sensitivity and style dynamically
        self.update_btn.connect("clicked", self.on_update_clicked)
        # View changes button (commits to pull)
        self.view_btn = Gtk.Button(label="View changes")
        self.view_btn.set_tooltip_text("View commits to be pulled")
        self.view_btn.connect("clicked", lambda _btn: on_view_changes_quick(self))
        # Reordered pack_end so right side shows: Update, View changes, Menu (dots)
        # Menu button (dropdown) with Settings and Logs
        menu = Gtk.Menu()
        mi_settings = Gtk.MenuItem(label="Settings")
        mi_settings.connect("activate", self.on_settings_clicked)
        menu.append(mi_settings)

        mi_logs = Gtk.MenuItem(label="Git Logs")
        mi_logs.connect("activate", self.on_logs_clicked)
        menu.append(mi_logs)

        mi_fonts = Gtk.MenuItem(label="Install Nerd Fonts")
        mi_fonts.connect("activate", self.on_install_nerd_fonts_clicked)
        menu.append(mi_fonts)

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
        # Add Nerd Fonts install accessible also via menu item

        # Main content
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(outer)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_border_width(16)
        outer.pack_start(content, True, True, 0)

        # Fullscreen primary banner (only main visible content)
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
        # Make banner clickable to show detailed repo info
        self.primary_label.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.primary_label.connect("button-press-event", self._on_banner_clicked)
        banner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        banner_box.set_hexpand(True)
        banner_box.set_vexpand(True)
        banner_box.pack_start(self.primary_label, True, True, 0)
        # Small info button under banner (hidden by default; shown when updates exist)
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

        # Remove secondary details label (minimal fullscreen banner)
        self.details_label = None

        # Spinner (for background work)
        spin_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        spin_box.set_hexpand(False)
        spin_box.set_vexpand(False)
        self.spinner = Gtk.Spinner()
        spin_box.pack_start(self.spinner, False, False, 0)

        self.status_hint = Gtk.Label(label="")
        self.status_hint.set_xalign(0.0)
        spin_box.pack_start(self.status_hint, False, False, 0)

        content.pack_start(spin_box, False, False, 0)

        # Expandable autohiding embedded log console (revealer)
        self.log_revealer = Gtk.Revealer()
        self.log_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.log_revealer.set_reveal_child(False)

        log_frame = Gtk.Frame()
        log_frame.set_shadow_type(Gtk.ShadowType.IN)
        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        log_box.set_border_width(6)
        log_frame.add(log_box)

        # Header with title and clear/hide controls
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

        # TextView inside scrolled window
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

        # Interactive controls (entry + Y/N/Enter/Ctrl+C)
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

        # Key press mapping for quick Y/N/Enter when focus is in the log view
        self.log_view.connect("key-press-event", self._on_log_key_press)

        self.log_revealer.add(log_frame)
        outer.pack_start(self.log_revealer, False, False, 0)

        # Error panel (kept)
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
        # Removed LogConsole usage; no key-press shortcut for install now.

        # Initial state
        self._status: Optional[RepoStatus] = None
        self._update_logs: list[
            tuple[str, str, str]
        ] = []  # (timestamp, event, details)

        self._busy(False, "")
        self._current_proc = None
        # Initialize sudo keepalive control objects
        self._sudo_keepalive_stop = None
        self._sudo_keepalive_thread = None

        # First refresh and periodic checks
        self.refresh_status()
        GLib.timeout_add_seconds(AUTO_REFRESH_SECONDS, self._auto_refresh)

    # Wrapper methods to call module-level helpers for log panel
    def _init_log_css(self) -> None:
        _init_log_css(self)

    def _append_log(self, text: str) -> None:
        _append_log(self, text)

    def _clear_log_view(self) -> None:
        _clear_log_view(self)

    def _show_message(self, msg_type: Gtk.MessageType, message: str) -> None:
        # Route messages to the top error panel instead of a bottom infobar
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

    def _add_log(self, event: str, summary: str, details: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self._update_logs.append(
            (ts, event, summary + ("\n" + details if details else ""))
        )

    # Sudo / pkexec pre-auth and keepalive
    def _start_sudo_keepalive(self) -> None:
        # Disabled: no background sudo keepalive
        return

    def _ensure_sudo_pre_auth(self) -> None:
        # Disabled: no automatic sudo or polkit pre-auth
        return

    def _patch_setup_for_polkit(self, repo_path: str) -> None:
        # Disabled: do not modify setup script for polkit/sudo
        return

    # Embedded log console helpers
    def _send_to_proc(self, text: str) -> None:
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
        p = getattr(self, "_current_proc", None)
        if p:
            try:
                import signal

                p.send_signal(signal.SIGINT)
                self._append_log("[signal] SIGINT sent\n")
            except Exception as ex:
                self._append_log(f"[ctrl-c error] {ex}\n")

    def _on_log_key_press(self, _widget, event) -> bool:
        # Map Y/N/Enter when log view has focus
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

    def _run_installer_common(
        self, test_mode: bool = False, commands: Optional[list[list[str]]] = None
    ) -> None:
        """
        Run installer commands either embedded (default) or in a detached SetupConsole
        window depending on SETTINGS['detached_console'].
        """
        repo_path = self._status.repo_path if self._status else REPO_PATH
        setup_path = os.path.join(repo_path, "setup")
        detached = bool(SETTINGS.get("detached_console", False))

        # Decide command list
        cmds = commands or [["./setup", "install"]]

        # Ensure sudo credential cache
        # Removed automatic sudo/polkit pre-auth

        if detached:
            # Detached console path
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
                ["bash", "-lc", chained],
                cwd=repo_path,
                on_finished=lambda: (
                    self.refresh_status(),
                    (not test_mode and self._run_post_script_if_configured()),
                ),
            )
            return

        # Embedded path (colored PTY streaming)
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
                            fallback_cmd = ["bash"] + cmd
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

    def _show_nerd_fonts_dialog(self) -> None:
        """
        Simple dialog to choose Nerd Fonts to install.
        Installation runs via background thread; updates appear in log panel.
        """
        fonts = [
            ("JetBrainsMono", "JetBrainsMono"),
            ("FiraCode", "FiraCode"),
            ("Hack", "Hack"),
            ("CascadiaCode", "CascadiaCode"),
            ("Iosevka", "Iosevka"),
            ("Mononoki", "Mononoki"),
            ("Meslo", "MesloLGS NF"),
            ("Symbols Nerd", "SymbolsNerdFont"),
            ("Noto Emoji", "NotoColorEmoji"),
        ]
        dialog = Gtk.Dialog(
            title="Install Nerd Fonts",
            transient_for=self,
            flags=0,
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Install", Gtk.ResponseType.OK)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(12)
        dialog.get_content_area().add(box)
        info = Gtk.Label(
            label="Select fonts to install (downloads to ~/.local/share/fonts/NerdFonts).\nRequires network and write permissions."
        )
        info.set_xalign(0.0)
        box.pack_start(info, False, False, 0)
        checks: list[tuple[Gtk.CheckButton, str]] = []
        for label, key in fonts:
            cb = Gtk.CheckButton.new_with_label(label)
            cb.set_active(label in ("JetBrainsMono", "Symbols Nerd"))
            box.pack_start(cb, False, False, 0)
            checks.append((cb, key))
        dialog.show_all()
        resp = dialog.run()
        if resp != Gtk.ResponseType.OK:
            dialog.destroy()
            return
        selected = [k for cb, k in checks if cb.get_active()]
        dialog.destroy()
        if not selected:
            self._show_message(Gtk.MessageType.INFO, "No fonts selected.")
            return
        lr = getattr(self, "log_revealer", None)
        if lr:
            lr.set_reveal_child(True)
        self._append_log("\n=== NERD FONTS INSTALL ===\n")
        self._busy(True, "Installing fonts...")

        def install_fonts():
            success = True
            target_dir = os.path.expanduser("~/.local/share/fonts/NerdFonts")
            try:
                os.makedirs(target_dir, exist_ok=True)
            except Exception as ex:
                self._append_log(f"[error] mkdir fonts: {ex}\n")
                success = False
            base_url = (
                "https://github.com/ryanoasis/nerd-fonts/releases/latest/download"
            )
            for font in selected:
                archive = f"{font}.tar.xz"
                url = f"{base_url}/{archive}"
                self._append_log(f"Downloading {archive}...\n")
                try:
                    import urllib.request

                    data = urllib.request.urlopen(url, timeout=30).read()
                    tmp = os.path.join(target_dir, archive)
                    with open(tmp, "wb") as f:
                        f.write(data)
                    import tarfile

                    self._append_log(f"Extracting {archive}...\n")
                    with tarfile.open(tmp, "r:xz") as tf:
                        tf.extractall(path=target_dir)
                    os.remove(tmp)
                except Exception as ex:
                    self._append_log(f"[error] {font}: {ex}\n")
                    success = False
            if success:
                self._append_log("Updating font cache...\n")
                try:
                    subprocess.run(
                        ["fc-cache", "-f", "-v"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception:
                    pass

            def done():
                self._busy(False, "")
                self._add_log(
                    "Nerd Fonts Install",
                    "Fonts installation complete"
                    if success
                    else "Fonts installation had errors",
                    ", ".join(selected),
                )
                self._show_message(
                    Gtk.MessageType.INFO,
                    "Nerd Fonts installed. Restart applications to use them."
                    if success
                    else "Some fonts failed to install. Check log.",
                )

            GLib.idle_add(done)

        threading.Thread(target=install_fonts, daemon=True).start()

    def _ensure_polkit_keep_auth(self) -> None:
        """
        Install a polkit rules file to allow cached admin authentication (AUTH_ADMIN_KEEP)
        for the current user (and wheel/sudo groups) so pkexec does not prompt repeatedly.
        Best-effort; silently ignores failures.
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
            # Check if already present with same content
            need_write = True
            try:
                with open(rule_path, "r", encoding="utf-8") as f:
                    existing = f.read()
                if "illogical-updots persistent auth rule" in existing and user in existing:
                    need_write = False
            except Exception:
                need_write = True
            if not need_write:
                return
            # Write via pkexec if available, fallback to sudo
            cmd = f"cat > {shlex.quote(rule_path)} <<'EOF'\n{rule_content}\nEOF\nchmod 644 {shlex.quote(rule_path)}"
            if shutil.which("pkexec"):
                subprocess.run(
                    ["pkexec", "bash", "-c", cmd],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            else:
                subprocess.run(
                    ["sudo", "bash", "-c", cmd],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
        except Exception:
            pass

    def _plan_install_commands(self) -> list[list[str]]:
        """
        Simplified plan: always run files-only install.
        """
        mode = str(SETTINGS.get("installer_mode", "files-only"))
        if mode == "full":
            self._append_log("Installer mode: full install.\n")
            return [["./setup", "install"]]
        self._append_log("Installer mode: files-only.\n")
        return [["./setup", "install-files"]]

    def on_install_nerd_fonts_clicked(self, _item):
        self._show_nerd_fonts_dialog()

    def _run_update_without_pull(self) -> None:
        # Backward compatibility: delegate to unified installer in test mode
        plan_cmds = self._plan_install_commands()
        self._run_installer_common(test_mode=True, commands=plan_cmds)

    def _on_key_press(self, _widget, event) -> bool:
        # Ctrl+I triggers test update (no git pull)
        if event.state & Gdk.ModifierType.CONTROL_MASK and event.keyval in (
            Gdk.KEY_i,
            Gdk.KEY_I,
        ):
            self._run_update_without_pull()
            return True
        return False

    def _auto_refresh(self) -> bool:
        # Periodic refresh; return True to keep the timer
        self.refresh_status()
        return True

    def _busy(self, is_busy: bool, hint: str) -> None:
        self.refresh_btn.set_sensitive(not is_busy)
        can_update = (
            not is_busy and self._status is not None and self._status.has_updates
        )
        self.update_btn.set_sensitive(can_update)
        # mirror availability for "View changes" button
        if hasattr(self, "view_btn"):
            self.view_btn.set_sensitive(can_update)
        if is_busy:
            self.spinner.start()
        else:
            self.spinner.stop()
        self.status_hint.set_text(hint or "")
        # In minimal mode no log updates to disable

    def _apply_update_button_style(self) -> None:
        # Always keep Update button clickable; change label/tooltip/style based on status
        ctx = self.update_btn.get_style_context()
        self.update_btn.set_sensitive(True)
        if self._status and self._status.has_updates:
            if not ctx.has_class("suggested-action"):
                ctx.add_class("suggested-action")  # typically blue in GTK themes
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
        if not st.ok:
            self.primary_label.set_markup(
                "<span size='xx-large' weight='bold' foreground='red'>Repository error</span>"
            )
            if self.details_label:
                self.details_label.set_text(st.error or "Unknown error")
            return

        if st.fetch_error:
            # Non-fatal fetch warning: show in error panel without using _show_message
            try:
                self.error_icon.set_from_icon_name(
                    "dialog-warning-symbolic", Gtk.IconSize.MENU
                )
            except Exception:
                pass
            self.error_label.set_text(f"Fetch warning: {st.fetch_error}")
            self.error_revealer.set_reveal_child(True)

        # Primary line
        # Reset banner state classes
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
            # Show small details link just below the banner
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
            # ctx.add_class("status-ok")  # removed to avoid green styling
            self.primary_label.set_markup(
                "<span size='xx-large' weight='bold'>Up to date</span>"
            )
            # Hide small details link when up to date
            if hasattr(self, "small_info_btn") and self.small_info_btn:
                self.small_info_btn.hide()

        # Secondary details
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
        def refresh_work():
            st = check_repo_status(REPO_PATH)
            GLib.idle_add(self._finish_refresh, st)

        if self._status is None:
            # First load: show busy immediately
            self._busy(True, "Checking for updates...")
        else:
            self._busy(True, "Refreshing...")
        threading.Thread(target=refresh_work, daemon=True).start()

    def _finish_refresh(self, st: RepoStatus) -> None:
        self._status = st
        self._set_labels_for_status(st)
        self._apply_update_button_style()
        # Update 'View changes' button based on status
        if hasattr(self, "view_btn"):
            can_view = bool(self._status and self._status.has_updates)
            self.view_btn.set_sensitive(can_view)
            self.view_btn.set_tooltip_text(
                "View commits to be pulled" if can_view else "No updates available"
            )
        self._busy(False, "")

    def _on_banner_clicked(self, _widget, _event) -> bool:
        # If updates are available, open the changes dialog; otherwise show repo info
        st = getattr(self, "_status", None)
        if st and st.has_updates:
            on_view_changes_quick(self)
        else:
            self._show_repo_info_dialog()
        return True

    def _show_repo_info_dialog(self) -> None:
        st = self._status
        if not st:
            show_details_dialog(self, "Repository Info", "Status not loaded yet.", "")
            return
        repo_path = st.repo_path
        # Gather extra git info
        status_rc, status_out, status_err = run_git(["status", "--short"], repo_path)
        remote_rc, remote_out, remote_err = run_git(["remote", "-v"], repo_path)
        branch = st.branch or "(unknown)"
        upstream = st.upstream or "(no upstream)"
        summary_lines = [
            f"Repo: {repo_path}",
            f"Branch: {branch}",
            f"Upstream: {upstream}",
            f"Ahead: {st.ahead}",
            f"Behind: {st.behind}",
            f"Dirty files: {st.dirty}",
        ]
        if st.fetch_error:
            summary_lines.append(f"Fetch warning: {st.fetch_error}")
        if st.error:
            summary_lines.append(f"Error: {st.error}")
        summary = "\n".join(summary_lines)
        details_parts = []
        details_parts.append("== git status --short ==")
        details_parts.append(status_out.strip() or "(clean)")
        if status_err.strip():
            details_parts.append("stderr:\n" + status_err.strip())
        details_parts.append("\n== git remote -v ==")
        details_parts.append(remote_out.strip() or "(none)")
        if remote_err.strip():
            details_parts.append("stderr:\n" + remote_err.strip())
        # Include pending commits and diffstat when updates are available
        if st.has_updates and st.upstream:
            log_rc, log_out, log_err = run_git(
                [
                    "log",
                    "--pretty=format:%h %s | %an, %ad",
                    "--date=short",
                    f"HEAD..{st.upstream}",
                ],
                repo_path,
            )
            details_parts.append("\n== commits to pull ==")
            details_parts.append(log_out.strip() or "(none)")
            if log_err.strip():
                details_parts.append("stderr:\n" + log_err.strip())
            diff_rc, diff_out, diff_err = run_git(
                ["diff", "--stat", f"HEAD..{st.upstream}"],
                repo_path,
            )
            details_parts.append("\n== diff stat ==")
            details_parts.append(diff_out.strip() or "(none)")
            if diff_err.strip():
                details_parts.append("stderr:\n" + diff_err.strip())
        details = "\n".join(details_parts)
        full_text = summary + "\n\n" + details

        dialog = Gtk.Window(title="Details…")
        dialog.set_transient_for(self)
        dialog.set_modal(True)
        dialog.set_default_size(900, 600)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_border_width(12)
        dialog.add(outer)

        header = Gtk.Label()
        header.set_markup("<b>Repository Details</b>")
        header.set_xalign(0.0)
        outer.pack_start(header, False, False, 0)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        outer.pack_start(sw, True, True, 0)

        tv = Gtk.TextView()
        tv.set_editable(False)
        tv.set_cursor_visible(False)
        tv.set_monospace(True)
        buf = tv.get_buffer()
        buf.set_text(full_text)
        sw.add(tv)

        dialog.show_all()

    def on_refresh_clicked(self, _btn: Gtk.Button) -> None:
        self.refresh_status()

    def on_logs_clicked(self, _btn: Gtk.Button) -> None:
        self._show_logs_dialog()

    def on_settings_clicked(self, _btn: Gtk.Button) -> None:
        self._show_settings_dialog()

    def on_about_clicked(self, _item) -> None:
        self._show_about_dialog()

    def _show_about_dialog(self) -> None:
        dialog = Gtk.Window(title=f"About {APP_TITLE}")
        dialog.set_transient_for(self)
        dialog.set_modal(True)
        dialog.set_default_size(700, 520)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_border_width(16)
        dialog.add(outer)

        header = Gtk.Label()
        header.set_use_markup(True)
        header.set_markup(
            f"<span size='xx-large' weight='bold'>{GLib.markup_escape_text(APP_TITLE)}</span>"
        )
        header.set_xalign(0.0)
        outer.pack_start(header, False, False, 0)

        subtitle = Gtk.Label()
        subtitle.set_use_markup(True)
        subtitle.set_markup(
            "<span size='large'>A simple End4 illogical-impulse dotfiles manager and updater.</span>"
        )
        subtitle.set_xalign(0.0)
        subtitle.set_line_wrap(True)
        subtitle.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        outer.pack_start(subtitle, False, False, 0)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        info_box.set_hexpand(True)
        outer.pack_start(info_box, False, False, 0)

        path_lbl = Gtk.Label()
        path_lbl.set_xalign(0.0)
        path_lbl.set_use_markup(True)
        repo_txt = str(SETTINGS.get("repo_path") or REPO_PATH)
        path_lbl.set_markup(f"<b>Repository:</b> {GLib.markup_escape_text(repo_txt)}")
        info_box.pack_start(path_lbl, False, False, 0)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.set_min_content_height(320)
        outer.pack_start(sw, True, True, 0)

        body = Gtk.TextView()
        body.set_editable(False)
        body.set_cursor_visible(False)
        body.set_monospace(True)
        buf = body.get_buffer()
        buf.set_text(
            "Features:\n"
            " - Detects when your local branch is behind its upstream\n"
            " - Shows a changes view with avatars and animations\n"
            " - Provides an interactive embedded console with color output\n"
            " - Runs your setup installer and an optional post-install script\n\n"
            "Tip: Configure the repository path and options in Settings."
        )
        sw.add(body)

        dialog.show_all()

    def _show_logs_dialog(self) -> None:
        if not self._update_logs:
            show_details_dialog(self, "Git Logs", "No update logs yet.", "")
            return
        brief_lines = [
            f"{ts} | {event} | {summary.splitlines()[0] if summary else ''}"
            for (ts, event, summary) in self._update_logs
        ]
        brief_body = "\n".join(brief_lines)
        expanded = "\n\n----\n\n".join(
            f"{ts}\nEvent: {event}\n{summary}"
            for (ts, event, summary) in self._update_logs
        )
        show_details_dialog(self, "Update Logs", brief_body, expanded)

    def _show_settings_dialog(self) -> None:
        global REPO_PATH, AUTO_REFRESH_SECONDS
        dialog = Gtk.Dialog(
            title="Settings",
            transient_for=self,
            flags=0,
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Save", Gtk.ResponseType.OK)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(12)
        content = dialog.get_content_area()
        content.add(box)

        # Repo path row
        repo_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl_repo = Gtk.Label(label="Repository path:")
        lbl_repo.set_xalign(0.0)
        repo_row.pack_start(lbl_repo, False, False, 0)

        entry_repo = Gtk.Entry()
        entry_repo.set_hexpand(True)
        entry_repo.set_text(SETTINGS.get("repo_path", REPO_PATH) or "")
        repo_row.pack_start(entry_repo, True, True, 0)

        browse_btn = Gtk.Button.new_from_icon_name(
            "folder-open-symbolic", Gtk.IconSize.BUTTON
        )
        browse_btn.set_tooltip_text("Browse for repository folder")

        def on_browse(_btn):
            chooser = Gtk.FileChooserDialog(
                title="Select repository directory",
                transient_for=self,
                action=Gtk.FileChooserAction.SELECT_FOLDER,
            )
            chooser.add_buttons(
                "Cancel", Gtk.ResponseType.CANCEL, "Select", Gtk.ResponseType.OK
            )
            try:
                start_dir = entry_repo.get_text().strip() or os.path.expanduser("~")
                if os.path.isdir(start_dir):
                    chooser.set_current_folder(start_dir)
            except Exception:
                pass
            resp = chooser.run()
            if resp == Gtk.ResponseType.OK:
                filename = chooser.get_filename()
                if filename:
                    entry_repo.set_text(filename)
            chooser.destroy()

        browse_btn.connect("clicked", on_browse)
        repo_row.pack_start(browse_btn, False, False, 0)
        box.pack_start(repo_row, False, False, 0)

        # Auto refresh interval row
        refresh_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl_ref = Gtk.Label(label="Auto refresh (s):")
        lbl_ref.set_xalign(0.0)
        refresh_row.pack_start(lbl_ref, False, False, 0)

        entry_refresh = Gtk.Entry()
        entry_refresh.set_width_chars(6)
        entry_refresh.set_text(
            str(SETTINGS.get("auto_refresh_seconds", AUTO_REFRESH_SECONDS))
        )
        refresh_row.pack_start(entry_refresh, False, False, 0)
        box.pack_start(refresh_row, False, False, 0)

        # Detached console toggle
        console_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        cb_detached = Gtk.CheckButton.new_with_label("Use detached installer console")
        cb_detached.set_active(bool(SETTINGS.get("detached_console", False)))
        cb_detached.set_tooltip_text(
            "If enabled, installer runs in a separate window with its own interactive console."
        )
        console_row.pack_start(cb_detached, False, False, 0)
        box.pack_start(console_row, False, False, 0)

        # Installer mode
        mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl_mode = Gtk.Label(label="Installer mode:")
        lbl_mode.set_xalign(0.0)
        mode_row.pack_start(lbl_mode, False, False, 0)
        cmb_mode = Gtk.ComboBoxText()
        cmb_mode.append_text("files-only")
        cmb_mode.append_text("full")
        current_mode = str(SETTINGS.get("installer_mode", "files-only"))
        if current_mode not in ("files-only", "full"):
            current_mode = "files-only"
        cmb_mode.set_active(0 if current_mode == "files-only" else 1)
        mode_row.pack_start(cmb_mode, False, False, 0)
        box.pack_start(mode_row, False, False, 0)

        # Console options
        console_opts = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        cb_pty = Gtk.CheckButton.new_with_label("Embedded console: use PTY")
        cb_pty.set_active(bool(SETTINGS.get("use_pty", True)))
        console_opts.pack_start(cb_pty, False, False, 0)

        cb_color = Gtk.CheckButton.new_with_label("Force color environment")
        cb_color.set_active(bool(SETTINGS.get("force_color_env", True)))
        cb_color.set_tooltip_text(
            "Sets TERM/CLICOLOR/CLICOLOR_FORCE for colorized output"
        )
        console_opts.pack_start(cb_color, False, False, 0)

        cb_notify = Gtk.CheckButton.new_with_label("Send notifications")
        cb_notify.set_active(bool(SETTINGS.get("send_notifications", True)))
        console_opts.pack_start(cb_notify, False, False, 0)
        box.pack_start(console_opts, False, False, 0)

        # Log options
        log_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl_log = Gtk.Label(label="Max log lines (0=unlimited):")
        lbl_log.set_xalign(0.0)
        log_row.pack_start(lbl_log, False, False, 0)
        spin_log = Gtk.SpinButton()
        spin_log.set_range(0, 100000)
        spin_log.set_increments(100, 1000)
        spin_log.set_value(float(int(SETTINGS.get("log_max_lines", 5000))))
        log_row.pack_start(spin_log, False, False, 0)
        box.pack_start(log_row, False, False, 0)

        # Changes view options
        changes_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        cb_lazy = Gtk.CheckButton.new_with_label("Lazy load commits with animations")
        cb_lazy.set_active(bool(SETTINGS.get("changes_lazy_load", True)))
        changes_row.pack_start(cb_lazy, False, False, 0)
        box.pack_start(changes_row, False, False, 0)

        # Show 'Details…' button under banner
        details_btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        cb_details_btn = Gtk.CheckButton.new_with_label(
            "Show 'Details…' button under banner"
        )
        cb_details_btn.set_active(bool(SETTINGS.get("show_details_button", True)))
        details_btn_row.pack_start(cb_details_btn, False, False, 0)
        box.pack_start(details_btn_row, False, False, 0)

        # Post-install script
        post_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl_post = Gtk.Label(label="Post-install script:")
        lbl_post.set_xalign(0.0)
        post_row.pack_start(lbl_post, False, False, 0)
        entry_post = Gtk.Entry()
        entry_post.set_hexpand(True)
        entry_post.set_placeholder_text("/path/to/script.sh")
        entry_post.set_text(str(SETTINGS.get("post_script_path", "") or ""))
        post_row.pack_start(entry_post, True, True, 0)
        browse_post = Gtk.Button.new_from_icon_name(
            "folder-open-symbolic", Gtk.IconSize.BUTTON
        )
        browse_post.set_tooltip_text("Browse for post-install script")

        def on_browse_post(_btn):
            chooser = Gtk.FileChooserDialog(
                title="Select script",
                transient_for=self,
                action=Gtk.FileChooserAction.OPEN,
            )
            chooser.add_buttons(
                "Cancel", Gtk.ResponseType.CANCEL, "Select", Gtk.ResponseType.OK
            )
            try:
                start_dir = os.path.dirname(
                    entry_post.get_text().strip() or os.path.expanduser("~")
                )
                if os.path.isdir(start_dir):
                    chooser.set_current_folder(start_dir)
            except Exception:
                pass
            resp2 = chooser.run()
            if resp2 == Gtk.ResponseType.OK:
                filename = chooser.get_filename()
                if filename:
                    entry_post.set_text(filename)
            chooser.destroy()

        browse_post.connect("clicked", on_browse_post)
        post_row.pack_start(browse_post, False, False, 0)
        box.pack_start(post_row, False, False, 0)

        dialog.show_all()
        resp = dialog.run()
        if resp == Gtk.ResponseType.OK:
            new_repo = entry_repo.get_text().strip()
            new_refresh_raw = entry_refresh.get_text().strip()
            try:
                new_refresh = int(new_refresh_raw)
                if new_refresh <= 0:
                    raise ValueError
            except ValueError:
                new_refresh = AUTO_REFRESH_SECONDS

            if new_repo and os.path.isdir(new_repo):
                SETTINGS["repo_path"] = new_repo
            else:
                self._show_message(
                    Gtk.MessageType.WARNING,
                    "Invalid repo path (must be an existing directory). Keeping previous.",
                )

            SETTINGS["auto_refresh_seconds"] = new_refresh
            SETTINGS["detached_console"] = cb_detached.get_active()
            # Advanced settings
            SETTINGS["installer_mode"] = (
                "files-only" if cmb_mode.get_active() == 0 else "full"
            )
            SETTINGS["use_pty"] = cb_pty.get_active()
            SETTINGS["force_color_env"] = cb_color.get_active()
            SETTINGS["send_notifications"] = cb_notify.get_active()
            try:
                SETTINGS["log_max_lines"] = int(spin_log.get_value())
            except Exception:
                pass
            SETTINGS["changes_lazy_load"] = cb_lazy.get_active()
            SETTINGS["show_details_button"] = cb_details_btn.get_active()
            SETTINGS["post_script_path"] = entry_post.get_text().strip()
            # removed: ensure_polkit_agent (no longer used)
            # removed: polkit_agent_cmd (no longer used)
            _save_settings(SETTINGS)

            REPO_PATH = str(
                SETTINGS.get("repo_path") or os.path.expanduser("~/dots-hyprland")
            )
            AUTO_REFRESH_SECONDS = int(
                SETTINGS.get("auto_refresh_seconds", AUTO_REFRESH_SECONDS)
            )

            # Refresh now to reflect new path
            if hasattr(self, "header_bar"):
                try:
                    self.header_bar.props.subtitle = REPO_PATH
                except Exception:
                    pass
            self.refresh_status()
        dialog.destroy()

    def on_update_clicked(self, _btn: Gtk.Button) -> None:
        # Allow update even when up to date (will run installer plan accordingly)
        if not self._status:
            # If status isn't ready yet, force a refresh then bail
            self.refresh_status()
            return
        repo_path = self._status.repo_path

        # Show embedded console and mark busy
        self._ensure_console_open()
        self._busy(True, "Updating...")

        def stream(cmd: list[str], cwd: str) -> int:
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

            # Pull with streaming already handled by stream() above for consistency if needed,
            # but keep concise summary via subprocess.run to capture stdout/stderr for logs
            # Decide installer plan based on pending commits before pulling
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

            # If installer exists, stream its output into the embedded log with PTY/colors
            setup_path = os.path.join(repo_path, "setup")
            if os.path.isfile(setup_path) and os.access(setup_path, os.X_OK):
                self._append_log("Running installer...\n")
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
                    out = getattr(p, "stdout", None)
                    if p and out is not None:
                        for line in iter(out.readline, ""):
                            if not line:
                                break
                            self._append_log(str(line))
                        rc = p.wait()
                        self._append_log(f"[installer exit {rc}]\n")
                        # Fallback: if install-files failed, retry plain install
                        if rc != 0 and "install-files" in extra_args:
                            self._append_log("[fallback] Retrying with 'install'...\n")
                            p2 = _spawn_setup_install(
                                repo_path,
                                lambda m: self._append_log(str(m)),
                                extra_args=["install"],
                                capture_stdout=True,
                                auto_input_seq=[],
                                use_pty=bool(SETTINGS.get("use_pty", True)),
                            )
                            out2 = getattr(p2, "stdout", None)
                            if p2 and out2 is not None:
                                for line in iter(out2.readline, ""):
                                    if not line:
                                        break
                                    self._append_log(str(line))
                                rc2 = p2.wait()
                                self._append_log(f"[installer exit {rc2}]\n")
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
        self._busy(False, "")
        title = "Update complete" if success else "Update failed"
        details = stdout + ("\n" + stderr if stderr else "")
        self._add_log(title, title, details)
        # Auto-hide console after finishing
        if getattr(self, "log_revealer", None):
            self.log_revealer.set_reveal_child(False)
        self.refresh_status()
        # After installer completes, run configured post-install script (if any)
        if success:
            self._run_post_script_if_configured()

    def _run_post_script_if_configured(self) -> None:
        """
        If a post-install script path is configured, run it via pkexec without prompting.
        Streams output to the embedded console when available.
        """
        path = str(SETTINGS.get("post_script_path") or "").strip()
        if not path:
            return

        # Reveal console for visibility
        self._ensure_console_open()

        self._append_log("\n=== POST-INSTALL SCRIPT ===\n")

        # Running unprivileged; no polkit needed

        def work():
            try:
                # Build command: if script isn't executable, run it via bash interpreter
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
                    cmd_str = f"exec bash {shlex.quote(path)}"
                    self._append_log(
                        "[post-script] script not executable; running via bash interpreter\n"
                    )
                self._append_log(f"$ bash -lc {shlex.quote(cmd_str)}\n")
                p = subprocess.Popen(
                    ["bash", "-lc", cmd_str],
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
            except Exception as ex:
                self._append_log(f"[post-script error] {ex}\n")

        threading.Thread(target=work, daemon=True).start()

    def _ensure_polkit_agent(self) -> None:
        """
        Removed: post-install runs unprivileged; no polkit agent handling needed.
        """
        return

    # Removed key press handler (console/shortcut no longer used)

    def _ensure_console_open(self, desired_height: int = 320) -> None:
        """
        Ensure the embedded console is visible.
        """
        rev = getattr(self, "log_revealer", None)
        if not rev:
            return
        try:
            rev.set_reveal_child(True)
            # Always show input controls when console visible
            if hasattr(self, "log_controls") and self.log_controls:
                self.log_controls.show_all()
        except Exception:
            pass

    def toggle_console(self) -> None:
        """
        Toggle embedded console visibility.
        """
        rev = getattr(self, "log_revealer", None)
        if not rev:
            return
        rev.set_reveal_child(not rev.get_reveal_child())

    def run_install_external(self) -> None:
        """
        Launch the setup installer in its own interactive log window (SetupConsole).
        Provides live output and allows sending input (Y/N/Enter, password) directly.
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

    # Removed auto-respond logic (no embedded console interaction).


class SetupConsole(Gtk.Window):
    """
    Dedicated interactive console window for running the setup installer (or other
    commands). Streams stdout/stderr, supports sending input (Enter, Y, N), Ctrl+C,
    and masked password entry when a sudo/password prompt is detected.
    """

    # PASSWORD_PATTERNS disabled (no auto password detection)
    PASSWORD_PATTERNS: list[str] = []

    def __init__(self, parent: Gtk.Window, title: str = "Setup Console"):
        super().__init__(title=title, transient_for=parent)
        self.set_default_size(820, 500)
        self.set_border_width(0)

        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.props.title = title
        self.set_titlebar(hb)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_border_width(8)
        self.add(outer)

        # Log view
        self.textview = Gtk.TextView()
        self.textview.set_editable(False)
        self.textview.set_cursor_visible(False)
        self.textview.set_monospace(True)
        self._apply_css()

        self.buf = self.textview.get_buffer()
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.textview)
        outer.pack_start(sw, True, True, 0)
        # Ensure ANSI tags exist for console highlighting
        try:
            # Create a tiny hidden buffer to initialize tags used by _insert_ansi_formatted
            _insert_ansi_formatted(self.buf, "")
        except Exception:
            pass

        # Controls
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        self.input_entry = Gtk.Entry()
        self.input_entry.set_placeholder_text("Type input (Enter to send)")
        self.input_entry.connect("activate", self._on_send)
        controls.pack_start(self.input_entry, True, True, 0)

        for label, payload in [("Y", "y\n"), ("N", "n\n"), ("Enter", "\n")]:
            btn = Gtk.Button(label=label)
            btn.connect("clicked", lambda _b, t=payload: self._send_text(t))
            controls.pack_start(btn, False, False, 0)

        ctrlc_btn = Gtk.Button(label="Ctrl+C")
        ctrlc_btn.connect("clicked", self._on_ctrl_c)
        controls.pack_start(ctrlc_btn, False, False, 0)

        clear_btn = Gtk.Button(label="Clear")
        clear_btn.connect("clicked", lambda _b: self.buf.set_text(""))
        controls.pack_start(clear_btn, False, False, 0)

        outer.pack_end(controls, False, False, 0)

        self.show_all()

        # Track destruction to avoid UI updates after widget is gone
        self._destroyed = False
        try:
            self.connect("destroy", lambda *a: setattr(self, "_destroyed", True))
        except Exception:
            pass

        self._proc: Optional[subprocess.Popen] = None
        self._password_cached: Optional[str] = None
        self._finished_callback = None

    def _apply_css(self):
        css = """
        .setup-console {
            font-size: 12px;
            line-height: 1.25;
        }
        """
        try:
            provider = Gtk.CssProvider()
            provider.load_from_data(css.encode("utf-8"))
            screen = Gdk.Screen.get_default()
            if screen:
                Gtk.StyleContext.add_provider_for_screen(
                    screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
            self.textview.get_style_context().add_class("setup-console")
        except Exception:
            pass

    def _append(self, text: str):
        # Guard against updates after destroy or before realization; degrade gracefully
        try:
            if getattr(self, "_destroyed", False) or not self.textview:
                return
            tv = self.textview
            if not tv.get_realized():
                # If not realized yet, just buffer text without scrolling
                self.buf.insert(self.buf.get_end_iter(), text)
                return
            # Safe insertion using offsets to avoid iterator reuse issues
            start_offset = self.buf.get_char_count()
            self.buf.insert(self.buf.get_end_iter(), text)
            end_offset = self.buf.get_char_count()
            # Scroll only if still visible
            if tv.get_visible():
                start_it = self.buf.get_iter_at_offset(start_offset)
                end_it = self.buf.get_iter_at_offset(end_offset)
                # Create mark at end and scroll
                mark = self.buf.create_mark(None, end_it, False)
                tv.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
        except Exception:
            pass

    def run_process(self, argv: list[str], cwd: Optional[str] = None, on_finished=None):
        """
        Start the child process and stream its output. When finished, optionally call on_finished().
        """
        self._finished_callback = on_finished
        self._append(f"$ {' '.join(shlex.quote(a) for a in argv)}\n")
        try:
            # If argv starts with ./setup attempt robust spawn with fallbacks
            if argv and argv[0] == "./setup":
                self._proc = _spawn_setup_install(
                    cwd or REPO_PATH,
                    lambda msg: self._append(msg),
                    extra_args=argv[1:],
                    capture_stdout=True,
                )
            else:
                env = dict(os.environ)
                env.update(
                    {
                        "TERM": "xterm-256color",
                        "FORCE_COLOR": "1",
                        "CLICOLOR": "1",
                        "CLICOLOR_FORCE": "1",
                    }
                )
                env.pop("NO_COLOR", None)
                self._proc = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                )
        except Exception as ex:
            self._append(f"[spawn error] {ex}\n")
            if self._finished_callback:
                self._finished_callback()
            return

        if not self._proc or not self._proc.stdout:
            self._append("[spawn error] setup failed to start\n")
            if self._finished_callback:
                self._finished_callback()
            return
        threading.Thread(target=self._stream_loop, daemon=True).start()

    def _stream_loop(self):
        assert self._proc and self._proc.stdout
        for line in iter(self._proc.stdout.readline, ""):
            if not line:
                break

            # Schedule UI mutation on main thread to avoid iterator invalidation
            def _append_line(text_line=line):
                try:
                    _insert_ansi_formatted(self.buf, text_line)
                except Exception:
                    self._append(text_line)
                mark = self.buf.create_mark(None, self.buf.get_end_iter(), False)
                self.textview.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
                # Trim console lines if limit configured
                try:
                    limit = int(SETTINGS.get("log_max_lines", 0))
                    if limit and self.buf.get_line_count() > limit:
                        s_it = self.buf.get_start_iter()
                        e_it = self.buf.get_iter_at_line(
                            self.buf.get_line_count() - limit
                        )
                        self.buf.delete(s_it, e_it)
                except Exception:
                    pass
                return False

            GLib.idle_add(_append_line)
            self._maybe_password_prompt(line)
        rc = self._proc.wait()

        def _final():
            try:
                _insert_ansi_formatted(self.buf, f"[exit {rc}]\n")
            except Exception:
                self._append(f"[exit {rc}]\n")
            # Remember exit code for notification logic
            self._last_exit_code = rc
            self._after_finish()
            return False

        GLib.idle_add(_final)

    def _after_finish(self):
        # Run any supplied completion callback first
        if callable(self._finished_callback):
            try:
                self._finished_callback()
            finally:
                self._finished_callback = None
        # Send desktop notification about installer result (detached console case)
        try:
            rc = getattr(self, "_last_exit_code", None)
            # Prefer the window's application, fallback to global default
            app = self.get_application()
            if not app:
                try:
                    app = Gio.Application.get_default()
                except Exception:
                    app = None
            if (
                app
                and rc is not None
                and bool(SETTINGS.get("send_notifications", True))
            ):
                notification = Gio.Notification.new(
                    "Update successful" if rc == 0 else "Update finished (errors)"
                )
                body = (
                    "Installer completed successfully."
                    if rc == 0
                    else f"Installer exited with code {rc}."
                )
                notification.set_body(body)
                try:
                    app.send_notification("illogical-updots-installer", notification)
                except Exception:
                    pass
        except Exception:
            pass
        # Close the console window automatically after process ends
        try:
            self.destroy()
        except Exception:
            pass

    def _on_send(self, _entry):
        txt = self.input_entry.get_text()
        if txt:
            if not txt.endswith("\n"):
                txt += "\n"
            self._send_text(txt)
        self.input_entry.set_text("")

    def _send_text(self, text: str):
        p = self._proc
        if not p:
            return
        try:
            mfd = getattr(p, "_pty_master_fd", None)
            if mfd is not None:
                import os

                os.write(mfd, text.encode("utf-8", "replace"))
            elif p.stdin:
                p.stdin.write(text)
                p.stdin.flush()
            else:
                self._append("[send error] no stdin available\n")
                return
            self._append(f"[sent] {text}")
        except Exception as ex:
            self._append(f"[send error] {ex}\n")

    def _on_ctrl_c(self, _btn):
        if self._proc:
            try:
                import signal

                self._proc.send_signal(signal.SIGINT)
                self._append("[signal] SIGINT sent\n")
            except Exception as ex:
                self._append(f"[ctrl-c error] {ex}\n")

    def _maybe_password_prompt(self, line: str):
        # Disabled: do not auto-handle password prompts
        return

    def _on_key_press(self, _widget, event) -> bool:
        if event.state & Gdk.ModifierType.CONTROL_MASK and event.keyval in (
            Gdk.KEY_i,
            Gdk.KEY_I,
        ):
            self.run_install_external()
            return True
        return False

    def _auto_inject(self, text: str) -> bool:
        # No auto injections; console removed.
        return False
        # Guard against automated inputs while a sudo password prompt is active
        block_until = getattr(self, "_auto_inject_block_until", 0.0)
        if time.time() < block_until:
            return False
        self.console.send_text(text)
        return False

    def _add_log(self, event: str, summary: str, details: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self._update_logs.append((ts, event, summary + "\n" + details))

    def on_logs_clicked(self, _btn: Gtk.Button) -> None:
        self._show_logs_dialog()

    def on_settings_clicked(self, _btn: Gtk.Button) -> None:
        self._show_settings_dialog()

    def _show_logs_dialog(self) -> None:
        if not self._update_logs:
            show_details_dialog(self, "Git Logs", "No update logs yet.", "")
            return
        # Brief list view
        brief_lines = [
            f"{ts} | {event} | {summary.splitlines()[0] if summary else ''}"
            for (ts, event, summary) in self._update_logs
        ]
        brief_body = "\n".join(brief_lines)
        # Full expanded details
        expanded = "\n\n----\n\n".join(
            f"{ts}\nEvent: {event}\n{summary}"
            for (ts, event, summary) in self._update_logs
        )
        show_details_dialog(self, "Update Logs", brief_body, expanded)

    def _show_settings_dialog(self) -> None:
        # Declare globals before any use to avoid "used prior to global declaration" SyntaxError
        global REPO_PATH, AUTO_REFRESH_SECONDS
        global REPO_PATH, AUTO_REFRESH_SECONDS
        dialog = Gtk.Dialog(
            title="Settings",
            transient_for=self,
            flags=0,
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Save", Gtk.ResponseType.OK)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(12)
        content = dialog.get_content_area()
        content.add(box)

        # Repo path
        repo_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl_repo = Gtk.Label(label="Repository path:")
        lbl_repo.set_xalign(0.0)
        repo_row.pack_start(lbl_repo, False, False, 0)
        entry_repo = Gtk.Entry()
        entry_repo.set_text(SETTINGS.get("repo_path", REPO_PATH) or "")
        entry_repo.set_hexpand(True)
        repo_row.pack_start(entry_repo, True, True, 0)

        # Directory picker button
        browse_btn = Gtk.Button.new_from_icon_name(
            "folder-open-symbolic", Gtk.IconSize.BUTTON
        )
        browse_btn.set_tooltip_text("Browse for repository folder")

        def on_browse(_btn):
            chooser = Gtk.FileChooserDialog(
                title="Select repository directory",
                transient_for=self,
                action=Gtk.FileChooserAction.SELECT_FOLDER,
            )
            chooser.add_buttons(
                "Cancel", Gtk.ResponseType.CANCEL, "Select", Gtk.ResponseType.OK
            )
            try:
                start_dir = entry_repo.get_text().strip() or os.path.expanduser("~")
                if os.path.isdir(start_dir):
                    chooser.set_current_folder(start_dir)
            except Exception:
                pass
            resp = chooser.run()
            if resp == Gtk.ResponseType.OK:
                filename = chooser.get_filename()
                if filename:
                    entry_repo.set_text(filename)
            chooser.destroy()

        browse_btn.connect("clicked", on_browse)
        repo_row.pack_start(browse_btn, False, False, 0)
        box.pack_start(repo_row, False, False, 0)

        # Auto refresh interval
        refresh_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl_ref = Gtk.Label(label="Auto refresh (s):")
        lbl_ref.set_xalign(0.0)
        refresh_row.pack_start(lbl_ref, False, False, 0)
        entry_refresh = Gtk.Entry()
        entry_refresh.set_width_chars(6)
        entry_refresh.set_text(
            str(SETTINGS.get("auto_refresh_seconds", AUTO_REFRESH_SECONDS))
        )
        refresh_row.pack_start(entry_refresh, False, False, 0)
        box.pack_start(refresh_row, False, False, 0)

        dialog.show_all()
        resp = dialog.run()
        if resp == Gtk.ResponseType.OK:
            new_repo = entry_repo.get_text().strip()
            new_refresh_raw = entry_refresh.get_text().strip()
            try:
                new_refresh = int(new_refresh_raw)
                if new_refresh <= 0:
                    raise ValueError
            except ValueError:
                new_refresh = AUTO_REFRESH_SECONDS
            if new_repo and os.path.isdir(new_repo):
                SETTINGS["repo_path"] = new_repo
            else:
                self._show_message(
                    Gtk.MessageType.WARNING,
                    "Invalid repo path (must be an existing directory). Keeping previous.",
                )
            SETTINGS["auto_refresh_seconds"] = new_refresh
            _save_settings(SETTINGS)
            # Update globals used elsewhere

            REPO_PATH = str(
                SETTINGS.get("repo_path") or os.path.expanduser("~/dots-hyprland")
            )
            AUTO_REFRESH_SECONDS = int(
                SETTINGS.get("auto_refresh_seconds", AUTO_REFRESH_SECONDS)
            )
            # Force immediate refresh
            self.refresh_status()
        dialog.destroy()

    def _show_message(self, msg_type: Gtk.MessageType, message: str) -> None:
        # Show a footer infobar
        self.infobar.set_message_type(msg_type)
        self.info_label.set_text(message)
        self.infobar.show_all()

    # Wrapper methods to call module-level helpers for log panel
    def _init_log_css(self) -> None:
        _init_log_css(self)

    def _append_log(self, text: str) -> None:
        _append_log(self, text)

    def _clear_log_view(self) -> None:
        _clear_log_view(self)


# Helper functions for embedded log panel and commit avatars


def _init_log_css(self):
    css = """
    .log-view { /* removed - no embedded log */ }
    /* Minimal theme for the main banner/status */
    .status-banner {
        margin-top: 6px;
        margin-bottom: 10px;
    }
    .status-banner.status-up { color: #ffffff; }
    /* status-ok styling removed to avoid green color */
    .status-banner.status-err { color: #ff4d4f; }
    .tiny-link { font-size: 10px; padding: 0 4px; opacity: 0.85; }
    .ansi-bold     { font-weight: bold; }
    .ansi-dim      { opacity: 0.7; }
    .ansi-italic   { font-style: italic; }
    .ansi-underline{ text-decoration: underline; }
    .ansi-red      { color: #ff5555; }
    .ansi-green    { color: #50fa7b; }
    .ansi-yellow   { color: #f1fa8c; }
    .ansi-blue     { color: #8be9fd; }
    .ansi-magenta  { color: #ff79c6; }
    .ansi-cyan     { color: #66d9ef; }
    .ansi-white    { color: #f8f8f2; }
    .ansi-bright-black { color: #6272a4; }
    .ansi-bright-red { color: #ff6e6e; }
    .ansi-bright-green { color: #69ff94; }
    .ansi-bright-yellow { color: #ffffa5; }
    .ansi-bright-blue { color: #9aedfe; }
    .ansi-bright-magenta { color: #ff92df; }
    .ansi-bright-cyan { color: #82e9ff; }
    .ansi-bright-white { color: #ffffff; }
    """
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
    Thread-safe append to the embedded log view with ANSI color/style formatting.
    Defensive against destroyed/unrealized widgets and iterator invalidation.
    """

    def do_append():
        if not hasattr(self, "log_buf") or not hasattr(self, "log_view"):
            return False
        buf = self.log_buf
        lv = self.log_view
        try:
            # If view destroyed or unrealized, degrade to plain append without formatting
            if not lv.get_realized():
                buf.insert(buf.get_end_iter(), text)
                return False
            # Use safe offset-based insertion; attempt ANSI formatting, fallback to plain
            buf.get_char_count()
            try:
                _insert_ansi_formatted(buf, text)
            except Exception:
                buf.insert(buf.get_end_iter(), text)
            end_offset = buf.get_char_count()
            # Scroll only if visible & realized
            if lv.get_visible() and lv.get_realized():
                end_it = buf.get_iter_at_offset(end_offset)
                mark = buf.create_mark(None, end_it, False)
                lv.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
            # Trim log lines if limit configured
            try:
                limit = int(SETTINGS.get("log_max_lines", 0))
                if limit and buf.get_line_count() > limit:
                    start_it = buf.get_start_iter()
                    end_it = buf.get_iter_at_line(buf.get_line_count() - limit)
                    buf.delete(start_it, end_it)
            except Exception:
                pass
        except Exception:
            # Swallow any GTK/Pango issues
            pass
        return False

    try:
        import threading

        if threading.current_thread() is threading.main_thread():
            do_append()
        else:
            GLib.idle_add(do_append)
    except Exception:
        # Fallback: ignore
        pass


def _clear_log_view(self):
    """
    Thread-safe clear of the log buffer (retains ANSI tags definitions).
    """

    def do_clear():
        if hasattr(self, "log_buf"):
            try:
                self.log_buf.set_text("")
            except Exception:
                pass
        return False

    try:
        import threading

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
    Spawn ./setup with ANSI color + interactive support.

    If use_pty is True we allocate a pseudo-terminal so tools think they are in a real
    terminal (preserves colors, interactive prompts). Falls back to direct execution
    methods if PTY allocation fails.

    Returns a Popen object or None. When PTY is used we monkey-patch p.stdout with a
    text wrapper so existing readline loops continue to work.
    """
    import errno
    import io
    import os
    import pty

    extra_args = extra_args or []
    base_cmds = [
        ["./setup"] + extra_args,
        ["bash", "./setup"] + extra_args,
        ["sh", "./setup"] + extra_args,
    ]

    def _env():
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
                # Close slave end in parent; child keeps it
                try:
                    os.close(slave_fd)
                except Exception:
                    pass
                # Wrap master in text IO for readline compatibility (read-only)
                master_file = os.fdopen(master_fd, "rb", buffering=0)
                text_stream = io.TextIOWrapper(
                    master_file, encoding="utf-8", errors="replace", newline="\n"
                )

                class PTYStdout:
                    def __init__(self, stream):
                        self._stream = stream
                        self._buffer = ""

                    def readline(self):
                        # Accumulate until newline or EOF
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
                # Non-PTY fallback
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

            # Auto input sequence (sent after slight delay to allow prompt rendering)
            if auto_input_seq:

                def _feed():
                    import time as _t

                    master_fd = getattr(p, "_pty_master_fd", None)
                    pipe = p.stdin if master_fd is None else None
                    if master_fd is None and not pipe:
                        logger(
                            "[auto-input] stdin unavailable; aborting auto sequence\n"
                        )
                        return
                    _t.sleep(0.2)
                    for item in auto_input_seq:
                        try:
                            if master_fd is not None:
                                os.write(master_fd, item.encode("utf-8", "replace"))
                            else:
                                if pipe is None:
                                    logger("[auto-input] stdin unavailable; stopping\n")
                                    break
                                if getattr(pipe, "closed", False):
                                    logger("[auto-input] stdin closed; stopping\n")
                                    break
                                os.write(pipe.fileno(), item.encode("utf-8", "replace"))
                            logger(f"[auto-input] {repr(item)}\n")
                        except Exception as _ex:
                            logger(f"[auto-input-error] {_ex}\n")
                            break
                        _t.sleep(0.25)
                    # After auto-enters, append yesforall
                    try:
                        yesforall = "yesforall\n"
                        if master_fd is not None:
                            os.write(master_fd, yesforall.encode("utf-8", "replace"))
                        elif pipe:
                            os.write(
                                pipe.fileno(), yesforall.encode("utf-8", "replace")
                            )
                        logger(f"[auto-input] {repr(yesforall)}\n")
                    except Exception as _ex:
                        logger(f"[auto-input-error] {_ex}\n")

                threading.Thread(target=_feed, daemon=True).start()
            else:
                # Even without an explicit auto_input_seq, send a trailing 'yesforall'
                def _feed_yesforall():
                    import os
                    import time as _t

                    _t.sleep(0.3)
                    master_fd = getattr(p, "_pty_master_fd", None)
                    pipe = p.stdin if master_fd is None else None
                    try:
                        msg = "yesforall\n"
                        if master_fd is not None:
                            os.write(master_fd, msg.encode("utf-8", "replace"))
                        elif pipe:
                            os.write(pipe.fileno(), msg.encode("utf-8", "replace"))
                        logger(f"[auto-input] {repr(msg)}\n")
                    except Exception as _ex:
                        logger(f"[auto-input-error] {_ex}\n")

                threading.Thread(target=_feed_yesforall, daemon=True).start()

            return p
        except OSError as ex:
            if ex.errno == errno.ENOEXEC:  # Exec format
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


def _insert_ansi_formatted(buf: Gtk.TextBuffer, raw: str) -> None:
    """
    Parse ANSI escape sequences and apply tags without reusing invalidated iterators.

    Strategy:
    - Scan raw once, splitting into (segment, tag-set) pairs.
    - Insert each segment capturing start_iter BEFORE insertion, end_iter AFTER insertion.
    - Apply tags using iter ranges (no stored iter reused across buffer mutations).
    """
    import re

    # Ensure base tags exist
    base_tags = {
        "ansi-bold": lambda t: t.set_property("weight", Pango.Weight.BOLD),
        "ansi-dim": lambda t: t.set_property("scale", 0.95),
        "ansi-italic": lambda t: t.set_property("style", Pango.Style.ITALIC),
        "ansi-underline": lambda t: t.set_property("underline", Pango.Underline.SINGLE),
        "ansi-red": lambda t: t.set_property("foreground", "#ff5555"),
        "ansi-green": lambda t: t.set_property("foreground", "#50fa7b"),
        "ansi-yellow": lambda t: t.set_property("foreground", "#f1fa8c"),
        "ansi-blue": lambda t: t.set_property("foreground", "#8be9fd"),
        "ansi-magenta": lambda t: t.set_property("foreground", "#ff79c6"),
        "ansi-cyan": lambda t: t.set_property("foreground", "#66d9ef"),
        "ansi-white": lambda t: t.set_property("foreground", "#f8f8f2"),
        "ansi-bright-black": lambda t: t.set_property("foreground", "#6272a4"),
        "ansi-bright-red": lambda t: t.set_property("foreground", "#ff6e6e"),
        "ansi-bright-green": lambda t: t.set_property("foreground", "#69ff94"),
        "ansi-bright-yellow": lambda t: t.set_property("foreground", "#ffffa5"),
        "ansi-bright-blue": lambda t: t.set_property("foreground", "#9aedfe"),
        "ansi-bright-magenta": lambda t: t.set_property("foreground", "#ff92df"),
        "ansi-bright-cyan": lambda t: t.set_property("foreground", "#82e9ff"),
        "ansi-bright-white": lambda t: t.set_property("foreground", "#ffffff"),
    }
    tag_table = buf.get_tag_table()
    for name, init in base_tags.items():
        if tag_table.lookup(name) is None:
            tg = Gtk.TextTag.new(name)
            init(tg)
            tag_table.add(tg)

    sgr_map = {
        "1": "ansi-bold",
        "2": "ansi-dim",
        "3": "ansi-italic",
        "4": "ansi-underline",
        "30": "ansi-bright-black",
        "31": "ansi-red",
        "32": "ansi-green",
        "33": "ansi-yellow",
        "34": "ansi-blue",
        "35": "ansi-magenta",
        "36": "ansi-cyan",
        "37": "ansi-white",
        "90": "ansi-bright-black",
        "91": "ansi-bright-red",
        "92": "ansi-bright-green",
        "93": "ansi-bright-yellow",
        "94": "ansi-bright-blue",
        "95": "ansi-bright-magenta",
        "96": "ansi-bright-cyan",
        "97": "ansi-bright-white",
    }
    bg_map = {
        "40": "#000000",
        "41": "#ff5555",
        "42": "#50fa7b",
        "43": "#f1fa8c",
        "44": "#8be9fd",
        "45": "#ff79c6",
        "46": "#66d9ef",
        "47": "#f8f8f2",
        "100": "#6272a4",
        "101": "#ff6e6e",
        "102": "#69ff94",
        "103": "#ffffa5",
        "104": "#9aedfe",
        "105": "#ff92df",
        "106": "#82e9ff",
        "107": "#ffffff",
    }

    ansi_re = re.compile(r"\x1b\[[0-9;]*m")
    pos = 0
    active = []

    def ensure_xterm_tag(kind: str, idx: int) -> str:
        name = f"ansi-xterm-{kind}-{idx}"
        if tag_table.lookup(name) is None:
            # Build color
            def xterm_color(n: int) -> str:
                if n < 16:
                    base = [
                        "#000000",
                        "#800000",
                        "#008000",
                        "#808000",
                        "#000080",
                        "#800080",
                        "#008080",
                        "#c0c0c0",
                        "#808080",
                        "#ff0000",
                        "#00ff00",
                        "#ffff00",
                        "#0000ff",
                        "#ff00ff",
                        "#00ffff",
                        "#ffffff",
                    ]
                    return base[n]
                if 16 <= n <= 231:
                    n -= 16
                    r = (n // 36) % 6
                    g = (n // 6) % 6
                    b = n % 6
                    conv = [0, 95, 135, 175, 215, 255]
                    return f"#{conv[r]:02x}{conv[g]:02x}{conv[b]:02x}"
                level = 8 + (n - 232) * 10
                return f"#{level:02x}{level:02x}{level:02x}"

            col = xterm_color(idx)
            tg = Gtk.TextTag.new(name)
            if kind == "38":
                tg.set_property("foreground", col)
            else:
                tg.set_property("background", col)
            tag_table.add(tg)
        return name

    while True:
        m = ansi_re.search(raw, pos)
        segment = raw[pos : m.start()] if m else raw[pos:]
        if segment:
            # compute offsets to avoid invalid iterators
            start_offset = buf.get_char_count()
            buf.insert(buf.get_end_iter(), segment)
            end_offset = buf.get_char_count()
            start_iter = buf.get_iter_at_offset(start_offset)
            end_iter = buf.get_iter_at_offset(end_offset)
            for t in active:
                tg = tag_table.lookup(t)
                if tg:
                    buf.apply_tag(tg, start_iter, end_iter)
        if not m:
            break
        seq = m.group()
        codes = seq[2:-1].split(";") if seq != "\x1b[m" else []
        if not codes or any(c == "0" for c in codes):
            active = []
        else:
            i = 0
            while i < len(codes):
                c = codes[i]
                if c in ("38", "48") and i + 2 < len(codes) and codes[i + 1] == "5":
                    try:
                        idx = int(codes[i + 2])
                        active.append(ensure_xterm_tag(c, idx))
                    except Exception:
                        pass
                    i += 3
                    continue
                mapped = sgr_map.get(c)
                if mapped and mapped not in active:
                    active.append(mapped)
                elif c in bg_map:
                    name = f"ansi-bg-{c}"
                    if tag_table.lookup(name) is None:
                        tg = Gtk.TextTag.new(name)
                        tg.set_property("background", bg_map[c])
                        tag_table.add(tg)
                    if name not in active:
                        active.append(name)
                i += 1
        pos = m.end()


def _fetch_github_avatar_url(email: str) -> str:
    """
    Naive attempt to guess GitHub avatar by using local-part as username.
    Returns direct PNG URL if reachable, else empty string.
    """
    import urllib.request

    try:
        local = (email or "").split("@")[0]
        if not local:
            return ""
        url = f"https://github.com/{local}.png"
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status == 200:
                return url
    except Exception:
        pass
    return ""


def _make_avatar_image(url: str) -> Gtk.Image:
    if not url:
        return Gtk.Image.new_from_icon_name(
            "avatar-default-symbolic", Gtk.IconSize.MENU
        )
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = resp.read()
        loader = GdkPixbuf.PixbufLoader()
        loader.write(data)
        loader.close()
        pixbuf = loader.get_pixbuf()
        if pixbuf:
            # Scale to 32x32 preserving aspect
            scaled = pixbuf.scale_simple(32, 32, GdkPixbuf.InterpType.BILINEAR)
            return Gtk.Image.new_from_pixbuf(scaled or pixbuf)
    except Exception:
        pass
    return Gtk.Image.new_from_icon_name("avatar-default-symbolic", Gtk.IconSize.MENU)


def show_details_dialog(
    parent: Gtk.Window, title: str, summary: str, details: str
) -> None:
    dialog = Gtk.Dialog(title=title, transient_for=parent, flags=0)
    dialog.add_button("Close", Gtk.ResponseType.CLOSE)
    content = dialog.get_content_area()

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.set_border_width(12)
    content.add(box)

    summary_lbl = Gtk.Label(label=summary or "")
    summary_lbl.set_xalign(0.0)
    box.pack_start(summary_lbl, False, False, 0)

    sw = Gtk.ScrolledWindow()
    sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    sw.set_min_content_height(240)

    tv = Gtk.TextView()
    tv.set_editable(False)
    tv.set_cursor_visible(False)
    buf = tv.get_buffer()
    buf.set_text(details or "(no details)")

    sw.add(tv)
    box.pack_start(sw, True, True, 0)

    dialog.show_all()
    dialog.run()
    dialog.destroy()


def on_view_changes_quick(window: Gtk.Window) -> None:
    """
    Faster, cleaner changes window that opens immediately and fills asynchronously.
    - Shows commit avatar, subject, author, date and 'ago'
    - If > 15 commits, shows a search box to filter results live
    - Incremental, lazy rendering with per-row reveal animation and lazy avatar fetch
    """
    st = getattr(window, "_status", None)
    if not (st and st.upstream):
        show_details_dialog(window, "Changes", "No updates available", "")
        return
    repo_path = st.repo_path
    upstream = st.upstream

    dialog = Gtk.Window(title="Pending Commits")
    dialog.set_transient_for(window)
    dialog.set_modal(True)
    dialog.set_default_size(1100, 760)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    outer.set_border_width(12)
    dialog.add(outer)

    header = Gtk.Label()
    header.set_markup("<b>Loading commits…</b>")
    header.set_xalign(0.0)
    outer.pack_start(header, False, False, 0)

    tools_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    tools_box.set_hexpand(True)
    outer.pack_start(tools_box, False, False, 0)
    search_entry = Gtk.SearchEntry()
    search_entry.set_placeholder_text("Search commits…")
    search_entry.set_hexpand(True)
    search_entry.hide()
    tools_box.pack_start(search_entry, True, True, 0)

    sw = Gtk.ScrolledWindow()
    sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    outer.pack_start(sw, True, True, 0)

    list_box = Gtk.ListBox()
    list_box.set_selection_mode(Gtk.SelectionMode.NONE)
    sw.add(list_box)

    # Apply CSS for rounded avatar backgrounds
    try:
        provider = Gtk.CssProvider()
        provider.load_from_data(b"""
        .avatar-bg {
            background-color: #2e3440;
            border-radius: 9999px;
            padding: 2px;
        }
        """)
        screen = Gdk.Screen.get_default()
        if screen:
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
    except Exception:
        pass
    dialog.show_all()

    commits_data: list[dict] = []
    row_widgets: list[Gtk.Widget] = []

    def format_ago(iso_str: str) -> str:
        # Expect ISO-like date from git: "YYYY-MM-DD HH:MM:SS +/-HHMM"
        try:
            import time as _t

            try:
                ts = _t.mktime(_t.strptime(iso_str[:19], "%Y-%m-%d %H:%M:%S"))
            except Exception:
                # Fallback: short date only
                ts = _t.mktime(_t.strptime(iso_str[:10], "%Y-%m-%d"))
            now = _t.time()
            delta = max(0, int(now - ts))
            if delta < 60:
                return f"{delta}s ago"
            if delta < 3600:
                return f"{delta // 60}m ago"
            if delta < 86400:
                return f"{delta // 3600}h ago"
            days = delta // 86400
            return f"{days}d ago"
        except Exception:
            return iso_str

    def guess_github_avatar(email: str) -> str:
        # Try to extract username for GitHub-hosted emails, else fallback to local-part guess
        em = email or ""
        local = em.split("@")[0]
        if em.endswith("@users.noreply.github.com"):
            # Formats: 12345+username@users.noreply.github.com or username@users.noreply.github.com
            if "+" in local:
                user = local.split("+", 1)[1]
            else:
                user = local
            return f"https://github.com/{user}.png"
        # Fallback to existing heuristic
        return f"https://github.com/{local}.png" if local else ""

    def build_row(c: dict) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.set_hexpand(True)

        # Placeholder avatar inside rounded background container; lazy-load actual GitHub avatar
        avatar_bg = Gtk.EventBox()
        avatar_bg.set_size_request(36, 36)
        avatar_bg.get_style_context().add_class("avatar-bg")
        avatar = Gtk.Image.new_from_icon_name(
            "avatar-default-symbolic", Gtk.IconSize.MENU
        )
        avatar_bg.add(avatar)
        row.pack_start(avatar_bg, False, False, 0)

        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        meta.set_hexpand(True)

        # First line: short hash + subject
        subject_lbl = Gtk.Label()
        subject_lbl.set_xalign(0.0)
        subject_lbl.set_line_wrap(True)
        subject_lbl.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        subject_lbl.set_use_markup(True)
        subject_markup = (
            f"<span foreground='#00ace6'>{GLib.markup_escape_text(c.get('short', ''))}</span> "
            f"{GLib.markup_escape_text(c.get('subject', ''))}"
        )
        subject_lbl.set_markup(subject_markup)
        meta.pack_start(subject_lbl, False, False, 0)

        # Second line: author — date (ago)
        info_lbl = Gtk.Label()
        info_lbl.set_xalign(0.0)
        info_lbl.set_use_markup(True)
        ago = format_ago(c.get("date_iso", c.get("date", "")))
        info_markup = (
            f"<small>{GLib.markup_escape_text(c.get('author', ''))} — "
            f"{GLib.markup_escape_text(c.get('date', ''))} ({GLib.markup_escape_text(ago)})</small>"
        )
        info_lbl.set_markup(info_markup)
        meta.pack_start(info_lbl, False, False, 0)

        row.pack_start(meta, True, True, 0)

        # Lazy-load avatar in a background thread and update on idle
        def load_avatar():
            try:
                url = c.get("avatar") or guess_github_avatar(c.get("email", ""))
                if not url:
                    return
                import urllib.request

                with urllib.request.urlopen(url, timeout=5) as resp:
                    data = resp.read()
                loader = GdkPixbuf.PixbufLoader()
                loader.write(data)
                loader.close()
                pixbuf = loader.get_pixbuf()
                if pixbuf:

                    def set_img():
                        try:
                            import math

                            import cairo

                            size = 32
                            scaled = (
                                pixbuf.scale_simple(
                                    size, size, GdkPixbuf.InterpType.BILINEAR
                                )
                                or pixbuf
                            )
                            surface = cairo.ImageSurface(
                                cairo.FORMAT_ARGB32, size, size
                            )
                            ctx = cairo.Context(surface)
                            ctx.arc(size / 2.0, size / 2.0, size / 2.0, 0, 2 * math.pi)
                            ctx.clip()
                            Gdk.cairo_set_source_pixbuf(ctx, scaled, 0, 0)
                            ctx.paint()
                            rounded = Gdk.pixbuf_get_from_surface(
                                surface, 0, 0, size, size
                            )
                            if rounded:
                                avatar.set_from_pixbuf(rounded)
                            else:
                                avatar.set_from_pixbuf(scaled)
                        except Exception:
                            avatar.set_from_pixbuf(pixbuf)
                        return False

                    GLib.idle_add(set_img)
            except Exception:
                pass

        threading.Thread(target=load_avatar, daemon=True).start()

        row.show_all()
        return row

    def apply_filter(_entry):
        q = search_entry.get_text().strip().lower()
        children = list_box.get_children()
        if not q:
            for ch in children:
                ch.show()
            return
        for i, ch in enumerate(children):
            if i >= len(commits_data):
                ch.hide()
                continue
            c = commits_data[i]
            hay = " ".join(
                [
                    c.get("short", ""),
                    c.get("subject", ""),
                    c.get("author", ""),
                    c.get("date", ""),
                ]
            ).lower()
            if q in hay:
                ch.show()
            else:
                ch.hide()

    def work():
        rc, out, err = run_git(
            [
                "log",
                "--pretty=format:%H|%h|%an|%ae|%ad|%s",
                "--date=iso",
                f"HEAD..{upstream}",
            ],
            repo_path,
        )
        if rc != 0:
            commits = None
            error = err or "Failed to load commits."
        else:
            lines = [ln for ln in out.splitlines() if ln.strip()]
            commits = []
            for ln in lines:
                parts = ln.split("|", 5)
                if len(parts) == 6:
                    full, short, author, email, date_iso, subject = parts
                    commits.append(
                        {
                            "full": full,
                            "short": short,
                            "author": author,
                            "email": email,
                            "date": date_iso.split(" ")[0],
                            "date_iso": date_iso,
                            "subject": subject,
                            "avatar": guess_github_avatar(email),
                        }
                    )
            error = None

        def done():
            if error:
                header.set_markup("<b>Error</b>")
                # Show error in a single row label
                list_box.foreach(lambda w: list_box.remove(w))
                lbl = Gtk.Label(label=error)
                lbl.set_xalign(0.0)
                list_box.add(lbl)
                dialog.show_all()
                return

            nonlocal commits_data
            commits_data = commits or []
            header.set_markup(f"<b>{len(commits_data)} commit(s) to pull</b>")

            # Clear list and incrementally add rows with reveal animation
            list_box.foreach(lambda w: list_box.remove(w))
            row_widgets.clear()

            index = {"i": 0}

            def add_next():
                i = index["i"]
                if i >= len(commits_data):
                    # Enable search if many
                    if len(commits_data) > 15:
                        search_entry.show()
                        search_entry.connect("changed", apply_filter)
                    return False
                c = commits_data[i]
                index["i"] = i + 1

                # Build row and wrap in revealer for animation
                row = build_row(c)
                revealer = Gtk.Revealer()
                revealer.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
                revealer.set_transition_duration(160)
                revealer.add(row)
                revealer.set_reveal_child(False)
                list_box.add(revealer)
                row_widgets.append(revealer)
                list_box.show_all()

                # Reveal after a tiny delay to animate
                def _reveal():
                    revealer.set_reveal_child(True)
                    return False

                GLib.timeout_add(30, _reveal)

                # Queue next row addition
                GLib.timeout_add(25, add_next)
                return False

            # Kick off incremental rendering
            GLib.idle_add(add_next)
            return False

        GLib.idle_add(done)

    threading.Thread(target=work, daemon=True).start()


def launch_install_external(repo_path: str) -> None:
    # Try common terminal emulators
    terminals = [
        ("kitty", ["kitty", "-e"]),
        ("alacritty", ["alacritty", "-e"]),
        ("gnome-terminal", ["gnome-terminal", "--"]),
        ("xterm", ["xterm", "-e"]),
        ("konsole", ["konsole", "-e"]),
        ("foot", ["foot", "sh", "-c"]),
    ]
    # No script patching; rely on sudo -v keepalive
    try:
        pass
    except Exception:
        pass
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
    # Fallback: run detached without terminal
    subprocess.Popen(cmd, cwd=repo_path)


class App(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)

    def do_activate(self) -> None:  # type: ignore[override]
        if not self.props.active_window:
            MainWindow(self)
        self.props.active_window.present()

    def do_shutdown(self) -> None:  # type: ignore[override]
        # Stop sudo keepalive thread cleanly
        win = self.props.active_window
        if win and hasattr(win, "_sudo_keepalive_stop"):
            try:
                win._sudo_keepalive_stop.set()
                t = getattr(win, "_sudo_keepalive_thread", None)
                if t and t.is_alive():
                    t.join(timeout=1.0)
            except Exception:
                pass
        Gtk.Application.do_shutdown(self)


def main(argv: Optional[list[str]] = None) -> int:
    app = App()
    return app.run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
