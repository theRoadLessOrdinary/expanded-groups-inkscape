"""
D-Bus helpers for controlling the live, already-open Inkscape window --
used by expanded_groups_panel.py (detached) and assign_group.py (a normal
blocking effect extension). NOT imported by the launcher (expanded_groups.py);
see expanded_groups_data.py's docstring for why that split matters.

Importing gi triggers a PyGIDeprecationWarning on stderr from PyGObject's
own internals (GLib.unix_signal_add_full), regardless of anything this
extension does. For assign_group.py, that stderr output reaches Inkscape
(it's a normal, briefly-blocking effect script) and would trigger
Inkscape's "script produced additional output" dialog on every single
Assign click. The warning is suppressed here at the import site so it
never reaches stderr in the first place.
"""

import warnings
warnings.filterwarnings('ignore')  # must precede the gi import below

import inkex
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gio, GLib

# Inkscape exposes its own action system (the same actions the Extensions/
# Edit menus call) over the session D-Bus bus as a standard GApplication
# action group. That's the only channel available for changing the LIVE
# canvas selection, or setting a live attribute value, in the already-open
# window -- a normal effect extension can only hand back a modified
# document. Confirmed select-by-id works live (status bar + highlighted
# objects actually changed, held steady) before relying on it here.
DBUS_NAME = 'org.inkscape.Inkscape'
DBUS_PATH = '/org/inkscape/Inkscape'


def _activate_live(action_name, *string_args):
    conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    params = [GLib.Variant('s', s) for s in string_args]
    conn.call_sync(
        DBUS_NAME, DBUS_PATH, 'org.gtk.Actions', 'Activate',
        GLib.Variant('(sava{sv})', (action_name, params, {})),
        None, Gio.DBusCallFlags.NONE, -1, None,
    )


def select_by_id_live(ids):
    """Select these object ids in the live, already-open Inkscape window."""
    try:
        _activate_live('select-clear')
        if ids:
            _activate_live('select-by-id', ','.join(ids))
    except GLib.Error as e:
        inkex.errormsg(f"Could not select objects in the live Inkscape window: {e}")
        return False
    return True


def set_attribute_live(attr_name, attr_value):
    """
    Set an attribute on whatever is currently selected in the live window,
    via Inkscape's own object-set-attribute action (documented usage:
    "object-set-attribute:attribute name, attribute value"). This goes
    through Inkscape's own in-memory document model, so it's tracked by
    undo/unsaved-changes correctly -- unlike writing the SVG file directly,
    which would risk clobbering whatever unsaved state Inkscape is holding.
    Caller is responsible for selecting exactly the intended object(s) with
    select_by_id_live() first.
    """
    try:
        _activate_live('object-set-attribute', f'{attr_name},{attr_value}')
    except GLib.Error as e:
        inkex.errormsg(f"Could not set {attr_name} in the live Inkscape window: {e}")
        return False
    return True
