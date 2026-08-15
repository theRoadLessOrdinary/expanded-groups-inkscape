"""
The actual Expanded Groups browse/Apply/Rename/Remove window. Launched
detached by expanded_groups.py (not a child process Inkscape waits on), so
it can stay open indefinitely without blocking the main Inkscape window.

This never touches the SVG file. It loads its starting model from the JSON
snapshot expanded_groups.py wrote, and every mutation (Rename/Remove) goes
out live through Inkscape's D-Bus Actions interface (select-by-id +
object-set-attribute) so edits land in Inkscape's own in-memory document
and stay correctly tracked by undo/unsaved-changes -- this script never
writes to disk itself. Its own model is then updated optimistically to
match, since there's no D-Bus query to read live attribute values back.

Because of that, this window's list can drift out of sync with reality if
something outside this tool changes the same objects' trlo:group attribute
while it's open. There's no live "refresh from Inkscape" available (no
D-Bus query mechanism for it) -- closing and reopening the tool re-scans
the document fresh.
"""

import json
import sys
import warnings
warnings.filterwarnings('ignore')  # before the gi import in expanded_groups_dbus

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk

from expanded_groups_data import GROUP_ATTR, merge_names
from expanded_groups_dbus import select_by_id_live, set_attribute_live, quiet_stderr

VERSION = "2.3.0"


class ExpandedGroupsPanel:

    def __init__(self, by_name, by_id):
        self.by_name = by_name  # {group_name: [id, ...]}
        self.by_id = by_id      # {id: [name, ...]}
        self.build_dialog()

    def build_dialog(self):
        # Redundant when launched detached (its stderr is already devnull'd
        # before exec), but kept consistent/safe if this script is ever run
        # directly for debugging. See quiet_stderr()'s docstring.
        with quiet_stderr():
            self._build_dialog_inner()

    def _build_dialog_inner(self):
        win = Gtk.Window(title=f"Expanded Groups v{VERSION}")
        win.set_default_size(340, 420)
        win.set_border_width(10)
        win.connect("destroy", Gtk.main_quit)
        self.window = win

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        win.add(vbox)

        note = Gtk.Label(
            label="To assign objects to a group, use\nExtensions → Expanded Groups → "
                  "Assign To Group.",
            xalign=0,
        )
        note.set_line_wrap(True)
        vbox.pack_start(note, False, False, 0)

        vbox.pack_start(Gtk.Separator(), False, False, 4)
        vbox.pack_start(Gtk.Label(label="Existing groups:", xalign=0), False, False, 0)

        self.liststore = Gtk.ListStore(str, int)
        self.treeview = Gtk.TreeView(model=self.liststore)
        self.treeview.append_column(Gtk.TreeViewColumn("Name", Gtk.CellRendererText(), text=0))
        self.treeview.append_column(Gtk.TreeViewColumn("Count", Gtk.CellRendererText(), text=1))

        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(220)
        scroll.add(self.treeview)
        vbox.pack_start(scroll, True, True, 0)

        self.refresh_list()

        rename_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        vbox.pack_start(rename_row, False, False, 0)
        rename_row.pack_start(Gtk.Label(label="New name:"), False, False, 0)
        self.rename_entry = Gtk.Entry()
        rename_row.pack_start(self.rename_entry, True, True, 0)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, homogeneous=True)
        vbox.pack_start(btn_box, False, False, 4)

        apply_btn = Gtk.Button(label="Apply (Select)")
        apply_btn.connect("clicked", self.on_apply)
        btn_box.pack_start(apply_btn, True, True, 0)

        rename_btn = Gtk.Button(label="Rename")
        rename_btn.connect("clicked", self.on_rename)
        btn_box.pack_start(rename_btn, True, True, 0)

        remove_btn = Gtk.Button(label="Remove")
        remove_btn.connect("clicked", self.on_remove)
        btn_box.pack_start(remove_btn, True, True, 0)

        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", lambda b: win.destroy())
        vbox.pack_start(close_btn, False, False, 4)

        win.show_all()

    # -- list helpers --------------------------------------------------

    def refresh_list(self):
        self.liststore.clear()
        for name in sorted(self.by_name.keys()):
            self.liststore.append([name, len(self.by_name[name])])

    def selected_group_name(self):
        model, treeiter = self.treeview.get_selection().get_selected()
        if treeiter is None:
            return None
        return model[treeiter][0]

    def rebuild_by_name(self):
        by_name = {}
        for node_id, names in self.by_id.items():
            for name in names:
                by_name.setdefault(name, []).append(node_id)
        self.by_name = by_name

    def error(self, message):
        dialog = Gtk.MessageDialog(
            transient_for=self.window, modal=True,
            message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK,
            text=message,
        )
        dialog.run()
        dialog.destroy()

    # -- actions ---------------------------------------------------------

    def on_apply(self, button):
        name = self.selected_group_name()
        if not name:
            self.error("Select a group from the list first.")
            return
        select_by_id_live(self.by_name.get(name, []))

    def on_rename(self, button):
        old_name = self.selected_group_name()
        if not old_name:
            self.error("Select a group from the list first.")
            return
        new_name = self.rename_entry.get_text().strip()
        if not new_name:
            self.error("Type the new name into the field first.")
            return
        if new_name == old_name:
            return

        for node_id in list(self.by_name.get(old_name, [])):
            new_names = merge_names(self.by_id[node_id], rename_from=old_name, rename_to=new_name)
            self.by_id[node_id] = new_names
            select_by_id_live([node_id])
            set_attribute_live(GROUP_ATTR, '|'.join(new_names))

        self.rebuild_by_name()
        self.refresh_list()
        self.rename_entry.set_text('')

    def on_remove(self, button):
        name = self.selected_group_name()
        if not name:
            self.error("Select a group from the list first.")
            return

        for node_id in list(self.by_name.get(name, [])):
            new_names = merge_names(self.by_id[node_id], remove=name)
            self.by_id[node_id] = new_names
            select_by_id_live([node_id])
            set_attribute_live(GROUP_ATTR, '|'.join(new_names))

        self.rebuild_by_name()
        self.refresh_list()


def main():
    if len(sys.argv) < 2:
        print("usage: expanded_groups_panel.py <snapshot.json>", file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1]) as f:
        snapshot = json.load(f)

    ExpandedGroupsPanel(snapshot.get('by_name', {}), snapshot.get('by_id', {}))
    Gtk.main()


if __name__ == '__main__':
    main()
