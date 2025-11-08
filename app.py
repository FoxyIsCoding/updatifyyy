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
from gi.repository import Gdk, GLib, Gtk  # noqa: E402  # type: ignore

APP_ID = "com.example.updatifyyy"
APP_TITLE = "Updatify"
REPO_PATH = os.path.expanduser("~/dots-hyprland")
AUTO_REFRESH_SECONDS = 60


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
        hb.pack_end(self.view_btn)
        self.logs_btn = Gtk.Button(label="Logs")
        self.logs_btn.set_tooltip_text("Show update logs")
        self.logs_btn.connect("clicked", self.on_logs_clicked)
        hb.pack_end(self.logs_btn)

        hb.pack_end(self.update_btn)

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
        def work():
            st = check_repo_status(REPO_PATH)
            GLib.idle_add(self._finish_refresh, st)

        if self._status is None:
            # First load: show busy immediately
            self._busy(True, "Checking for updates...")
        else:
            self._busy(True, "Refreshing...")
        threading.Thread(target=work, daemon=True).start()

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

    def on_update_clicked(self, _btn: Gtk.Button) -> None:
        if not (self._status and self._status.has_updates):
            return
        repo_path = self._status.repo_path
        self._busy(True, "Updating...")

        def work():
            stashed = False
            if self._status and self._status.dirty > 0:
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
            pull = subprocess.run(
                ["git", "pull", "--rebase", "--autostash", "--stat"],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            success = pull.returncode == 0
            if success and stashed:
                subprocess.run(
                    ["git", "stash", "pop"],
                    cwd=repo_path,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
            # Launch installer in external terminal (if present)
            setup_path = os.path.join(repo_path, "setup")
            if (
                success
                and os.path.isfile(setup_path)
                and os.access(setup_path, os.X_OK)
            ):
                launch_install_external(repo_path)
            GLib.idle_add(
                lambda: self._finish_update(success, pull.stdout, pull.stderr)
            )

        threading.Thread(target=work, daemon=True).start()

    def _finish_update(self, success: bool, stdout: str, stderr: str) -> None:
        self._busy(False, "")
        title = "Update complete" if success else "Update failed"
        details = stdout + ("\n" + stderr if stderr else "")
        self._add_log(title, title, details)
        self.refresh_status()

    # Removed key press handler (console/shortcut no longer used)

    def run_install_external(self) -> None:
        setup_path = os.path.join(REPO_PATH, "setup")
        if not (os.path.isfile(setup_path) and os.access(setup_path, os.X_OK)):
            self._show_message(Gtk.MessageType.INFO, "No executable './setup' found.")
            return
        launch_install_external(REPO_PATH)

    # Removed auto-respond logic (no embedded console interaction).

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

    def _show_message(self, msg_type: Gtk.MessageType, message: str) -> None:
        # Show a footer infobar
        self.infobar.set_message_type(msg_type)
        self.info_label.set_text(message)
        self.infobar.show_all()

    # Removed LogConsole class (console UI eliminated in favor of external terminal)
    # (Removed LogConsole class; external terminal used for installer)

    def _insert_ansi(
        self, buf: Gtk.TextBuffer, iter_end: Gtk.TextIter, text: str
    ) -> None:
        """
        Very lightweight ANSI color parser: supports SGR codes for foreground colors
        and reset. Unknown codes are ignored; nested sequences adjust current tag.
        """
        # Pattern for ANSI escape sequences
        import re

        pattern = re.compile(r"(\x1b\[[0-9;]*m)")
        # Split keeping delimiters
        parts = pattern.split(text)
        current_tag = None
        for part in parts:
            if not part:
                continue
            if part.startswith("\x1b[") and part.endswith("m"):
                codes = part[2:-1].split(";")
                if "0" in codes:
                    current_tag = None
                    continue
                # Foreground color codes 30-37, 90-97
                fg_code = next(
                    (
                        c
                        for c in codes
                        if c.isdigit() and (30 <= int(c) <= 37 or 90 <= int(c) <= 97)
                    ),
                    None,
                )
                if fg_code:
                    color = self._ansi_code_to_color(int(fg_code))
                    tagname = f"fg_{fg_code}"
                    if not buf.get_tag_table().lookup(tagname):
                        buf.create_tag(tagname, foreground=color)
                    current_tag = tagname
                continue
            # Regular text
            if current_tag:
                buf.insert_with_tags_by_name(iter_end, part, current_tag)
            else:
                buf.insert(iter_end, part)
            iter_end = buf.get_end_iter()

    def _ansi_code_to_color(self, code: int) -> str:
        base = {
            30: "#000000",
            31: "#cc0000",
            32: "#4e9a06",
            33: "#c4a000",
            34: "#3465a4",
            35: "#75507b",
            36: "#06989a",
            37: "#d3d7cf",
            90: "#555753",
            91: "#ef2929",
            92: "#8ae234",
            93: "#fce94f",
            94: "#729fcf",
            95: "#ad7fa8",
            96: "#34e2e2",
            97: "#eeeeec",
        }
        return base.get(code, "#ffffff")


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
    # Expect a MainWindow-like object with _status and _busy
    st = getattr(window, "_status", None)
    if not (st and st.upstream):
        show_details_dialog(window, "Changes", "No updates available", "")
        return

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
