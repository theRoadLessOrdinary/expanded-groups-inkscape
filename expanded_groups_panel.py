"""
The actual Expanded Groups browse panel: a native-layers-style tree.
Launched detached by expanded_groups.py (not a child process Inkscape
waits on), so it can stay open indefinitely without blocking the main
Inkscape window.

Top-level rows are group names; expanding one reveals its member object
ids as child rows. Clicking a group row selects every member live on
canvas; clicking an object row selects just that one. Ctrl+click adds a
row to the selection or takes it out, Shift+click selects every row from
the last one clicked to this one, and the canvas selection becomes
everything in all the selected rows. Right-click and Delete act on all
the selected rows. Double-click (or
Rename, toolbar/context menu) renames a group inline. Delete removes a
group's membership from all its objects, or - on an object row - just
that one object's membership in its parent group. No nested groups
(groups are flat; only the object rows nest, for display).

Drag and drop: drag rows inside Stitch Order to change the sewing order;
drag a group up or down to change the order groups are listed in; drag an
object onto another group (or onto one of its objects) to move it there,
or hold Ctrl while dropping to add it to that group and keep it in its
own. Shift/Ctrl+click several rows of the same kind to drag them together.
The new stitch order goes through Stitch Order's hidden Reorder action and
group changes through this extension's hidden group_apply.py, both told
what to do through a small file (ORDER_PENDING_PATH, GROUP_APPLY_PENDING_PATH).
The group order is saved on the root <svg> as trlo:group_order.

This never touches the SVG file. It loads its starting model from the
JSON snapshot expanded_groups.py wrote, and every mutation goes out live
through Inkscape's D-Bus Actions interface, which runs group_apply.py on
the open document (rename, delete, remove, move and the group order), so
edits land in Inkscape's own in-memory document
and stay correctly tracked by undo/unsaved-changes - this script never
writes to disk itself. Its own model is then updated optimistically to
match, since there's no D-Bus query to read live attribute values back.

Because of that, this window's list can drift out of sync with reality
if something outside this tool changes the same objects' trlo:group
attribute while it's open. There's no live "refresh from Inkscape"
available (no D-Bus query mechanism for it) - closing and reopening the
tool re-scans the document fresh.

The first row, Stitch Order, is not a named group: it lists the objects
numbered by the Stitch Order extension (stitch-order/), in sewing order.
It selects, hides and locks like a group. Adding to it and removing from it
run the Stitch Order extension's own Add/Remove/Clear actions, which leave
a record (ORDER_RECORD_PATH) this panel polls to update the row. The toolbar's Assign
button runs the same Assign To Group extension as the menu item; that
extension leaves a small record this panel polls to update its list.
"""

import json
import os
import sys
import time
import warnings
warnings.filterwarnings('ignore')  # before the gi import in expanded_groups_dbus

os.environ.setdefault('NO_AT_BRIDGE', '1')  # see assign_group.py for why

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib

from expanded_groups_data import (LAST_ASSIGN_PATH, PENDING_TARGET_PATH, ORDER_RECORD_PATH,
                                   ORDER_PENDING_PATH, GROUP_APPLY_PENDING_PATH,
                                   PANEL_PID_PATH, PANEL_FOCUS_REQUEST_PATH, merge_names)
from expanded_groups_dbus import select_by_id_live, run_extension_live, set_hidden_live, set_locked_live, quiet_stderr

VERSION = "3.13.0"

# Pause between a live D-Bus select and the extension run that acts on it.
LIVE_ACTION_PAUSE = 0.05

# TreeStore columns: icon name, display text, kind ("group"/"object"), id
# (group name for a group row, object id for an object row)
# plus the eye/lock icon names shown at the right of each row
COL_ICON, COL_NAME, COL_KIND, COL_ID, COL_EYE, COL_LOCK = range(6)

# The Stitch Order row: kind "order", its object rows kind "order-object".
# Its id can't clash with a group name typed by the user.
ORDER_ROW_ID = "\x00stitch-order"
ORDER_ADD_ACTION = 'maps-edge.stitch-order-add.noprefs'
ORDER_REMOVE_ACTION = 'maps-edge.stitch-order-remove.noprefs'
ORDER_CLEAR_ACTION = 'maps-edge.stitch-order-clear.noprefs'
ORDER_REORDER_ACTION = 'maps-edge.stitch-order-reorder.noprefs'
# Group membership is written by an extension, not object-set-attribute:
# that action takes the attribute name literally, so "{namespace}group"
# was saved as a broken attribute, and a prefix like "trlo:group" only
# works when Inkscape knows that prefix for this document.
GROUP_APPLY_ACTION = 'trlo.group-apply.noprefs'

# Rows are only dragged inside this window.
DRAG_TARGETS = [Gtk.TargetEntry.new('expanded-groups-row', Gtk.TargetFlags.SAME_WIDGET, 0)]
# Scroll the list while a drag is this close to its top or bottom edge (px).
DRAG_SCROLL_EDGE = 24
DRAG_SCROLL_STEP = 12
BEFORE = Gtk.TreeViewDropPosition.BEFORE
AFTER = Gtk.TreeViewDropPosition.AFTER
INTO = Gtk.TreeViewDropPosition.INTO_OR_BEFORE


class ExpandedGroupsPanel:

    def __init__(self, by_name, by_id, states, stitch_order=(), inkscape_pid=None, group_order=(),
                 labels=None):
        self.labels = dict(labels or {})  # {id: Inkscape label}; rows show the label, else the id
        self.inkscape_pid = inkscape_pid  # the window closes when this process ends
        self.group_order = list(group_order)  # names in dragged order; others follow, sorted
        self.by_name = by_name  # {group_name: [id, ...]}
        self.by_id = by_id      # {id: [name, ...]}
        self.states = states    # {id: {'hidden': bool, 'locked': bool}}
        self.stitch_order = list(stitch_order)  # [id, ...] in sewing order
        self.last_assign_mtime = self._assign_mtime()
        self.last_order_mtime = self._mtime(ORDER_RECORD_PATH)
        self.last_focus_mtime = self._focus_mtime()
        self.drag_rows = []          # (kind, id, parent group) of the rows being dragged
        self.drag_y = None           # last drag height over the list, for scrolling
        self.hold_selection = False  # keep a multi-row selection while it may be dragged
        self.pending_click = None    # the click to finish on release if no drag starts
        self.anchor_path = None      # where a Shift+click range starts
        with quiet_stderr():
            self.build_dialog()

    def build_dialog(self):
        win = Gtk.Window(title=f"Expanded Groups v{VERSION}")
        win.set_default_size(320, 460)
        win.connect("destroy", self.on_destroy)
        win.connect("key-press-event", self.on_key_press)
        self.window = win

        # Claims single-instance status for this launcher's "already open?"
        # check. Written after the window exists so a focus request arriving
        # right after this can find self.window ready.
        try:
            with open(PANEL_PID_PATH, 'w') as f:
                f.write(str(os.getpid()))
        except OSError:
            pass

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

        self.tree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.tree.get_selection().set_select_function(lambda *args: not self.hold_selection)
        self.tree.connect("button-press-event", self.on_button_press)
        self.tree.connect("button-release-event", self.on_button_release)

        actions = Gdk.DragAction.MOVE | Gdk.DragAction.COPY
        self.tree.enable_model_drag_source(Gdk.ModifierType.BUTTON1_MASK, DRAG_TARGETS, actions)
        self.tree.enable_model_drag_dest(DRAG_TARGETS, actions)
        self.tree.connect("drag-begin", self.on_drag_begin)
        self.tree.connect("drag-end", self.on_drag_end)
        self.tree.connect("drag-motion", self.on_drag_motion)
        self.tree.connect("drag-drop", self.on_drag_drop)

        scroll = Gtk.ScrolledWindow()
        scroll.add(self.tree)
        vbox.pack_start(scroll, True, True, 0)

        self.statusbar = Gtk.Statusbar()
        self.status_ctx = self.statusbar.get_context_id("main")
        vbox.pack_start(self.statusbar, False, False, 0)

        self.refresh_tree()
        win.show_all()
        GLib.timeout_add(500, self.poll_assign)

    def on_destroy(self, *args):
        try:
            os.remove(PANEL_PID_PATH)
        except OSError:
            pass
        Gtk.main_quit()

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

    @staticmethod
    def _mtime(path):
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return 0

    @staticmethod
    def _focus_mtime():
        try:
            return os.stat(PANEL_FOCUS_REQUEST_PATH).st_mtime_ns
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

    def inkscape_gone(self):
        if self.inkscape_pid is None:
            return False
        try:
            os.kill(self.inkscape_pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return False

    def poll_assign(self):
        if self.inkscape_gone():
            self.window.destroy()
            return False
        # Also checked here (same 500ms tick) rather than a separate timer:
        # the launcher touches PANEL_FOCUS_REQUEST_PATH instead of spawning
        # a second panel when one is already open, and this is how that
        # request reaches the existing window.
        focus_mtime = self._focus_mtime()
        if focus_mtime != self.last_focus_mtime:
            self.last_focus_mtime = focus_mtime
            self.window.present()

        # stitch_order.py leaves the whole order after each change.
        order_mtime = self._mtime(ORDER_RECORD_PATH)
        if order_mtime != self.last_order_mtime:
            self.last_order_mtime = order_mtime
            try:
                with open(ORDER_RECORD_PATH) as f:
                    rec = json.load(f)
                self.stitch_order = list(rec['ids'])
                self.labels.update(rec.get('labels', {}))
                expanded = self._expanded_groups()
                self.refresh_tree()
                self._restore_expanded(expanded)
                self.show_status(f"Stitch order: {len(self.stitch_order)} object(s)")
            except (OSError, ValueError, KeyError):
                pass

        # assign_group.py leaves a record of each run; fold it into this
        # panel's model (there's no way to query Inkscape for it).
        mtime = self._assign_mtime()
        if mtime != self.last_assign_mtime:
            self.last_assign_mtime = mtime
            try:
                with open(LAST_ASSIGN_PATH) as f:
                    rec = json.load(f)
                name = rec['name']
                self.labels.update(rec.get('labels', {}))
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

    def refresh_tree(self, renamed=None):
        """Rebuild the list from the model, keeping which rows are selected
        and expanded. renamed=(old, new) carries a renamed group's rows over."""
        selected = self.selected_rows() if len(self.store) else []
        expanded = self._expanded_groups()
        if renamed:
            old, new = renamed
            selected = [(k, new if (k == "group" and i == old) else i, new if p == old else p)
                        for k, i, p in selected]
            expanded = {new if n == old else n for n in expanded}
        self.anchor_path = None
        self.store.clear()
        order = self.stitch_order
        oit = self.store.append(None, ["format-list-ordered", "Stitch Order", "order", ORDER_ROW_ID,
                                       self._eye_icon(self.group_hidden(order)),
                                       self._lock_icon(self.group_locked(order))])
        for n, node_id in enumerate(order, start=1):
            self.store.append(oit, ["text-x-generic", f"{n}. {self.name_of(node_id)}", "order-object", node_id,
                                    self._eye_icon(self.state_of(node_id)['hidden']),
                                    self._lock_icon(self.state_of(node_id)['locked'])])
        for name in self.ordered_group_names():
            members = self.by_name[name]
            git = self.store.append(None, ["folder", name, "group", name,
                                           self._eye_icon(self.group_hidden(members)),
                                           self._lock_icon(self.group_locked(members))])
            for node_id in members:
                self.store.append(git, ["text-x-generic", self.name_of(node_id), "object", node_id,
                                        self._eye_icon(self.state_of(node_id)['hidden']),
                                        self._lock_icon(self.state_of(node_id)['locked'])])
        self._restore_expanded(expanded)
        self.reselect(selected)

    def name_of(self, node_id):
        """What a row shows for an object: its label, or its id if it has none."""
        return self.labels.get(node_id) or node_id

    def ordered_group_names(self):
        """Groups in the order they were dragged into, then the rest sorted."""
        names = [n for n in dict.fromkeys(self.group_order) if n in self.by_name]
        return names + sorted(n for n in self.by_name if n not in names)

    # -- eye / lock state ---------------------------------------------------

    def members(self, kind, item_id):
        """The object ids a row stands for."""
        if kind == "group":
            return list(self.by_name.get(item_id, []))
        if kind == "order":
            return list(self.stitch_order)
        return [item_id]

    def state_of(self, node_id):
        return self.states.setdefault(node_id, {'hidden': False, 'locked': False})

    def group_hidden(self, ids):
        return bool(ids) and all(self.state_of(i)['hidden'] for i in ids)

    def group_locked(self, ids):
        return bool(ids) and all(self.state_of(i)['locked'] for i in ids)

    @staticmethod
    def _eye_icon(hidden):
        return "view-conceal-symbolic" if hidden else "view-reveal-symbolic"

    @staticmethod
    def _lock_icon(locked):
        return "object-locked-symbolic" if locked else "object-unlocked-symbolic"

    def toggle_state(self, kind, item_id, which, rows=None):
        """which is 'hidden' or 'locked'. The clicked row decides: a group row
        sets every member to the opposite of the group's current combined
        state (all on -> all off, otherwise all on); an object row flips that
        object. With rows (the selected rows, when the clicked one is among
        them), every object in all of them gets that same new state."""
        clicked = self.members(kind, item_id)
        if not clicked:
            return
        turn_on = not all(self.state_of(i)[which] for i in clicked)
        ids = list(dict.fromkeys(i for k, r, _ in (rows or [(kind, item_id, None)])
                                 for i in self.members(k, r)))
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

    def isolate_state(self, rows, which, this_value, other_value):
        """Set the objects in these rows to this_value and every other known
        object to other_value, for which='hidden' or 'locked'. Used by the
        "all but this" context menu items."""
        this_ids = set()
        for kind, item_id, _ in rows:
            this_ids.update(self.members(kind, item_id))
        other_ids = (set(self.by_id.keys()) | set(self.stitch_order)) - this_ids
        setter = set_hidden_live if which == 'hidden' else set_locked_live

        if this_ids:
            select_by_id_live(list(this_ids))
            setter(this_value)
            for i in this_ids:
                self.state_of(i)[which] = this_value
        if other_ids:
            select_by_id_live(list(other_ids))
            setter(other_value)
            for i in other_ids:
                self.state_of(i)[which] = other_value

        expanded = self._expanded_groups()
        self.refresh_tree()
        self._restore_expanded(expanded)
        verb = {'hidden': 'Hid', 'locked': 'Locked'}[which] if other_value else \
               {'hidden': 'Showed', 'locked': 'Unlocked'}[which]
        self.show_status(f"{verb} {len(other_ids)} object(s) other than {self.rows_label(rows)}")

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

    def selected_rows(self):
        """(kind, id, parent group or None) for each selected row."""
        model, paths = self.tree.get_selection().get_selected_rows()
        rows = []
        for path in paths:
            it = model.get_iter(path)
            parent = model.iter_parent(it)
            rows.append((model[it][COL_KIND], model[it][COL_ID],
                         model[parent][COL_ID] if parent is not None else None))
        return rows

    def reselect(self, rows):
        """Select these (kind, id, parent) rows again after a refresh. An
        object that moved to another group is selected where it is now."""
        wanted = set(rows)
        selection = self.tree.get_selection()
        found, moved = set(), {}

        def walk(it, parent):
            while it is not None:
                row = self.store[it]
                key = (row[COL_KIND], row[COL_ID], parent)
                if key in wanted:
                    selection.select_iter(it)
                    found.add(key)
                else:
                    moved.setdefault(key[:2], []).append(it)
                walk(self.store.iter_children(it), row[COL_ID])
                it = self.store.iter_next(it)
        walk(self.store.get_iter_first(), None)
        for key in wanted - found:
            for it in moved.get(key[:2], []):
                selection.select_iter(it)

    @staticmethod
    def rows_label(rows):
        if len(rows) == 1:
            kind, item_id, _ = rows[0]
            if kind == "order":
                return "“Stitch Order”"
            return f"“{item_id if kind == 'group' else self.name_of(item_id)}”"
        return f"the {len(rows)} selected rows"

    def _selected_iter(self):
        """The first selected row (rename and delete act on one row)."""
        model, paths = self.tree.get_selection().get_selected_rows()
        return model.get_iter(paths[0]) if paths else None

    def select_rows_on_canvas(self):
        """Select, on the canvas, everything in all the selected rows."""
        model, paths = self.tree.get_selection().get_selected_rows()
        ids, seen = [], set()
        for path in paths:
            row = model[path]
            for node_id in self.members(row[COL_KIND], row[COL_ID]):
                if node_id not in seen:
                    seen.add(node_id)
                    ids.append(node_id)
        select_by_id_live(ids)
        self.show_status(f"Selected {len(ids)} object(s) in {len(paths)} row(s)")

    # -- click to select / double-click to rename -------------------------

    def on_button_press(self, widget, event):
        if event.button == 3:
            self.show_context_menu(event)
            return True  # otherwise the tree would select only this row
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
                # On one of several selected rows it acts on all of them.
                selection = self.tree.get_selection()
                rows = None
                if selection.path_is_selected(path) and selection.count_selected_rows() > 1:
                    rows = self.selected_rows()
                self.toggle_state(kind, item_id, which, rows)
            return True

        if event.type == Gdk.EventType._2BUTTON_PRESS and kind == "group":
            GLib.idle_add(self.start_rename, path)
            return False

        # Shift+click selects every row from the last one clicked to this one
        # (Ctrl+Shift adds them to the selection). Ctrl+click adds the row to
        # the selection, or takes it out again.
        shift = event.state & Gdk.ModifierType.SHIFT_MASK
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK
        selection = self.tree.get_selection()
        if shift and self.anchor_path is not None:
            if not ctrl:
                selection.unselect_all()
            selection.select_range(self.anchor_path, path)
            self.select_rows_on_canvas()
            return True
        if shift or ctrl:
            if selection.path_is_selected(path):
                selection.unselect_path(path)
            else:
                selection.select_path(path)
            self.anchor_path = path.copy()
            self.select_rows_on_canvas()
            return True

        # A plain press on one of several selected rows may start dragging
        # them all, so the selection waits until the button comes up.
        selection = self.tree.get_selection()
        if selection.path_is_selected(path) and selection.count_selected_rows() > 1:
            self.hold_selection = True
            self.pending_click = path
            return False
        self.click_row(path)
        return False

    def on_button_release(self, widget, event):
        if event.button == 1 and self.pending_click is not None:
            path = self.pending_click
            self.pending_click = None
            self.hold_selection = False
            self.click_row(path)
        return False

    def click_row(self, path):
        """A plain click: this becomes the only selected row, and its
        objects the canvas selection."""
        it = self.store.get_iter(path)
        kind = self.store[it][COL_KIND]
        item_id = self.store[it][COL_ID]
        selection = self.tree.get_selection()
        selection.unselect_all()
        selection.select_path(path)
        self.anchor_path = path.copy()

        if kind in ("group", "order"):
            ids = self.members(kind, item_id)
            select_by_id_live(ids)
            name = "Stitch Order" if kind == "order" else item_id
            self.show_status(f"Selected {len(ids)} object(s) in “{name}”")
        else:
            select_by_id_live([item_id])
            self.show_status(f"Selected {self.name_of(item_id)}")

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

        changes = {}
        for node_id in self.by_name.get(old_name, []):
            changes[node_id] = merge_names(self.by_id[node_id], rename_from=old_name, rename_to=new_name)
        if old_name in self.group_order:
            self.group_order = [new_name if n == old_name else n for n in self.group_order]
        if not self.apply_groups(changes, with_order=True):
            return
        self.refresh_tree(renamed=(old_name, new_name))
        self.show_status(f"Renamed “{old_name}” to “{new_name}”")

    # -- delete / remove ---------------------------------------------------

    def delete_selected(self):
        rows = self.selected_rows()
        if not rows:
            self.error("Select a group or object first.")
            return
        kinds = {kind for kind, _, _ in rows}
        if "order" in kinds:
            self.clear_stitch_order()
        elif kinds == {"order-object"}:
            self.remove_from_stitch_order([item_id for _, item_id, _ in rows])
        elif kinds == {"group"}:
            self.delete_groups([item_id for _, item_id, _ in rows])
        elif kinds == {"object"}:
            self.remove_objects([(item_id, parent) for _, item_id, parent in rows])
        else:
            self.error("Select only groups, only objects in groups, or only objects in Stitch Order.")

    def delete_groups(self, names):
        ids = {i for name in names for i in self.by_name.get(name, [])}
        title = f"Delete “{names[0]}”?" if len(names) == 1 else f"Delete {len(names)} groups?"
        if not self.confirm(
            title,
            f"Removes {'this group' if len(names) == 1 else 'these groups'} from all {len(ids)} object(s). "
            "The objects themselves aren't affected.",
        ):
            return
        changes = {}
        for node_id in ids:
            remaining = self.by_id[node_id]
            for name in names:
                remaining = merge_names(remaining, remove=name)
            changes[node_id] = remaining
        if not self.apply_groups(changes):
            return
        self.refresh_tree()
        if len(names) == 1:
            self.show_status(f"Deleted “{names[0]}”")
        else:
            self.show_status(f"Deleted {len(names)} groups")

    def remove_objects(self, pairs):
        """Take each (object id, group name) membership away."""
        changes = {}
        for node_id, group_name in pairs:
            changes[node_id] = merge_names(changes.get(node_id, self.by_id[node_id]), remove=group_name)
        if not self.apply_groups(changes):
            return
        expanded = self._expanded_groups()
        self.refresh_tree()
        self._restore_expanded(expanded)
        if len(pairs) == 1:
            self.show_status(f"Removed {self.name_of(pairs[0][0])} from “{pairs[0][1]}”")
        else:
            self.show_status(f"Removed {len(pairs)} object(s) from their groups")

    # -- stitch order (runs the Stitch Order extension) -------------------

    def add_selection_to_stitch_order(self):
        if run_extension_live(ORDER_ADD_ACTION):
            self.show_status("Adding selection to Stitch Order...")

    def remove_from_stitch_order(self, ids):
        select_by_id_live(ids)
        time.sleep(LIVE_ACTION_PAUSE)
        if run_extension_live(ORDER_REMOVE_ACTION):
            what = self.name_of(ids[0]) if len(ids) == 1 else f"{len(ids)} objects"
            self.show_status(f"Removing {what} from Stitch Order...")

    def clear_stitch_order(self):
        if not self.stitch_order:
            return
        if not self.confirm("Clear the stitch order?",
                            f"Takes the number off all {len(self.stitch_order)} object(s). "
                            "The objects themselves aren't affected."):
            return
        if run_extension_live(ORDER_CLEAR_ACTION):
            self.show_status("Clearing Stitch Order...")

    # -- drag and drop -------------------------------------------------------

    def on_drag_begin(self, widget, context):
        self.pending_click = None
        self.drag_rows = self.selected_rows()
        self.drag_y = None
        GLib.timeout_add(50, self.drag_scroll)

    def on_drag_end(self, widget, context):
        self.drag_rows = []
        self.drag_y = None
        self.hold_selection = False

    def drag_scroll(self):
        """Scroll the list while a drag waits near its top or bottom."""
        if not self.drag_rows:
            return False
        if self.drag_y is not None:
            adj = self.tree.get_vadjustment()
            height = self.tree.get_allocated_height()
            if self.drag_y < DRAG_SCROLL_EDGE:
                adj.set_value(max(adj.get_lower(), adj.get_value() - DRAG_SCROLL_STEP))
            elif self.drag_y > height - DRAG_SCROLL_EDGE:
                adj.set_value(min(adj.get_upper() - adj.get_page_size(),
                                  adj.get_value() + DRAG_SCROLL_STEP))
        return True

    @staticmethod
    def copy_wanted():
        """Ctrl held: add an object to the group it is dropped on and keep it
        in its own group too."""
        display = Gdk.Display.get_default()
        pointer = display.get_default_seat().get_pointer()
        _, _, _, mask = display.get_default_screen().get_root_window().get_device_position(pointer)
        return bool(mask & Gdk.ModifierType.CONTROL_MASK)

    def drop_plan(self, x, y):
        """What dropping the dragged rows here would do, as (what, value,
        highlight path, highlight position), or None where they can't go."""
        rows = self.drag_rows
        kinds = {kind for kind, _, _ in rows}
        if len(kinds) != 1:
            return None
        kind = kinds.pop()
        dest = self.tree.get_dest_row_at_pos(x, y)
        before = dest is not None and dest[1] in (BEFORE, Gtk.TreeViewDropPosition.INTO_OR_BEFORE)
        dest_kind = self.store[dest[0]][COL_KIND] if dest else None

        if kind == "order-object":
            if dest is None or dest_kind not in ("order", "order-object"):
                return None
            path = dest[0]
            if dest_kind == "order":
                at = 0 if before or self.tree.row_expanded(path) else len(self.stitch_order)
            else:
                at = path[1] if before else path[1] + 1
            moving = [i for i in self.stitch_order if i in {r[1] for r in rows}]
            rest = [i for i in self.stitch_order if i not in moving]
            at -= sum(1 for i in self.stitch_order[:at] if i in moving)
            return ("order", rest[:at] + moving + rest[at:], path, BEFORE if before else AFTER)

        if kind == "group":
            names = self.ordered_group_names()
            if dest is None:
                at, path, pos = len(names), Gtk.TreePath(len(names)), AFTER
            elif dest_kind in ("order", "order-object"):
                at, path, pos = 0, Gtk.TreePath(1), BEFORE
            else:
                top = Gtk.TreePath(dest[0][0])
                index = top[0] - 1  # row 0 is Stitch Order
                if dest_kind == "group" and before:
                    at, path, pos = index, top, BEFORE
                else:
                    at, path, pos = index + 1, top, AFTER
            if path[0] > len(names):
                path = Gtk.TreePath(len(names))
            moving = [n for n in names if n in {r[1] for r in rows}]
            rest = [n for n in names if n not in moving]
            at -= sum(1 for n in names[:at] if n in moving)
            return ("groups", rest[:at] + moving + rest[at:], path, pos)

        if kind == "object":
            if dest is None or dest_kind not in ("group", "object"):
                return None
            top = Gtk.TreePath(dest[0][0])
            target = self.store[top][COL_ID]
            if all(parent == target for _, _, parent in rows):
                return None
            return ("move", target, top, INTO)
        return None

    def on_drag_motion(self, widget, context, x, y, time_):
        self.drag_y = y
        plan = self.drop_plan(x, y)
        if plan is None:
            self.tree.set_drag_dest_row(None, BEFORE)
            Gdk.drag_status(context, 0, time_)
            return True
        self.tree.set_drag_dest_row(plan[2], plan[3])
        copy = plan[0] == "move" and self.copy_wanted()
        Gdk.drag_status(context, Gdk.DragAction.COPY if copy else Gdk.DragAction.MOVE, time_)
        return True

    def on_drag_drop(self, widget, context, x, y, time_):
        plan = self.drop_plan(x, y)
        copy = plan is not None and plan[0] == "move" and self.copy_wanted()
        rows = list(self.drag_rows)
        Gtk.drag_finish(context, plan is not None, False, time_)
        if plan is not None:
            # After the drag has let go of the pointer.
            GLib.idle_add(self.apply_drop, plan, rows, copy)
        return True

    def apply_drop(self, plan, rows, copy):
        what, value = plan[0], plan[1]
        expanded = self._expanded_groups()
        if what == "order":
            if value != self.stitch_order:
                self.set_stitch_order(value)
        elif what == "groups":
            if value != self.ordered_group_names():
                self.group_order = value
                if not self.apply_groups({}, with_order=True):
                    return False
                self.refresh_tree()
                self._restore_expanded(expanded)
                self.show_status("Group order changed")
        elif what == "move":
            self.move_objects(rows, value, copy, expanded)
        return False

    def set_stitch_order(self, ids):
        with open(ORDER_PENDING_PATH, 'w') as f:
            json.dump({'ids': ids, 'time': time.time()}, f)
        if not run_extension_live(ORDER_REORDER_ACTION):
            try:
                os.remove(ORDER_PENDING_PATH)
            except OSError:
                pass
            return
        # The record Stitch Order leaves will confirm this; show it now.
        expanded = self._expanded_groups()
        self.stitch_order = list(ids)
        self.refresh_tree()
        self._restore_expanded(expanded)
        self.show_status("Stitch order changed")

    def apply_groups(self, changes, with_order=False):
        """Write new group lists ({id: [name, ...]}) and, if asked, the
        group order into the open document in one step (one undo), then
        update this window's model to match."""
        changes = {i: names for i, names in changes.items() if names != self.by_id.get(i, [])}
        if not changes and not with_order:
            return True
        updated = dict(self.by_id)
        updated.update(changes)
        self.by_id = updated
        self.rebuild_by_name()
        rec = {'members': changes, 'time': time.time()}
        if with_order:
            rec['order'] = self.ordered_group_names()
        with open(GROUP_APPLY_PENDING_PATH, 'w') as f:
            json.dump(rec, f)
        if run_extension_live(GROUP_APPLY_ACTION):
            return True
        try:
            os.remove(GROUP_APPLY_PENDING_PATH)
        except OSError:
            pass
        return False

    def move_objects(self, rows, target, copy, expanded):
        changes = {}
        for _, node_id, source in rows:
            if source == target and not copy:
                continue
            old = self.by_id.get(node_id, [])
            if copy or source is None:
                new = merge_names(old + [target])
            else:
                new = merge_names(old, rename_from=source, rename_to=target)
            if new != old:
                changes[node_id] = new
        if not self.apply_groups(changes):
            return
        self.refresh_tree()
        self._restore_expanded(expanded | {target})
        verb = "Added" if copy else "Moved"
        self.show_status(f"{verb} {len(changes)} object(s) to “{target}”")

    # -- context menu / keyboard -------------------------------------------

    def show_context_menu(self, event):
        """The menu acts on every selected row. Right-clicking a row that
        isn't selected selects just that row first."""
        path_info = self.tree.get_path_at_pos(int(event.x), int(event.y))
        if path_info is None:
            return
        path = path_info[0]
        selection = self.tree.get_selection()
        if not selection.path_is_selected(path):
            selection.unselect_all()
            selection.select_path(path)
            self.anchor_path = path.copy()
        rows = self.selected_rows()
        kinds = {kind for kind, _, _ in rows}
        single = len(rows) == 1
        kind, item_id, parent = rows[0]
        menu = Gtk.Menu()

        def add(label, callback):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda w: callback())
            menu.append(item)

        if kinds <= {"order", "order-object"}:
            add("Add Selection to Stitch Order", self.add_selection_to_stitch_order)
            menu.append(Gtk.SeparatorMenuItem())
            if "order" in kinds:
                add("Clear Stitch Order", self.clear_stitch_order)
            else:
                ids = [i for _, i, _ in rows]
                add("Remove from Stitch Order" if single else f"Remove {len(ids)} from Stitch Order",
                    lambda: self.remove_from_stitch_order(ids))
        elif kinds == {"group"}:
            if single:
                add(f"Add Selection to “{item_id}”", lambda: self.assign_selection_to(item_id))
                menu.append(Gtk.SeparatorMenuItem())
                add("Rename", lambda: self.start_rename(path))
                menu.append(Gtk.SeparatorMenuItem())
                add("Delete Group", lambda: self.delete_groups([item_id]))
            else:
                names = [i for _, i, _ in rows]
                add(f"Delete {len(names)} Groups", lambda: self.delete_groups(names))
        elif kinds == {"object"}:
            if single:
                add(f"Add Selection to “{parent}”", lambda: self.assign_selection_to(parent))
                menu.append(Gtk.SeparatorMenuItem())
                add(f"Remove from “{parent}”", lambda: self.remove_objects([(item_id, parent)]))
            else:
                pairs = [(i, g) for _, i, g in rows]
                add(f"Remove {len(pairs)} from Their Groups", lambda: self.remove_objects(pairs))

        self._append_isolate_items(menu, rows)
        menu.show_all()
        menu.popup_at_pointer(event)

    def _append_isolate_items(self, menu, rows):
        menu.append(Gtk.SeparatorMenuItem())
        word = "this" if len(rows) == 1 else "these"
        for label, which, this_value, other_value in (
                ("Hide", 'hidden', False, True), ("Show", 'hidden', True, False),
                ("Lock", 'locked', False, True), ("Unlock", 'locked', True, False)):
            item = Gtk.MenuItem(label=f"{label} all but {word}")
            item.connect("activate", lambda w, a=which, b=this_value, c=other_value:
                         self.isolate_state(rows, a, b, c))
            menu.append(item)

    def on_key_press(self, widget, event):
        keyname = Gdk.keyval_name(event.keyval)
        if keyname in ("Delete", "BackSpace"):
            self.delete_selected()
            return True
        return False


def main():
    if len(sys.argv) < 2:
        print("usage: expanded_groups_panel.py <snapshot.json> [inkscape pid]", file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1]) as f:
        snapshot = json.load(f)

    try:
        inkscape_pid = int(sys.argv[2])
    except (IndexError, ValueError):
        inkscape_pid = None
    ExpandedGroupsPanel(snapshot.get('by_name', {}), snapshot.get('by_id', {}),
                        snapshot.get('states', {}), snapshot.get('stitch_order', []),
                        inkscape_pid, snapshot.get('group_order', []), snapshot.get('labels', {}))
    Gtk.main()


if __name__ == '__main__':
    main()
