import os

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk


def show_settings_dialog(
    window, SETTINGS, REPO_PATH, AUTO_REFRESH_SECONDS, _save_settings
) -> None:
    """
    Clean single-page settings dialog with visual section headers using a ListBox.
    Categories:
      GENERAL
      CONSOLE
      VIEW
      POST ACTIONS
    """

    dialog = Gtk.Dialog(
        title="Settings",
        transient_for=window,
        flags=0,
    )
    dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
    dialog.add_button("Save", Gtk.ResponseType.OK)

    content = dialog.get_content_area()
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    outer.set_border_width(18)
    content.add(outer)

    listbox = Gtk.ListBox()
    listbox.set_selection_mode(Gtk.SelectionMode.NONE)
    outer.pack_start(listbox, True, True, 0)

    def separator() -> None:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_border_width(10)
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        box.pack_start(sep, False, False, 0)
        row.add(box)
        listbox.add(row)

    def setting(label: str, widget: Gtk.Widget, tooltip: str = "") -> None:
        row = Gtk.ListBoxRow()
        h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        h.set_border_width(6)
        lbl = Gtk.Label(label=label)
        lbl.set_xalign(0.0)
        lbl.set_width_chars(26)
        if tooltip:
            lbl.set_tooltip_text(tooltip)
            widget.set_tooltip_text(tooltip)
        h.pack_start(lbl, False, False, 0)
        h.pack_start(widget, True, True, 0)
        row.add(h)
        listbox.add(row)

    # GENERAL

    entry_repo = Gtk.Entry()
    entry_repo.set_text(SETTINGS.get("repo_path", REPO_PATH) or "")
    btn_repo = Gtk.Button.new_from_icon_name(
        "folder-open-symbolic", Gtk.IconSize.BUTTON
    )

    repo_container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    repo_container.pack_start(entry_repo, True, True, 0)
    repo_container.pack_start(btn_repo, False, False, 0)

    def browse_repo(_b):
        chooser = Gtk.FileChooserDialog(
            title="Select repository directory",
            transient_for=window,
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
            chosen = chooser.get_filename()
            if chosen:
                entry_repo.set_text(chosen)
        chooser.destroy()

    btn_repo.connect("clicked", browse_repo)
    setting("Repository path", repo_container, "Folder containing your git repo")

    entry_refresh = Gtk.Entry()
    entry_refresh.set_width_chars(6)
    entry_refresh.set_text(
        str(SETTINGS.get("auto_refresh_seconds", AUTO_REFRESH_SECONDS))
    )
    setting(
        "Auto refresh (seconds)",
        entry_refresh,
        "Interval between automatic repository status checks",
    )

    cmb_mode = Gtk.ComboBoxText()
    cmb_mode.append_text("files-only")
    cmb_mode.append_text("full")
    cmb_mode.append_text("auto")
    current_mode = str(SETTINGS.get("installer_mode", "files-only"))
    if current_mode not in ("files-only", "full", "auto"):
        current_mode = "files-only"
    cmb_mode.set_active({"files-only": 0, "full": 1, "auto": 2}[current_mode])
    setting("Installer mode", cmb_mode, "Which setup subcommand to run")

    cb_detached = Gtk.CheckButton.new_with_label("Run installer in separate window")
    cb_detached.set_active(bool(SETTINGS.get("detached_console", False)))
    setting("Detached installer console", cb_detached)

    # CONSOLE (separator)
    separator()
    cb_pty = Gtk.CheckButton.new_with_label("Allocate PTY for embedded process")
    cb_pty.set_active(bool(SETTINGS.get("use_pty", True)))
    setting("Use PTY", cb_pty)

    cb_color = Gtk.CheckButton.new_with_label("Force color environment variables")
    cb_color.set_active(bool(SETTINGS.get("force_color_env", True)))
    setting("Force color env", cb_color, "Sets TERM/CLICOLOR/CLICOLOR_FORCE")

    cb_notify = Gtk.CheckButton.new_with_label("Show desktop notifications")
    cb_notify.set_active(bool(SETTINGS.get("send_notifications", True)))
    setting("Notifications", cb_notify)

    spin_log = Gtk.SpinButton()
    spin_log.set_range(0, 100000)
    spin_log.set_increments(100, 1000)
    spin_log.set_value(float(int(SETTINGS.get("log_max_lines", 5000))))
    setting("Max log lines (0 = unlimited)", spin_log)

    # VIEW (separator)
    separator()
    cb_lazy = Gtk.CheckButton.new_with_label("Animate & lazy-load commit rows")
    cb_lazy.set_active(bool(SETTINGS.get("changes_lazy_load", True)))
    setting("Lazy load commits", cb_lazy)

    cb_details_btn = Gtk.CheckButton.new_with_label("Show banner 'Details…' button")
    cb_details_btn.set_active(bool(SETTINGS.get("show_details_button", True)))
    setting("Banner details button", cb_details_btn)

    # POST ACTIONS (separator)
    separator()
    entry_post = Gtk.Entry()
    entry_post.set_text(str(SETTINGS.get("post_script_path", "") or ""))
    entry_post.set_placeholder_text("/path/to/script.sh")
    btn_post = Gtk.Button.new_from_icon_name(
        "folder-open-symbolic", Gtk.IconSize.BUTTON
    )
    post_container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    post_container.pack_start(entry_post, True, True, 0)
    post_container.pack_start(btn_post, False, False, 0)

    def browse_post(_b):
        chooser = Gtk.FileChooserDialog(
            title="Select script",
            transient_for=window,
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
        resp = chooser.run()
        if resp == Gtk.ResponseType.OK:
            chosen = chooser.get_filename()
            if chosen:
                entry_post.set_text(chosen)
        chooser.destroy()

    btn_post.connect("clicked", browse_post)
    setting("Post-install script", post_container, "Script run after installer")

    dialog.show_all()
    resp = dialog.run()
    if resp == Gtk.ResponseType.OK:
        # Refresh interval
        new_refresh_raw = entry_refresh.get_text().strip()
        try:
            new_refresh = int(new_refresh_raw)
            if new_refresh <= 0:
                raise ValueError
        except ValueError:
            new_refresh = AUTO_REFRESH_SECONDS
        # Repo path
        new_repo = entry_repo.get_text().strip()
        if new_repo and os.path.isdir(new_repo):
            SETTINGS["repo_path"] = new_repo
        else:
            window._show_message(
                Gtk.MessageType.WARNING,
                "Invalid repo path (must exist). Keeping previous.",
            )
        # Persist all settings
        SETTINGS["auto_refresh_seconds"] = new_refresh
        SETTINGS["detached_console"] = cb_detached.get_active()
        SETTINGS["installer_mode"] = {0: "files-only", 1: "full", 2: "auto"}.get(
            cmb_mode.get_active(), "files-only"
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
        _save_settings(SETTINGS)
        REPO_PATH = str(SETTINGS.get("repo_path") or "")
        AUTO_REFRESH_SECONDS = int(
            SETTINGS.get("auto_refresh_seconds", AUTO_REFRESH_SECONDS)
        )
        if hasattr(window, "header_bar"):
            try:
                window.header_bar.props.subtitle = REPO_PATH
            except Exception:
                pass
        window.refresh_status()
    dialog.destroy()
