"""
The actual Expanded Groups browse panel: a native-layers-style tree.
Launched detached by expanded_groups.py (not a child process Inkscape
waits on), so it can stay open indefinitely without blocking the main
Inkscape window.

Top-level rows are group names; expanding one reveals its member object
ids as child rows. Clicking a group row selects every member live on
canvas; clicking an object row selects just that one. Double-click (or
Rename, toolbar/context menu) renames a group inline. Delete removes a
group's membership from all its objects, or - on an object row - just
that one object's membership in its parent group. No drag-and-drop, no
nested groups (groups are flat; only the object rows nest, for display).

This never touches the SVG file. It loads its starting model from the
JSON snapshot expanded_groups.py wrote, and every mutation goes out live
through Inkscape's D-Bus Actions interface (select-by-id +
object-set-attribute) so edits land in Inkscape's own in-memory document
and stay correctly tracked by undo/unsaved-changes - this script never
writes to disk itself. Its own model is then updated optimistically to
match, since there's no D-Bus query to read live attribute values back.

Because of that, this window's list can drift out of sync with reality
if something outside this tool changes the same objects' trlo:group
attribute while it's open. There's no live "refresh from Inkscape"
available (no D-Bus query mechanism for it) - closing and reopening the
tool re-scans the document fresh. The toolbar's Assign
button runs the same Assign To Group extension as the menu item; that
extension leaves a small record this panel polls to update its list.
"""

import json
import os
import sys
import time
import warnings
warnings.filterwarnings('ignore')  # before the gi import in expanded_groups_dbus

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib

from expanded_groups_data import GROUP_ATTR, LAST_ASSIGN_PATH, PENDING_TARGET_PATH, merge_names
from expanded_groups_dbus import select_by_id_live, set_attribute_live, run_extension_live, set_hidden_live, set_locked_live, quiet_stderr

VERSION = "3.3.0"

# TreeStore columns: icon name, display text, kind ("group"/"object"), id
# (group name for a group row, object id for an object row)
# plus the eye/lock icon names shown at the right of each row
COL_ICON, COL_NAME, COL_KIND, COL_ID, COL_EYE, COL_LOCK = range(6)


class ExpandedGroupsPanel:

    def __init__(self, by_name, by_id, states):
        self.by_name = by_name  # {group_name: [id, ...]}
        self.by_id = by_id      # {id: [name, ...]}
        self.states = states    # {id: {'hidden': bool, 'locked': bool}}
        self.last_assign_mtime = self._assign_mtime()
        with quiet_stderr():
            self.build_dialog()

    def build_dialog(self):
        win = Gtk.Window(title=f"Expanded Groups v{VERSION}")
        win.set_default_size(320, 460)
        win.connect("destroy", Gtk.main_quit)
        win.connect("key-press-event", self.on_key_press)
        self.window = win

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        win.add(vbox)

        toolbar = Gtk.Toolbar()
        assign_btn = Gtk.ToolButton()
        assign_btn.set_icon_name("list-add")
        assign_btn.set_label("Assign To Group...")
        assign_btn.set_is_important(True)
        assign_btn.set_tooltip_text("Assign the current canvas selection to a group (same as Extensions > Expanded Groups > Assign To Group)")
        assign_btn.connect("clicked", lambda b: self.assign_selection())
        toolbar.insert(assign_btn, -1)

        rename_btn = Gtk.ToolButton()
        rename_btn.set_icon_name("document-edit")
        rename_btn.set_tooltip_text("Rename selected group")
        rename_btn.connect("clicked", lambda b: self.start_rename_selected())
        toolbar.insert(rename_btn, -1)

        delete_btn = Gtk.ToolButton()
        delete_btn.set_icon_name("edit-delete")
        delete_btn.set_tooltip_text("Delete selected group, or remove selected object from its group")
        delete_btn.connect("clicked", lambda b: self.delete_selected())
        toolbar.insert(delete_btn, -1)
        vbox.pack_start(toolbar, False, False, 0)

        self.store = Gtk.TreeStore(str, str, str, str, str, str)
        self.tree = Gtk.TreeView(model=self.store)
        self.tree.set_headers_visible(False)

        col = Gtk.TreeViewColumn("Name")
        icon_renderer = Gtk.CellRendererPixbuf()
        col.pack_start(icon_renderer, False)
        col.add_attribute(icon_renderer, "icon-name", COL_ICON)
        self.text_renderer = Gtk.CellRendererText()
        self.text_renderer.connect("edited", self.on_name_edited)
        col.pack_start(self.text_renderer, True)
        col.add_attribute(self.text_renderer, "text", COL_NAME)
        col.set_expand(True)
        self.tree.append_column(col)

        # Eye and lock toggles, one icon column each (like Inkscape's own
        # Layers panel). Clicks are handled in on_button_press.
        self.eye_col = self._icon_column(COL_EYE)
        self.lock_col = self._icon_column(COL_LOCK)

        self.tree.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
        self.tree.connect("button-press-event", self.on_button_press)

        scroll = Gtk.ScrolledWindow()
        scroll.add(self.tree)
        vbox.pack_start(scroll, True, True, 0)

        self.statusbar = Gtk.Statusbar()
        self.status_ctx = self.statusbar.get_context_id("main")
        vbox.pack_start(self.statusbar, False, False, 0)

        self.refresh_tree()
        win.show_all()
        GLib.timeout_add(500, self.poll_assign)

    def _icon_column(self, model_col):
        renderer = Gtk.CellRendererPixbuf()
        column = Gtk.TreeViewColumn("", renderer)
        column.add_attribute(renderer, "icon-name", model_col)
        self.tree.append_column(column)
        return column

    def show_status(self, msg):
        self.statusbar.pop(self.status_ctx)
        self.statusbar.push(self.status_ctx, msg)

    # -- assign (runs the Assign To Group extension) -------------------------

    @staticmethod
    def _assign_mtime():
        try:
            return os.stat(LAST_ASSIGN_PATH).st_mtime_ns
        except OSError:
            return 0

    def assign_selection(self):
        if run_extension_live('trlo.assign-group.noprefs'):
            self.show_status("Assign To Group opened in Inkscape...")

    def assign_selection_to(self, group_name):
        """Add the current canvas selection to an existing group: same
        extension as the menu item, but told which group so it doesn't ask."""
        with open(PENDING_TARGET_PATH, 'w') as f:
            json.dump({'name': group_name, 'time': time.time()}, f)
        if run_extension_live('trlo.assign-group.noprefs'):
            self.show_status(f"Adding selection to “{group_name}”...")
        else:
            try:
                os.remove(PENDING_TARGET_PATH)
            except OSError:
                pass

    def poll_assign(self):
        # assign_group.py leaves a record of each run; fold it into this
        # panel's model (there's no way to query Inkscape for it).
        mtime = self._assign_mtime()
        if mtime != self.last_assign_mtime:
            self.last_assign_mtime = mtime
            try:
                with open(LAST_ASSIGN_PATH) as f:
                    rec = json.load(f)
                name = rec['name']
                for node_id in rec['ids']:
                    names = self.by_id.setdefault(node_id, [])
                    if name not in names:
                        names.append(name)
                self.rebuild_by_name()
                self.refresh_tree()
                self.show_status(f"Assigned {len(rec['ids'])} object(s) to “{name}”")
            except (OSError, ValueError, KeyError):
                pass
        return True

    # -- tree building ---------------------------------------------------

    def refresh_tree(self):
        self.store.clear()
        for name in sorted(self.by_name.keys()):
            members = self.by_name[name]
            git = self.store.append(None, ["folder", name, "group", name,
                                           self._eye_icon(self.group_hidden(members)),
                                           self._lock_icon(self.group_locked(members))])
            for node_id in members:
                self.store.append(git, ["text-x-generic", node_id, "object", node_id,
                                        self._eye_icon(self.state_of(node_id)['hidden']),
                                        self._lock_icon(self.state_of(node_id)['locked'])])

    # -- eye / lock state ---------------------------------------------------

    def state_of(self, node_id):
        return self.states.setdefault(node_id, {'hidden': False, 'locked': False})

    def group_hidden(self, ids):
        return bool(ids) and all(self.state_of(i)['hidden'] for i in ids)

    def group_locked(self, ids):
        return bool(ids) and all(self.state_of(i)['locked'] for i in ids)

    @staticmethod
    def _eye_icon(hidden):
        return "object-hidden-symbolic" if hidden else "object-visible-symbolic"

    @staticmethod
    def _lock_icon(locked):
        return "object-locked-symbolic" if locked else "object-unlocked-symbolic"

    def toggle_state(self, kind, item_id, which):
        """which is 'hidden' or 'locked'. A group row sets every member to the
        opposite of the group's current combined state (all on -> all off,
        otherwise all on); an object row flips just that object."""
        ids = self.by_name.get(item_id, []) if kind == "group" else [item_id]
        if not ids:
            return
        turn_on = not all(self.state_of(i)[which] for i in ids)
        select_by_id_live(ids)
        if which == 'hidden':
            ok = set_hidden_live(turn_on)
        else:
            ok = set_locked_live(turn_on)
        if not ok:
            return
        for i in ids:
            self.state_of(i)[which] = turn_on
        expanded = self._expanded_groups()
        self.refresh_tree()
        self._restore_expanded(expanded)
        word = {'hidden': ('Hid', 'Showed'), 'locked': ('Locked', 'Unlocked')}[which][0 if turn_on else 1]
        self.show_status(f"{word} {len(ids)} object(s)")

    def _expanded_groups(self):
        names = set()
        for row in self.store:
            if self.tree.row_expanded(row.path):
                names.add(row[COL_ID])
        return names

    def _restore_expanded(self, names):
        for row in self.store:
            if row[COL_ID] in names:
                self.tree.expand_row(row.path, False)

    def rebuild_by_name(self):
        by_name = {}
        for node_id, names in self.by_id.items():
            for name in names:
                by_name.setdefault(name, []).append(node_id)
        self.by_name = by_name

    def error(self, message):
        with quiet_stderr():
            dialog = Gtk.MessageDialog(
                transient_for=self.window, modal=True,
                message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK,
                text=message,
            )
            dialog.run()
            dialog.destroy()

    def confirm(self, text, secondary):
        with quiet_stderr():
            dialog = Gtk.MessageDialog(
                transient_for=self.window, modal=True,
                message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO,
                text=text,
            )
            dialog.format_secondary_text(secondary)
            response = dialog.run()
            dialog.destroy()
        return response == Gtk.ResponseType.YES

    def _selected_iter(self):
        model, it = self.tree.get_selection().get_selected()
        return it

    # -- click to select / double-click to rename -------------------------

    def on_button_press(self, widget, event):
        if event.button == 3:
            self.show_context_menu(event)
            return False
        if event.button != 1:
            return False

        path_info = self.tree.get_path_at_pos(int(event.x), int(event.y))
        if path_info is None:
            return False
        path = path_info[0]
        it = self.store.get_iter(path)
        kind = self.store[it][COL_KIND]
        item_id = self.store[it][COL_ID]

        if path_info[1] in (self.eye_col, self.lock_col):
            which = 'hidden' if path_info[1] is self.eye_col else 'locked'
            if event.type == Gdk.EventType.BUTTON_PRESS:
                self.toggle_state(kind, item_id, which)
            return True

        if event.type == Gdk.EventType._2BUTTON_PRESS and kind == "group":
            GLib.idle_add(self.start_rename, path)
            return False

        if kind == "group":
            select_by_id_live(self.by_name.get(item_id, []))
            self.show_status(f"Selected {len(self.by_name.get(item_id, []))} object(s) in “{item_id}”")
        else:
            select_by_id_live([item_id])
            self.show_status(f"Selected {item_id}")
        return False

    def start_rename(self, path):
        self.text_renderer.set_property("editable", True)
        self.tree.set_cursor(path, self.tree.get_column(0), True)
        return False

    def start_rename_selected(self):
        it = self._selected_iter()
        if it is None or self.store[it][COL_KIND] != "group":
            self.error("Select a group first.")
            return
        self.start_rename(self.store.get_path(it))

    def on_name_edited(self, renderer, path_str, new_text):
        renderer.set_property("editable", False)
        it = self.store.get_iter_from_string(path_str)
        if self.store[it][COL_KIND] != "group":
            return
        old_name = self.store[it][COL_ID]
        new_name = new_text.strip()
        if not new_name or new_name == old_name:
            return
        # Renaming into an existing group's name merges them - merge_names
        # already dedupes per-object, so this is a safe, foreseeable
        # operation, not an error case.

        for node_id in list(self.by_name.get(old_name, [])):
            new_names = merge_names(self.by_id[node_id], rename_from=old_name, rename_to=new_name)
            self.by_id[node_id] = new_names
            select_by_id_live([node_id])
            set_attribute_live(GROUP_ATTR, '|'.join(new_names))

        self.rebuild_by_name()
        self.refresh_tree()
        self.show_status(f"Renamed “{old_name}” to “{new_name}”")

    # -- delete / remove ---------------------------------------------------

    def delete_selected(self):
        it = self._selected_iter()
        if it is None:
            self.error("Select a group or object first.")
            return
        kind = self.store[it][COL_KIND]
        if kind == "group":
            self.delete_group(self.store[it][COL_ID])
        else:
            parent_iter = self.store.iter_parent(it)
            group_name = self.store[parent_iter][COL_ID]
            self.remove_object_from_group(group_name, self.store[it][COL_ID])

    def delete_group(self, name):
        count = len(self.by_name.get(name, []))
        if not self.confirm(
            f"Delete “{name}”?",
            f"Removes this group membership from all {count} object(s). "
            "The objects themselves aren't affected.",
        ):
            return
        for node_id in list(self.by_name.get(name, [])):
            new_names = merge_names(self.by_id[node_id], remove=name)
            self.by_id[node_id] = new_names
            select_by_id_live([node_id])
            set_attribute_live(GROUP_ATTR, '|'.join(new_names))
        self.rebuild_by_name()
        self.refresh_tree()
        self.show_status(f"Deleted “{name}”")

    def remove_object_from_group(self, group_name, node_id):
        new_names = merge_names(self.by_id[node_id], remove=group_name)
        self.by_id[node_id] = new_names
        select_by_id_live([node_id])
        set_attribute_live(GROUP_ATTR, '|'.join(new_names))
        self.rebuild_by_name()
        self.refresh_tree()
        self.show_status(f"Removed {node_id} from “{group_name}”")

    # -- context menu / keyboard -------------------------------------------

    def show_context_menu(self, event):
        path_info = self.tree.get_path_at_pos(int(event.x), int(event.y))
        if path_info is None:
            return
        path = path_info[0]
        self.tree.get_selection().select_path(path)
        it = self.store.get_iter(path)
        kind = self.store[it][COL_KIND]
        item_id = self.store[it][COL_ID]

        menu = Gtk.Menu()
        target = item_id if kind == "group" else self.store[self.store.iter_parent(it)][COL_ID]
        add_item = Gtk.MenuItem(label=f"Add Selection to “{target}”")
        add_item.connect("activate", lambda w: self.assign_selection_to(target))
        menu.append(add_item)
        menu.append(Gtk.SeparatorMenuItem())
        if kind == "group":
            rename_item = Gtk.MenuItem(label="Rename")
            rename_item.connect("activate", lambda w: self.start_rename(path))
            menu.append(rename_item)
            menu.append(Gtk.SeparatorMenuItem())
            del_item = Gtk.MenuItem(label="Delete Group")
            del_item.connect("activate", lambda w: self.delete_group(item_id))
            menu.append(del_item)
        else:
            parent_iter = self.store.iter_parent(it)
            group_name = self.store[parent_iter][COL_ID]
            rem_item = Gtk.MenuItem(label=f"Remove from “{group_name}”")
            rem_item.connect("activate", lambda w: self.remove_object_from_group(group_name, item_id))
            menu.append(rem_item)
        menu.show_all()
        menu.popup_at_pointer(event)

    def on_key_press(self, widget, event):
        keyname = Gdk.keyval_name(event.keyval)
        if keyname in ("Delete", "BackSpace"):
            self.delete_selected()
            return True
        return False


def main():
    if len(sys.argv) < 2:
        print("usage: expanded_groups_panel.py <snapshot.json>", file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1]) as f:
        snapshot = json.load(f)

    ExpandedGroupsPanel(snapshot.get('by_name', {}), snapshot.get('by_id', {}),
                        snapshot.get('states', {}))
    Gtk.main()


if __name__ == '__main__':
    main()
