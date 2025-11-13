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

import logging
import os
import sys
import threading
import urllib.request
from typing import Optional

LOG = logging.getLogger("illogical-updots")
if not LOG.handlers:
    level = logging.DEBUG if os.environ.get("UPDOTS_DEBUG") else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk

from main_window import (
    APP_ID,
    APP_TITLE,
    REPO_PATH,
    SETTINGS,
    MainWindow,
    _save_settings,
)


class App(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)
        # Ensure the process and app names map to the desktop file for icon association
        GLib.set_prgname("illogical-updots")
        GLib.set_application_name("Illogical Updots")
        # Use theme icon name first
        try:
            LOG.debug("Setting default icon name 'illogical-updots'")
            Gtk.Window.set_default_icon_name("illogical-updots")
            LOG.debug("Icon name set via theme lookup")
        except Exception as e:
            LOG.debug(f"Failed to set icon name via theme lookup: {e}")

        def _try_set_icon_file(p: str) -> bool:
            if p and os.path.isfile(p):
                LOG.debug(f"Trying icon file: {p}")
                try:
                    Gtk.Window.set_default_icon_from_file(p)
                    LOG.debug(f"Set default icon from file: {p}")
                    return True
                except Exception as e:
                    LOG.debug(f"Failed to set icon from file: {p}: {e}")
                    return False
            else:
                LOG.debug(f"Icon file not found: {p}")
            return False

        # Known system and local paths
        base_dir = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            "/usr/share/icons/hicolor/256x256/apps/illogical-updots.png",
            "/usr/share/icons/hicolor/scalable/apps/illogical-updots.svg",
            "/usr/share/pixmaps/illogical-updots.png",
            os.path.join(base_dir, ".github", "assets", "logo.png"),
            os.path.join(base_dir, "assets", "logo.png"),
        ]
        LOG.debug(f"Icon candidates: {candidates}")
        if not any(_try_set_icon_file(p) for p in candidates):
            # Fallback: cached download to avoid repeated network hits
            cache_dir = os.path.join(os.path.expanduser("~/.cache"), "illogical-updots")
            cache_path = os.path.join(cache_dir, "icon.png")
            LOG.debug(f"Checking cached icon at {cache_path}")
            if not _try_set_icon_file(cache_path):

                def _download_icon():
                    try:
                        os.makedirs(cache_dir, exist_ok=True)
                        url = "https://github.com/FoxyIsCoding/illogical-updots/blob/main/.github/assets/logo.png?raw=true"
                        LOG.debug(f"Downloading icon from {url} to {cache_path}")
                        urllib.request.urlretrieve(url, cache_path)
                        LOG.debug("Icon downloaded; scheduling set from cache")
                        GLib.idle_add(lambda: _try_set_icon_file(cache_path))
                    except Exception as e:
                        LOG.debug(f"Icon download failed: {e}")

                try:
                    LOG.debug("Starting background thread to download icon")
                    threading.Thread(target=_download_icon, daemon=True).start()
                except Exception as e:
                    LOG.debug(f"Failed to start icon download thread: {e}")
        _icon_file = ".github/assets/logo.png"
        if os.path.isfile(_icon_file):
            try:
                LOG.debug(f"Using local repo icon: {_icon_file}")
                Gtk.Window.set_default_icon_from_file(_icon_file)
            except Exception as e:
                LOG.debug(f"Failed to set local repo icon: {e}")

    def do_activate(self) -> None:  # type: ignore[override]
        global REPO_PATH
        # First-run selection if no repo path configured and no fallback found
        if not REPO_PATH or not os.path.isdir(REPO_PATH):
            # Explain the situation and ask user to continue to select a repository
            alert = Gtk.MessageDialog(
                transient_for=None,
                flags=0,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.NONE,
                text="Repository not found",
            )
            alert.format_secondary_text(
                "No repository path is configured, and no default could be detected.\n"
                "Please select your repository folder to continue."
            )
            alert.add_button("Cancel", Gtk.ResponseType.CANCEL)
            alert.add_button("Continue", Gtk.ResponseType.OK)
            resp_alert = alert.run()
            alert.destroy()
            if resp_alert != Gtk.ResponseType.OK:
                return
            # Open file chooser after user confirms
            chooser = Gtk.FileChooserDialog(
                title="Select repository directory",
                transient_for=None,
                action=Gtk.FileChooserAction.SELECT_FOLDER,
            )
            chooser.add_buttons(
                "Cancel", Gtk.ResponseType.CANCEL, "Select", Gtk.ResponseType.OK
            )
            try:
                start_dir = os.path.expanduser("~")
                if os.path.isdir(start_dir):
                    chooser.set_current_folder(start_dir)
            except Exception:
                pass
            resp = chooser.run()
            if resp == Gtk.ResponseType.OK:
                chosen = chooser.get_filename()
                if chosen and os.path.isdir(chosen):
                    SETTINGS["repo_path"] = chosen
                    _save_settings(SETTINGS)
                    REPO_PATH = chosen
            chooser.destroy()
            # If user canceled and we still don't have a valid path, do not open main window
            if not REPO_PATH or not os.path.isdir(REPO_PATH):
                return

        if not self.props.active_window:
            MainWindow(self)
        win = self.props.active_window
        if win:
            # Help DEs map the window to the desktop file/icon
            try:
                win.set_icon_name("illogical-updots")
            except Exception:
                pass
            try:
                # X11-specific; ignored elsewhere
                win.set_wmclass("illogical-updots", "Illogical Updots")
            except Exception:
                pass
            win.present()

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
