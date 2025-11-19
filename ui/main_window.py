#!/usr/bin/env python3
"""
Refactored MainWindow implementation using modular helpers.

- App metadata and settings: core.app_meta
- Git helpers and RepoStatus: core.git_utils
- Console/logging panel: ui.console_panel
- Process/installer helpers: utils.process
- Dialogs: dialogs.*
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import threading
import time
from typing import Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango

# App metadata and settings
from core.app_meta import (
    APP_ID,
    APP_TITLE,
    AUTO_REFRESH_SECONDS,
    REPO_PATH,
    SETTINGS,
)
from core.app_meta import (
    save_settings as _save_settings,
)

# Git utilities
from core.git_utils import (
    RepoStatus,
    check_repo_status,
    get_branch,
    get_upstream,
    run_git,
)

# Dialogs
from dialogs.about import show_about_dialog
from dialogs.changes import on_view_changes_quick
from dialogs.details import show_repo_info_dialog
from dialogs.logs import show_logs_dialog
from dialogs.settings import show_settings_dialog

# Reusable console panel
from ui.console_panel import ConsolePanel

# Process helpers
from utils.process import (
    spawn_setup_install as _spawn_setup_install,
)

# Optional external console widget
from widgets.console import SetupConsole


class MainWindow(Gtk.ApplicationWindow):
    """
    Primary GTK Application window.

    UI:
      - Header bar with Refresh, Update, View changes, Menu
      - Banner label indicating update availability
      - Collapsible ConsolePanel for streaming installer/log output
      - Error message revealer for transient errors/warnings

    Behavior:
      - Asynchronous repo status refresh with periodic auto-refresh
      - Update flow: stash -> pull (rebase/autostash) -> installer
      - Optional external terminal for full installer
      - Ctrl+I: run files-only installer shortcut (no pull)
    """

    def __init__(self, app: Gtk.Application) -> None:
        super().__init__(application=app, title=APP_TITLE)
        self.set_default_size(520, 280)
        self.set_border_width(0)

        # Icons (theme + fallbacks)
        self._init_icons()

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

        # Update button
        self.update_btn = Gtk.Button(label="Update")
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

        # Outer layout
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(outer)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_border_width(16)
        outer.pack_start(content, True, True, 0)

        # Banner
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

        # Small details button
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

        # Details label omitted (minimal design)
        self.details_label = None

        # Spinner + hint
        spin_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.spinner = Gtk.Spinner()
        spin_box.pack_start(self.spinner, False, False, 0)
        self.status_hint = Gtk.Label(label="")
        self.status_hint.set_xalign(0.0)
        spin_box.pack_start(self.status_hint, False, False, 0)
        content.pack_start(spin_box, False, False, 0)

        # Console panel
        self.console = ConsolePanel(settings=SETTINGS)
        outer.pack_start(self.console.revealer, False, False, 0)

        # Error panel
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

        # State
        self._status: Optional[RepoStatus] = None
        self._update_logs: list[tuple[str, str, str]] = []
        self._busy(False, "")
        self._tray_icon = None
        self._auto_mode_choice: Optional[str] = None

        # Initial + periodic refresh
        self.refresh_status()
        GLib.timeout_add_seconds(AUTO_REFRESH_SECONDS, self._auto_refresh)

    # Icons
    def _init_icons(self) -> None:
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
                os.path.join(base_dir, "..", ".github", "assets", "logo.png"),
                os.path.join(base_dir, "..", "assets", "logo.png"),
            ]
            candidates = [
                os.path.abspath(os.path.join(base_dir, p))
                if not p.startswith("/")
                else p
                for p in candidates
            ]
            src = next((p for p in candidates if os.path.isfile(p)), None)
            if not src:
                return
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
                try:
                    self.set_icon_from_file(src)
                except Exception:
                    pass
        except Exception:
            pass

    # Busy state
    def _busy(self, is_busy: bool, hint: str) -> None:
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
        for c in ("status-up", "status-ok", "status-err"):
            ctx.remove_class(c)

        if st.behind > 0:
            ctx.add_class("status-up")
            self.primary_label.set_markup(
                f"<span size='xx-large' weight='bold'>Updates available</span>\n"
                f"<span size='large'>{st.behind} new commit(s) to pull</span>"
            )
            if bool(SETTINGS.get("show_details_button", True)):
                self.small_info_btn.set_label("Details…")
                self.small_info_btn.show()
            else:
                self.small_info_btn.hide()
        else:
            self.primary_label.set_markup(
                "<span size='xx-large' weight='bold'>Up to date</span>"
            )
            self.small_info_btn.hide()

        if self.details_label:
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

    # Error panel
    def _show_message(self, msg_type: Gtk.MessageType, message: str) -> None:
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

    # Update logs list
    def _add_log(self, event: str, summary: str, details: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self._update_logs.append(
            (ts, event, summary + ("\n" + details if details else ""))
        )

    # UI actions
    def on_refresh_clicked(self, _btn: Gtk.Button) -> None:
        self.refresh_status()

    def on_logs_clicked(self, _btn: Gtk.Button) -> None:
        show_logs_dialog(self)

    def on_settings_clicked(self, _btn: Gtk.Button) -> None:
        show_settings_dialog(
            self, SETTINGS, REPO_PATH, AUTO_REFRESH_SECONDS, _save_settings
        )

    def on_about_clicked(self, _item) -> None:
        show_about_dialog(self, APP_TITLE, REPO_PATH, SETTINGS)

    def _show_repo_info_dialog(self) -> None:
        show_repo_info_dialog(self, run_git)

    def _on_banner_clicked(self, _widget, _event) -> bool:
        st = getattr(self, "_status", None)
        if st and st.has_updates:
            on_view_changes_quick(self, run_git)
        else:
            self._show_repo_info_dialog()
        return True

    def _on_key_press(self, _widget, event) -> bool:
        # Ctrl+I: run files-only installer without pulling (quick shortcut)
        if event.state & Gdk.ModifierType.CONTROL_MASK and event.keyval in (
            Gdk.KEY_i,
            Gdk.KEY_I,
        ):
            self._run_update_without_pull()
            return True
        return False

    def _run_update_without_pull(self) -> None:
        # Run files-only installer in embedded console without pulling
        repo_path = self._status.repo_path if self._status else REPO_PATH
        setup_path = os.path.join(repo_path, "setup")
        self.console.ensure_open()
        self.console.append("\n=== INSTALLER START (FILES-ONLY SHORTCUT) ===\n")

        def work():
            try:
                self._compute_upstream_changed_ii(repo_path)
            except Exception as ex:
                self.console.append(f"[keep-tweaks precompute error] {ex}\n")
            if bool(SETTINGS.get("keep_tweaks_beta", False)):
                try:
                    self._backup_tweaks_before_install()
                except Exception as ex:
                    self.console.append(f"[keep-tweaks backup error] {ex}\n")
            if not (os.path.isfile(setup_path) and os.access(setup_path, os.X_OK)):
                GLib.idle_add(
                    lambda: (
                        self._show_message(
                            Gtk.MessageType.INFO, "No executable './setup' found."
                        ),
                        False,
                    )
                )
                return
            try:
                p = _spawn_setup_install(
                    repo_path,
                    lambda m: self.console.append(str(m)),
                    extra_args=["install-files"],
                    capture_stdout=True,
                    auto_input_seq=[],
                    use_pty=bool(SETTINGS.get("use_pty", True)),
                )
                self.console.set_process(p)
                out = getattr(p, "stdout", None) if p else None
                if p and out is not None:
                    for line in iter(out.readline, ""):
                        if not line:
                            break
                        self.console.append(str(line))
                    rc = p.wait()
                    self.console.append(f"[installer exit {rc}]\n")
                    GLib.idle_add(
                        lambda: (
                            self._restore_tweaks_after_install(rc == 0),
                            self.refresh_status(),
                            False,
                        )
                    )
                self.console.set_process(None)
            except Exception as ex:
                self.console.append(f"[installer error] {ex}\n")
                GLib.idle_add(
                    lambda: (
                        self._restore_tweaks_after_install(False),
                        self.refresh_status(),
                        False,
                    )
                )
            finally:
                GLib.idle_add(self.refresh_status)

        threading.Thread(target=work, daemon=True).start()

    # Refresh logic
    def _auto_refresh(self) -> bool:
        self.refresh_status()
        return True

    def refresh_status(self) -> None:
        def refresh_work():
            st = check_repo_status(REPO_PATH)
            GLib.idle_add(self._finish_refresh, st)

        if self._status is None:
            self._busy(True, "Checking for updates...")
        else:
            self._busy(True, "Refreshing...")
        threading.Thread(target=refresh_work, daemon=True).start()

    def _finish_refresh(self, st: RepoStatus) -> None:
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

    # Update workflow
    def on_update_clicked(self, _btn: Gtk.Button | None) -> None:
        if not self._status:
            self.refresh_status()
            return
        repo_path = self._status.repo_path

        mode = str(SETTINGS.get("installer_mode", "auto"))
        if mode == "auto":
            full = self._auto_mode_decide_full(repo_path)
            self._auto_mode_choice = "full" if full else "files-only"

        if not self._check_and_handle_unmerged_conflicts(repo_path):
            self.console.append(
                "[update] Aborted due to unresolved merge/rebase or user cancel.\n"
            )
            return

        self.console.ensure_open()
        self._busy(True, "Updating...")

        def update_work():
            # Beta keep tweaks: backup config before installer
            if bool(SETTINGS.get("keep_tweaks_beta", False)):
                try:
                    self._backup_tweaks_before_install()
                except Exception as ex:
                    self.console.append(f"[keep-tweaks backup error] {ex}\n")
            # Precompute upstream-changed ii files (HEAD..upstream) before pull
            try:
                self._compute_upstream_changed_ii(repo_path)
            except Exception as ex:
                self.console.append(f"[keep-tweaks precompute error] {ex}\n")
            stashed = False
            if self._status and self._status.dirty > 0:
                self.console.append("Stashing local changes...\n")
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

            pull = subprocess.run(
                ["git", "pull", "--rebase", "--autostash", "--stat"],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            success = pull.returncode == 0

            if success and stashed:
                self.console.append("Restoring stash...\n")
                subprocess.run(
                    ["git", "stash", "pop"],
                    cwd=repo_path,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )

            # Decide install plan
            plan_cmds = self._plan_install_commands()
            mode_local = str(SETTINGS.get("installer_mode", "auto"))
            full = False
            if mode_local == "auto":
                full = getattr(self, "_auto_mode_choice", "files-only") == "full"
                self._auto_mode_choice = None
            elif mode_local == "full":
                full = True
            if mode_local in ("auto", "full"):
                if full:
                    # Prefer external terminal (kitty) for full install
                    if shutil.which("kitty") is not None:

                        def _prep_tray():
                            self._ensure_tray_icon()
                            try:
                                if getattr(self, "_tray_icon", None) and hasattr(
                                    self._tray_icon, "set_visible"
                                ):
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
                                rc = subprocess.Popen(
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
                                rc = 1
                                GLib.idle_add(
                                    lambda: (
                                        self._show_message(Gtk.MessageType.ERROR, msg),
                                        False,
                                    )
                                )
                            finally:
                                GLib.idle_add(
                                    lambda: (
                                        self._restore_tweaks_after_install(rc == 0),
                                        (
                                            self._restore_from_tray()
                                            if os.environ.get(
                                                "XDG_SESSION_TYPE", ""
                                            ).lower()
                                            == "x11"
                                            else None
                                        ),
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

            # Run installer (embedded)
            setup_path = os.path.join(repo_path, "setup")
            if os.path.isfile(setup_path) and os.access(setup_path, os.X_OK):
                self.console.append("Running installer...\n")
                if not plan_cmds:
                    plan_cmds = [["./setup", "install-files"]]
                extra_args = plan_cmds[0][1:]
                try:
                    p = _spawn_setup_install(
                        repo_path,
                        lambda m: self.console.append(str(m)),
                        extra_args=extra_args,
                        capture_stdout=True,
                        auto_input_seq=[],
                        use_pty=bool(SETTINGS.get("use_pty", True)),
                    )
                    self.console.set_process(p)
                    out = getattr(p, "stdout", None) if p else None
                    if p and out is not None:
                        for line in iter(out.readline, ""):
                            if not line:
                                break
                            self.console.append(str(line))
                        rc = p.wait()
                        self.console.append(f"[installer exit {rc}]\n")
                        self.console.set_process(None)
                        if rc != 0 and "install-files" in extra_args:
                            # Fallback to full install if minimal fails
                            self.console.append(
                                "[fallback] Retrying with 'install'...\n"
                            )
                            p2 = _spawn_setup_install(
                                repo_path,
                                lambda m: self.console.append(str(m)),
                                extra_args=["install"],
                                capture_stdout=True,
                                auto_input_seq=[],
                                use_pty=bool(SETTINGS.get("use_pty", True)),
                            )
                            self.console.set_process(p2)
                            out2 = getattr(p2, "stdout", None) if p2 else None
                            if p2 and out2 is not None:
                                for line in iter(out2.readline, ""):
                                    if not line:
                                        break
                                    self.console.append(str(line))
                                rc2 = p2.wait()
                                self.console.append(f"[installer exit {rc2}]\n")
                            self.console.set_process(None)
                    else:
                        self.console.append(
                            "[warn] Installer spawn returned no stdout.\n"
                        )
                except Exception as ex:
                    self.console.append(f"[installer error] {ex}\n")
            else:
                self.console.append(
                    "No executable './setup' found. Skipping installer.\n"
                )

            GLib.idle_add(
                lambda: self._finish_update(success, pull.stdout, pull.stderr)
            )

        threading.Thread(target=update_work, daemon=True).start()

    # Keep tweaks beta helpers
    def _backup_tweaks_before_install(self) -> None:
        """
        Create a zip backup of ~/.config/quickshell/ii for later merge restore.
        """
        if getattr(self, "_tweaks_backup_zip", None):
            return  # already backed up this update
        target = os.path.expanduser("~/.config/quickshell/ii")
        if not os.path.isdir(target):
            return
        import tempfile
        import time
        import zipfile

        ts = int(time.time())
        tmpdir = tempfile.gettempdir()
        zip_path = os.path.join(tmpdir, f"illogical-updots-tweaks-{ts}.zip")
        try:
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for root, dirs, files in os.walk(target):
                    for fn in files:
                        full = os.path.join(root, fn)
                        rel = os.path.relpath(full, target)
                        zf.write(full, rel)
            self._tweaks_backup_zip = zip_path
            self.console.append(f"[keep-tweaks] Backed up tweaks to {zip_path}\n")
        except Exception as ex:
            self.console.append(f"[keep-tweaks] Backup failed: {ex}\n")

    def _restore_tweaks_after_install(self, success: bool) -> None:
        """
        Per-file interactive restore of tweak files (only prompt when content differs).

        Updated logic:
        - Skip entirely if keep_tweaks_beta disabled or no backup.
        - Extract backup.
        - Enumerate backup files.
        - Upstream-changed or upstream-deleted files: keep new (no prompt).
        - If backup vs new file identical: auto keep new (no prompt).
        - Otherwise show a clean boxed dialog with a COLORED diff.
            * Restore = replace updated file with backup (your tweaks) after saving current as .tweaks.bak
            * Keep New = leave updated file untouched
        - Summary printed to console.
        """
        if not bool(SETTINGS.get("keep_tweaks_beta", False)):
            return
        zip_path = getattr(self, "_tweaks_backup_zip", None)
        if not zip_path or not os.path.isfile(zip_path):
            return
        target = os.path.expanduser("~/.config/quickshell/ii")
        if not os.path.isdir(target):
            return

        import difflib
        import tempfile
        import zipfile

        work_dir = tempfile.mkdtemp(prefix="illogical-updots-tweaks-")
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(work_dir)
        except Exception as ex:
            self.console.append(f"[keep-tweaks] Failed to extract backup: {ex}\n")
            return

        # Upstream change tracking removed (simplified logic):
        # We only prompt when file content differs or file was deleted.
        # Identical content -> auto keep new. Missing current file -> prompt.

        # Gather backup files
        all_backup_files = []
        for root, _dirs, files in os.walk(work_dir):
            for fn in files:
                backup_full = os.path.join(root, fn)
                rel = os.path.relpath(backup_full, work_dir)
                all_backup_files.append((rel, backup_full))

        if not all_backup_files:
            self.console.append("[keep-tweaks] Backup contained no files.\n")
            return

        restored_count = 0
        kept_count = 0
        skipped_count = 0

        # If huge number of files, ask once for bulk action
        if len(all_backup_files) > 200:
            dlg_bulk = Gtk.MessageDialog(
                transient_for=self,
                flags=0,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.NONE,
                text="Large backup",
            )
            dlg_bulk.format_secondary_text(
                f"{len(all_backup_files)} files in backup.\n"
                "Restore all unchanged files automatically?\n"
                "Yes = restore all non-upstream-changed.\nNo = prompt file-by-file."
            )
            dlg_bulk.add_button("No (prompt)", Gtk.ResponseType.NO)
            dlg_bulk.add_button("Yes (auto restore unchanged)", Gtk.ResponseType.YES)
            r_bulk = dlg_bulk.run()
            dlg_bulk.destroy()
            auto_restore = r_bulk == Gtk.ResponseType.YES
        else:
            auto_restore = False

        for rel, backup_full in all_backup_files:
            target_full = os.path.join(target, rel)
            os.makedirs(os.path.dirname(target_full), exist_ok=True)

            # Determine initial action:
            # If current file missing -> treat as difference (prompt user).
            # If present -> will compare contents to decide (prompt only if different).
            if not os.path.exists(target_full):
                action = None  # missing: prompt
            else:
                action = None  # will compare later

            if auto_restore and action is None:
                action = Gtk.ResponseType.YES  # bulk restore for unchanged

            # Interactive prompt if not decided yet
            if action is None:
                # Prepare a short diff preview (first 20 lines of unified diff)
                diff_preview = ""
                diff_lines = []
                try:
                    with open(
                        backup_full, "r", encoding="utf-8", errors="ignore"
                    ) as f_old:
                        old_lines = f_old.readlines()
                    if os.path.exists(target_full):
                        with open(
                            target_full, "r", encoding="utf-8", errors="ignore"
                        ) as f_new:
                            new_lines = f_new.readlines()
                    else:
                        new_lines = []
                    # If identical (and file existed), skip prompt (auto keep new)
                    if new_lines and old_lines == new_lines:
                        kept_count += 1
                        continue
                    # If file missing or differs -> proceed to diff / prompt
                    diff_lines = list(
                        difflib.unified_diff(
                            new_lines,
                            old_lines,
                            fromfile="updated",
                            tofile="backup",
                            lineterm="",
                        )
                    )
                    # Truncate for preview rendering; full diff kept in memory
                    if diff_lines:
                        diff_preview = diff_lines[:200]
                except Exception:
                    diff_lines = []
                    diff_preview = []

                # Custom boxed dialog (cleaner UI) with colored diff
                dlg = Gtk.Dialog(
                    title="Restore previous version", transient_for=self, flags=0
                )
                dlg.add_button("Keep New", Gtk.ResponseType.NO)
                dlg.add_button("Restore", Gtk.ResponseType.YES)
                content = dlg.get_content_area()
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
                box.set_border_width(16)
                content.add(box)

                # Filename header
                lbl_title = Gtk.Label()
                lbl_title.set_xalign(0.0)
                lbl_title.set_use_markup(True)
                lbl_title.set_markup(f"<b>{GLib.markup_escape_text(rel)}</b>")
                box.pack_start(lbl_title, False, False, 0)

                # Explanation
                lbl_info = Gtk.Label()
                lbl_info.set_xalign(0.0)
                lbl_info.set_use_markup(True)
                lbl_info.set_line_wrap(True)
                lbl_info.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
                lbl_info.set_markup(
                    "<small>Restore = use backup (your tweaks)\nKeep New = keep updated file</small>"
                )
                box.pack_start(lbl_info, False, False, 0)

                # Colored diff (only when content actually differs)
                if diff_lines:
                    frame = Gtk.Frame()
                    frame.set_shadow_type(Gtk.ShadowType.IN)
                    box.pack_start(frame, True, True, 0)
                    sw_diff = Gtk.ScrolledWindow()
                    sw_diff.set_policy(
                        Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC
                    )
                    sw_diff.set_min_content_height(260)
                    frame.add(sw_diff)
                    tv = Gtk.TextView()
                    tv.set_editable(False)
                    tv.set_cursor_visible(False)
                    tv.modify_font(Pango.FontDescription("Monospace 10"))
                    buf = tv.get_buffer()
                    # Tag definitions
                    tag_add = buf.create_tag(None, foreground="#A3BE8C")
                    tag_del = buf.create_tag(None, foreground="#BF616A")
                    tag_hunk = buf.create_tag(None, foreground="#EBCB8B")
                    tag_meta = buf.create_tag(None, foreground="#5E81AC")
                    tag_norm = buf.create_tag(None, foreground="#D8DEE9")
                    for line in diff_preview:
                        line_text = line + ("\n" if not line.endswith("\n") else "")
                        start_iter = buf.get_end_iter()
                        buf.insert(start_iter, line_text)
                        end_iter = buf.get_end_iter()
                        if line.startswith("@@"):
                            buf.apply_tag(tag_hunk, start_iter, end_iter)
                        elif line.startswith("+") and not line.startswith("+++"):
                            buf.apply_tag(tag_add, start_iter, end_iter)
                        elif line.startswith("-") and not line.startswith("---"):
                            buf.apply_tag(tag_del, start_iter, end_iter)
                        elif (
                            line.startswith("diff ")
                            or line.startswith("---")
                            or line.startswith("+++")
                        ):
                            buf.apply_tag(tag_meta, start_iter, end_iter)
                        else:
                            buf.apply_tag(tag_norm, start_iter, end_iter)
                    sw_diff.add(tv)

                dlg.show_all()
                resp = dlg.run()
                dlg.destroy()
                action = resp

            if action == Gtk.ResponseType.YES:
                try:
                    # Save current new file for rollback if it exists
                    if os.path.exists(target_full):
                        try:
                            shutil.copy2(target_full, target_full + ".tweaks.bak")
                        except Exception:
                            pass
                    shutil.copy2(backup_full, target_full)
                    restored_count += 1
                except Exception as ex:
                    self.console.append(f"[keep-tweaks restore error] {rel}: {ex}\n")
                    skipped_count += 1
            else:
                kept_count += 1

        self.console.append(
            f"[keep-tweaks] Interactive restore finished. Restored {restored_count}, kept new {kept_count}, skipped {skipped_count}.\n"
        )

    def _finish_update(self, success: bool, stdout: str, stderr: str) -> None:
        self._busy(False, "")
        title = "Update complete" if success else "Update failed"
        details = stdout + ("\n" + stderr if stderr else "")
        self._add_log(title, title, details)
        # Hide console after update to keep UI tidy
        try:
            self.console.revealer.set_reveal_child(False)
        except Exception:
            pass
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
        # Offer tweaks restore & merge (beta)
        try:
            self._restore_tweaks_after_install(success)
        except Exception as ex:
            self.console.append(f"[keep-tweaks error] {ex}\n")
        if success:
            self._run_post_script_if_configured()

    # Installer helpers

    def _compute_upstream_changed_ii(self, repo_path: str) -> None:
        """
        Compute files under dots/.config/quickshell/ii that changed upstream
        (HEAD..upstream) and cache their relative paths for post-install decisions.
        """
        try:
            self._upstream_changed_ii = set()
            st = getattr(self, "_status", None)
            branch = st.branch if st and st.branch else get_branch(repo_path)
            upstream = (
                st.upstream if st and st.upstream else get_upstream(repo_path, branch)
            )
            if not upstream:
                run_git(["fetch", "--all", "--prune"], repo_path)
                upstream = get_upstream(repo_path, branch)
            if not upstream:
                return
            rc, out, _ = run_git(
                [
                    "diff",
                    "--name-only",
                    f"HEAD..{upstream}",
                    "--",
                    "dots/.config/quickshell/ii",
                ],
                repo_path,
            )
            changed = []
            if rc == 0:
                for ln in out.splitlines() if out else []:
                    ln = ln.strip()
                    if not ln:
                        continue
                    prefix = "dots/.config/quickshell/ii/"
                    if ln.startswith(prefix):
                        rel = ln[len(prefix) :]
                        if rel:
                            changed.append(rel)
            self._upstream_changed_ii = set(changed)
            if changed:
                self.console.append(
                    f"[keep-tweaks] Upstream-changed ii files: {len(changed)}\n"
                )
        except Exception:
            # Best-effort; if it fails we just fall back to merge logic
            pass

    def _plan_install_commands(self) -> list[list[str]]:
        mode = str(SETTINGS.get("installer_mode", "auto"))
        if mode == "full":
            self.console.append("Installer mode: full install.\n")
            return [["./setup", "install"]]
        if mode == "auto":
            self.console.append("Installer mode: auto (pending decision).\n")
            return [["./setup", "install-files"]]
        self.console.append("Installer mode: files-only.\n")
        return [["./setup", "install-files"]]

    def _ensure_tray_icon(self) -> None:
        if getattr(self, "_tray_icon", None):
            return
        try:
            # Only create a tray icon under X11; Gtk.StatusIcon is not supported on Wayland
            if os.environ.get("XDG_SESSION_TYPE", "").lower() != "x11":
                return
            icon = Gtk.StatusIcon.new_from_icon_name("illogical-updots")
            icon.set_tooltip_text(APP_TITLE)
            icon.connect("activate", lambda _i: self._restore_from_tray())
            self._tray_icon = icon
        except Exception:
            pass

    def _restore_from_tray(self) -> None:
        try:
            if os.environ.get("XDG_SESSION_TYPE", "").lower() == "x11":
                self.show_all()
                if getattr(self, "_tray_icon", None) and hasattr(
                    self._tray_icon, "set_visible"
                ):
                    self._tray_icon.set_visible(False)
        except Exception:
            pass

    def _auto_mode_decide_full(self, repo_path: str) -> bool:
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

    def _check_and_handle_unmerged_conflicts(self, repo_path: str) -> bool:
        rc_u, out_u, _ = run_git(["diff", "--name-only", "--diff-filter=U"], repo_path)
        unmerged_files = [
            ln for ln in (out_u.splitlines() if rc_u == 0 else []) if ln.strip()
        ]

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

        ok = True
        if merge_in_progress:
            self.console.append("[git] merge --abort\n")
            r = subprocess.run(
                ["git", "merge", "--abort"],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                ok = False
                self.console.append(f"[git error] merge --abort: {r.stderr}\n")
        if rebase_in_progress:
            self.console.append("[git] rebase --abort\n")
            r = subprocess.run(
                ["git", "rebase", "--abort"],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                ok = False
                self.console.append(f"[git error] rebase --abort: {r.stderr}\n")
        if cherry_in_progress:
            self.console.append("[git] cherry-pick --abort\n")
            r = subprocess.run(
                ["git", "cherry-pick", "--abort"],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                ok = False
                self.console.append(f"[git error] cherry-pick --abort: {r.stderr}\n")

        rc_u2, out_u2, _ = run_git(
            ["diff", "--name-only", "--diff-filter=U"], repo_path
        )
        still_unmerged = [
            ln for ln in (out_u2.splitlines() if rc_u2 == 0 else []) if ln.strip()
        ]
        if still_unmerged:
            ok = False
            self.console.append(
                "[git] Unmerged files still present after abort; canceling update.\n"
            )
            self._show_message(
                Gtk.MessageType.ERROR,
                "Unmerged files remain after abort. Resolve conflicts manually before updating.",
            )
        return ok

    def _run_post_script_if_configured(self) -> None:
        path = str(SETTINGS.get("post_script_path") or "").strip()
        if not path:
            return
        self.console.ensure_open()
        self.console.append("\n=== POST-INSTALL SCRIPT ===\n")

        def work():
            try:
                if not os.path.exists(path):
                    self.console.append(
                        f"[post-script error] path does not exist: {path}\n"
                    )
                    return
                if os.path.isdir(path):
                    self.console.append(
                        f"[post-script error] path is a directory: {path}\n"
                    )
                    return
                if os.access(path, os.X_OK):
                    cmd_str = f"exec {shlex.quote(path)}"
                else:
                    cmd_str = f"exec fish {shlex.quote(path)}"
                    self.console.append(
                        "[post-script] script not executable; running via fish interpreter\n"
                    )
                self.console.append(f"$ fish -lc {shlex.quote(cmd_str)}\n")
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
                    self.console.append(str(line))
                rc = p.wait()
                self.console.append(f"[post-script exit {rc}]\n")
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
                self.console.append(f"[post-script error] {ex}\n")

        threading.Thread(target=work, daemon=True).start()

    # External installer run (explicit)
    def run_install_external(self) -> None:
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


# Re-export commonly used symbols for convenience when importing this module
__all__ = [
    "MainWindow",
    "APP_ID",
    "APP_TITLE",
    "SETTINGS",
    "REPO_PATH",
    "_save_settings",
]
