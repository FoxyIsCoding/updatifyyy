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
import re
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
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402  # type: ignore

APP_ID = "com.example.updatifyyy"
APP_TITLE = "Updatify"
# Settings (persisted)
SETTINGS_DIR = os.path.join(os.path.expanduser("~"), ".config", "updatifyyy")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")


def _load_settings() -> dict:
    data = {
        "repo_path": os.path.expanduser("~/dots-hyprland"),
        "auto_refresh_seconds": 60,
    }
    try:
        if os.path.isfile(SETTINGS_FILE):
            import json

            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
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
        hb.props.subtitle = REPO_PATH
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
        self.view_btn.connect("clicked", lambda _btn: on_view_changes_clicked(self))
        # Reordered pack_end so right side shows: Update, View changes, Menu (dots)
        # Menu button (dropdown) with Settings and Logs
        menu = Gtk.Menu()
        mi_settings = Gtk.MenuItem(label="Settings")
        mi_settings.connect("activate", self.on_settings_clicked)
        menu.append(mi_settings)
        mi_logs = Gtk.MenuItem(label="Logs")
        mi_logs.connect("activate", self.on_logs_clicked)
        menu.append(mi_logs)
        menu.show_all()

        menu_btn = Gtk.MenuButton()
        menu_btn.set_tooltip_text("Menu")
        menu_btn.set_popup(menu)
        menu_btn.set_image(
            Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON)
        )

        hb.pack_end(self.update_btn)
        hb.pack_end(self.view_btn)
        hb.pack_end(menu_btn)

        # Main content
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(outer)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_border_width(16)
        outer.pack_start(content, True, True, 0)

        # Primary status label
        self.primary_label = Gtk.Label()
        self.primary_label.set_xalign(0.0)
        self.primary_label.set_use_markup(True)
        content.pack_start(self.primary_label, False, False, 0)

        # Secondary details / stats
        self.details_label = Gtk.Label()
        self.details_label.set_xalign(0.0)
        self.details_label.set_selectable(True)
        content.pack_start(self.details_label, False, False, 0)

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

        # Embedded log panel (hidden by default)
        self.log_revealer = Gtk.Revealer()
        self.log_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.log_revealer.set_reveal_child(False)

        log_frame = Gtk.Frame()
        log_frame.set_shadow_type(Gtk.ShadowType.IN)
        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        log_box.set_border_width(6)
        log_frame.add(log_box)

        log_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        log_title = Gtk.Label(label="Update / Install Log")
        log_title.set_xalign(0.0)
        log_header.pack_start(log_title, True, True, 0)
        self.log_clear_btn = Gtk.Button.new_from_icon_name(
            "edit-clear-symbolic", Gtk.IconSize.SMALL_TOOLBAR
        )
        self.log_clear_btn.set_tooltip_text("Clear log")
        self.log_clear_btn.connect("clicked", lambda _b: self._clear_log_view())
        log_header.pack_end(self.log_clear_btn, False, False, 0)
        log_box.pack_start(log_header, False, False, 0)

        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_monospace(True)
        self._init_log_css()
        self.log_buf = self.log_view.get_buffer()

        log_sw = Gtk.ScrolledWindow()
        log_sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        log_sw.add(self.log_view)
        log_box.pack_start(log_sw, True, True, 0)

        self.log_revealer.add(log_frame)
        outer.pack_start(self.log_revealer, True, True, 0)

        # Footer InfoBar for messages
        self.infobar = Gtk.InfoBar()
        self.infobar.set_show_close_button(True)
        self.infobar.connect("response", lambda bar, resp: bar.hide())
        self.info_label = Gtk.Label(xalign=0.0)
        self.info_label.set_line_wrap(True)
        self.info_label.set_max_width_chars(60)
        content_area = self.infobar.get_content_area()
        content_area.add(self.info_label)
        self.infobar.hide()
        outer.pack_end(self.infobar, False, False, 0)

        self.show_all()
        self.connect("key-press-event", self._on_key_press)
        # Removed LogConsole usage; no key-press shortcut for install now.

        # Initial state
        self._status: Optional[RepoStatus] = None
        self._update_logs: list[
            tuple[str, str, str]
        ] = []  # (timestamp, event, details)

        self._busy(False, "")

        # First refresh and periodic checks
        self.refresh_status()
        GLib.timeout_add_seconds(AUTO_REFRESH_SECONDS, self._auto_refresh)

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

    def _apply_update_button_style(self) -> None:
        # Blue and clickable when updates are available; grey/disabled otherwise
        ctx = self.update_btn.get_style_context()
        if self._status and self._status.has_updates:
            self.update_btn.set_sensitive(True)
            if not ctx.has_class("suggested-action"):
                ctx.add_class("suggested-action")  # typically blue in GTK themes
            self.update_btn.set_tooltip_text("Pull latest updates")
        else:
            self.update_btn.set_sensitive(False)
            if ctx.has_class("suggested-action"):
                ctx.remove_class("suggested-action")
            self.update_btn.set_tooltip_text("No updates available")

    def _set_labels_for_status(self, st: RepoStatus) -> None:
        if not st.ok:
            self.primary_label.set_markup(
                "<b>Repository status:</b> <span color='red'>Error</span>"
            )
            self.details_label.set_text(st.error or "Unknown error")
            return

        if st.fetch_error:
            # Non-fatal: show warning on fetch error but continue with whatever info we have
            self._show_message(
                Gtk.MessageType.WARNING,
                f"Fetch warning: {st.fetch_error}",
            )

        # Primary line
        if st.behind > 0:
            self.primary_label.set_markup(
                f"<b>Updates available</b> — {st.behind} new commit(s) to pull"
            )
        else:
            self.primary_label.set_markup("<b>Up to date</b>")

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

    def on_refresh_clicked(self, _btn: Gtk.Button) -> None:
        self.refresh_status()

    def on_logs_clicked(self, _btn: Gtk.Button) -> None:
        self._show_logs_dialog()

    def on_settings_clicked(self, _btn: Gtk.Button) -> None:
        self._show_settings_dialog()

    def _show_logs_dialog(self) -> None:
        if not self._update_logs:
            show_details_dialog(self, "Logs", "No update logs yet.", "")
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

            REPO_PATH = str(
                SETTINGS.get("repo_path") or os.path.expanduser("~/dots-hyprland")
            )
            AUTO_REFRESH_SECONDS = int(
                SETTINGS.get("auto_refresh_seconds", AUTO_REFRESH_SECONDS)
            )

            # Refresh now to reflect new path
            self.refresh_status()
        dialog.destroy()

    def on_update_clicked(self, _btn: Gtk.Button) -> None:
        if not (self._status and self._status.has_updates):
            return
        repo_path = self._status.repo_path

        # Open embedded log panel
        self.log_revealer.set_reveal_child(True)
        self._append_log("\n=== UPDATE START ===\n")
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
                    self._append_log(line)
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
                        "updatifyyy-auto",
                    ],
                    cwd=repo_path,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                stashed = True

            # Pull with streaming already handled by stream() above for consistency if needed,
            # but keep concise summary via subprocess.run to capture stdout/stderr for logs
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

            # If installer exists, stream its output into the embedded log
            setup_path = os.path.join(repo_path, "setup")
            if (
                success
                and os.path.isfile(setup_path)
                and os.access(setup_path, os.X_OK)
            ):
                self._append_log("Launching installer...\n")
                # Reuse the streaming helper to show live logs in the embedded panel
                try:
                    p = subprocess.Popen(
                        ["./setup", "install"],
                        cwd=repo_path,
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
                        self._append_log(line)
                    rc = p.wait()
                    self._append_log(f"[exit {rc}]\n")
                except Exception as ex:
                    self._append_log(f"[error] {ex}\n")

            GLib.idle_add(
                lambda: self._finish_update(success, pull.stdout, pull.stderr)
            )

        threading.Thread(target=update_work, daemon=True).start()

    def _finish_update(self, success: bool, stdout: str, stderr: str) -> None:
        self._busy(False, "")
        title = "Update complete" if success else "Update failed"
        details = stdout + ("\n" + stderr if stderr else "")
        self._add_log(title, title, details)
        self.refresh_status()
        # After update (and installer launch) prompt for tweaks if success
        if success:
            self._post_update_prompt()

    def _post_update_prompt(self) -> None:
        # Ask user whether to apply after-update tweaks
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text="Apply after-update tweaks?",
        )
        dialog.format_secondary_text(
            "Would you like to apply post-update tweaks now?\n"
            "Tweaks will remove hyprland portal override file."
        )
        dialog.add_button("No", Gtk.ResponseType.NO)
        dialog.add_button("Yes", Gtk.ResponseType.YES)
        resp = dialog.run()
        dialog.destroy()

        applied = False
        if resp == Gtk.ResponseType.YES:
            target = os.path.expanduser(
                "~/.config/xdg-desktop-portal/hyprland-portals.conf"
            )
            try:
                if os.path.isfile(target):
                    os.remove(target)
                    applied = True
            except Exception:
                # Ignore failures silently; still notify
                pass

        app = self.get_application()
        if isinstance(app, Gio.Application):
            notification = Gio.Notification.new("Updatify Update")
            if applied:
                notification.set_body(
                    "Tweaks applied (portal config removed). Update successful."
                )
            else:
                notification.set_body("Update successful.")
            try:
                app.send_notification("updatifyyy-update", notification)
            except Exception:
                pass

    # Removed key press handler (console/shortcut no longer used)

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
            ["./setup", "install"], cwd=REPO_PATH, on_finished=self._post_update_prompt
        )

    # Removed auto-respond logic (no embedded console interaction).


class SetupConsole(Gtk.Window):
    """
    Dedicated interactive console window for running the setup installer (or other
    commands). Streams stdout/stderr, supports sending input (Enter, Y, N), Ctrl+C,
    and masked password entry when a sudo/password prompt is detected.
    """

    PASSWORD_PATTERNS = [
        "password for",
        "[sudo] password",
        "sudo password",
        "authentication required",
        "enter password",
        "enter your password",
    ]

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

        self._proc: Optional[subprocess.Popen] = None
        self._password_cached: Optional[str] = None
        self._finished_callback = None

    def _apply_css(self):
        css = """
        .setup-console {
            font-family: "JetBrainsMono Nerd Font", "FiraCode Nerd Font", "Hack Nerd Font",
                          "Cascadia Code PL", "MesloLGS NF", "Noto Sans Symbols2",
                          "Noto Emoji", "DejaVu Sans Mono", monospace;
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
        end = self.buf.get_end_iter()
        self.buf.insert(end, text)
        mark = self.buf.create_mark(None, self.buf.get_end_iter(), False)
        self.textview.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)

    def run_process(self, argv: list[str], cwd: Optional[str] = None, on_finished=None):
        """
        Start the child process and stream its output. When finished, optionally call on_finished().
        """
        self._finished_callback = on_finished
        self._append(f"$ {' '.join(shlex.quote(a) for a in argv)}\n")
        try:
            self._proc = subprocess.Popen(
                argv,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as ex:
            self._append(f"[spawn error] {ex}\n")
            if self._finished_callback:
                self._finished_callback()
            return

        assert self._proc.stdout is not None
        threading.Thread(target=self._stream_loop, daemon=True).start()

    def _stream_loop(self):
        assert self._proc and self._proc.stdout
        for line in iter(self._proc.stdout.readline, ""):
            if not line:
                break
            self._append(line)
            self._maybe_password_prompt(line)
        rc = self._proc.wait()
        self._append(f"[exit {rc}]\n")
        GLib.idle_add(self._after_finish)

    def _after_finish(self):
        if callable(self._finished_callback):
            try:
                self._finished_callback()
            finally:
                self._finished_callback = None

    def _on_send(self, _entry):
        txt = self.input_entry.get_text()
        if txt:
            if not txt.endswith("\n"):
                txt += "\n"
            self._send_text(txt)
        self.input_entry.set_text("")

    def _send_text(self, text: str):
        if self._proc and self._proc.stdin:
            try:
                self._proc.stdin.write(text)
                self._proc.stdin.flush()
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
        low = line.lower()
        if any(p in low for p in self.PASSWORD_PATTERNS):
            if self._password_cached:
                self._append("[auto] reusing cached password\n")
                self._send_text(self._password_cached + "\n")
                return
            dlg = Gtk.Dialog(
                title="Authentication Required",
                transient_for=self,
                flags=0,
            )
            dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
            dlg.add_button("OK", Gtk.ResponseType.OK)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.set_border_width(12)
            content = dlg.get_content_area()
            content.add(box)
            lbl = Gtk.Label(label="Enter password:")
            lbl.set_xalign(0.0)
            box.pack_start(lbl, False, False, 0)
            entry = Gtk.Entry()
            entry.set_visibility(False)
            entry.set_invisible_char("•")
            entry.set_activates_default(True)
            box.pack_start(entry, False, False, 0)
            dlg.set_default_response(Gtk.ResponseType.OK)
            dlg.show_all()
            resp = dlg.run()
            pwd = entry.get_text() if resp == Gtk.ResponseType.OK else ""
            dlg.destroy()
            if pwd:
                self._password_cached = pwd
                self._send_text(pwd + "\n")
                self._append("[auto] password sent\n")

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
            show_details_dialog(self, "Logs", "No update logs yet.", "")
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
    .log-view {
        font-family: "JetBrainsMono Nerd Font", "FiraCode Nerd Font", "Hack Nerd Font", "Cascadia Code PL", "MesloLGS NF", "Noto Sans Symbols2", "Noto Emoji", "DejaVu Sans Mono", monospace;
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
        if hasattr(self, "log_view"):
            self.log_view.get_style_context().add_class("log-view")
    except Exception:
        pass


def _append_log(self, text: str):
    if not hasattr(self, "log_buf"):
        return
    buf = self.log_buf
    end_iter = buf.get_end_iter()
    buf.insert(end_iter, text)
    # Auto scroll
    mark = buf.create_mark(None, buf.get_end_iter(), False)
    self.log_view.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)


def _clear_log_view(self):
    if hasattr(self, "log_buf"):
        self.log_buf.set_text("")


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


def on_view_changes_clicked(window: Gtk.Window) -> None:
    st = getattr(window, "_status", None)
    if not (st and st.upstream):
        show_details_dialog(window, "Changes", "No updates available", "")
        return

    repo_path = st.repo_path
    upstream = st.upstream

    def fetch_commits():
        rc, out, err = run_git(
            [
                "log",
                "--pretty=format:%H|%h|%an|%ae|%ad|%s",
                "--date=short",
                f"HEAD..{upstream}",
            ],
            repo_path,
        )
        if rc != 0:
            return None, err or "Failed to load commits."
        lines = [line for line in out.splitlines() if line.strip()]
        commits = []
        for line in lines:
            parts = line.split("|", 5)
            if len(parts) == 6:
                full, short, author, email, date, subject = parts
                commits.append(
                    {
                        "full": full,
                        "short": short,
                        "author": author,
                        "email": email,
                        "date": date,
                        "subject": subject,
                        "avatar": _fetch_github_avatar_url(email),
                    }
                )
        return commits, None

    commits, error = fetch_commits()
    if error:
        show_details_dialog(window, "Changes", "Error", error)
        return
    if not commits:
        show_details_dialog(window, "Changes", "No pending commits", "")
        return

    dialog = Gtk.Dialog(title="Pending Commits", transient_for=window, flags=0)
    dialog.set_default_size(800, min(600, 120 + 32 * len(commits)))
    dialog.add_button("Close", Gtk.ResponseType.CLOSE)

    area = dialog.get_content_area()
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    outer.set_border_width(12)
    area.add(outer)

    header = Gtk.Label()
    header.set_markup(f"<b>{len(commits)} commit(s) to pull</b>")
    header.set_xalign(0.0)
    outer.pack_start(header, False, False, 0)

    sw = Gtk.ScrolledWindow()
    sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    outer.pack_start(sw, True, True, 0)

    list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    sw.add(list_box)

    for c in commits:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        avatar_img = _make_avatar_image(c["avatar"])
        row.pack_start(avatar_img, False, False, 0)

        meta_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        subject_lbl = Gtk.Label()
        subject_lbl.set_xalign(0.0)
        subject_lbl.set_ellipsize(Gtk.EllipsizeMode.END)
        subject_lbl.set_markup(
            f"<span foreground='#00ace6'>{GLib.markup_escape_text(c['short'])}</span> {GLib.markup_escape_text(c['subject'])}"
        )
        meta_box.pack_start(subject_lbl, False, False, 0)

        info_lbl = Gtk.Label()
        info_lbl.set_xalign(0.0)
        info_lbl.set_markup(
            f"<small>{GLib.markup_escape_text(c['author'])} — {GLib.markup_escape_text(c['date'])}</small>"
        )
        meta_box.pack_start(info_lbl, False, False, 0)

        row.pack_start(meta_box, True, True, 0)
        list_box.pack_start(row, False, False, 0)
    dialog.show_all()
    dialog.run()
    dialog.destroy()

    # Show busy indicator
    if hasattr(window, "_busy"):
        window._busy(True, "Loading commit list...")

    repo_path = st.repo_path
    upstream = st.upstream

    def work():
        rc, out, err = run_git(
            [
                "log",
                "--pretty=format:%h  %ad  %s  (%an)",
                "--date=short",
                f"HEAD..{upstream}",
            ],
            repo_path,
        )
        text = out if rc == 0 else (err or "Failed to load commits.")

        def done():
            if hasattr(window, "_busy"):
                window._busy(False, "")
            title = "Pending commits" if rc == 0 else "Error loading commits"
            summary = f"{st.behind} commit(s) will be pulled" if rc == 0 else ""
            show_details_dialog(window, title, summary, text.strip())

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
    # Ensure setup script uses polkitexec wrappers (polkit via pkexec)
    try:
        setup_path = os.path.join(repo_path, "setup")
        if os.path.isfile(setup_path) and os.access(setup_path, os.R_OK):
            with open(setup_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            if "UPDATIFYYY_POLKIT_PATCHED" not in content:
                header = '# UPDATIFYYY_POLKIT_PATCHED\npolkitexec() { command -v pkexec >/dev/null 2>&1 && pkexec "$@" || "$@"; }\n'
                content = header + content
            # Replace 'sudo ' with 'polkitexec '
            content = re.sub(r"(?m)(?<![\\w-])sudo\\s+", "polkitexec ", content)
            # Replace 'yay ' with 'polkitexec yay '
            content = re.sub(r"(?m)(?<![\\w-])yay\\s+", "polkitexec yay ", content)
            with open(setup_path, "w", encoding="utf-8") as f:
                f.write(content)
            # Ensure executable bit
            os.chmod(setup_path, os.stat(setup_path).st_mode | 0o111)
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
        super().do_shutdown()


def main(argv: Optional[list[str]] = None) -> int:
    app = App()
    return app.run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
