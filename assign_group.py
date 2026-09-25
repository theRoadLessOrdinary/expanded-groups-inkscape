"""
Assign the current canvas selection to an Expanded Group. Unlike the
browse/Apply/Rename/Remove panel, this stays a normal effect extension
(briefly blocking, same as every other one-click button in this family)
because it genuinely needs live selection + each selected object's current
group memberships at the moment it runs -- and there's no D-Bus query for
either of those, only the document Inkscape hands a normal effect script.
"""

import inkex
import json
import os
import time
import warnings
warnings.filterwarnings('ignore')  # before the gi import below

# Must be set before GTK initializes -- otherwise GTK tries to register
# with the accessibility (AT-SPI) bus, which the Flatpak sandbox blocks.
# That registration is async and times out well after quiet_stderr()'s
# redirect window has already closed, so its "dbind-WARNING: Couldn't
# register with accessibility bus" lands on real stderr regardless,
# tripping Inkscape's "script produced additional output" dialog even
# though the script itself succeeded.
os.environ.setdefault('NO_AT_BRIDGE', '1')

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk

from expanded_groups_data import (GROUP_SEP, LAST_ASSIGN_PATH, PENDING_MAX_AGE,
                                  PENDING_TARGET_PATH, add_group_name)
from expanded_groups_dbus import quiet_stderr


class AssignGroup(inkex.EffectExtension):

    def effect(self):
        selected = list(self.svg.selection)
        if not selected:
            inkex.errormsg("Select at least one object on the canvas first.")
            return

        name = self.pending_target()
        if name is None:
            name = self.prompt_for_name()
        if name is None:
            return  # cancelled
        if not name:
            inkex.errormsg("Enter a group name.")
            return
        if GROUP_SEP in name:
            inkex.errormsg(f'Group names can\'t contain "{GROUP_SEP}".')
            return

        for node in selected:
            if node.get('id') is None:
                node.set('id', self.svg.get_unique_id('object'))
            add_group_name(node, name)

        with open(LAST_ASSIGN_PATH, 'w') as f:
            json.dump({'name': name, 'ids': [n.get('id') for n in selected]}, f)

    def pending_target(self):
        """Group name left by the browse panel's context menu, if fresh.
        Consumed (deleted) either way so it can't apply twice."""
        try:
            with open(PENDING_TARGET_PATH) as f:
                rec = json.load(f)
            os.remove(PENDING_TARGET_PATH)
        except (OSError, ValueError):
            return None
        if time.time() - rec.get('time', 0) > PENDING_MAX_AGE:
            return None
        return rec.get('name') or None

    def prompt_for_name(self):
        # Creating/showing the first GTK window in this process is what
        # triggers GLib's "Failed to load module ..." messages on stderr
        # (Flatpak's sandboxed GTK missing theme-integration modules) --
        # not the gi import itself. See quiet_stderr()'s docstring.
        with quiet_stderr():
            dialog = Gtk.Dialog(title="Assign To Expanded Group")
            dialog.set_modal(True)
            dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
            dialog.add_button("_OK", Gtk.ResponseType.OK)
            dialog.set_default_response(Gtk.ResponseType.OK)

            box = dialog.get_content_area()
            box.set_border_width(10)
            box.set_spacing(6)
            box.add(Gtk.Label(label="Group name:", xalign=0))
            entry = Gtk.Entry()
            entry.set_activates_default(True)
            box.add(entry)
            dialog.show_all()

            response = dialog.run()
            text = entry.get_text().strip()
            dialog.destroy()

        if response != Gtk.ResponseType.OK:
            return None
        return text


if __name__ == '__main__':
    AssignGroup().run()
