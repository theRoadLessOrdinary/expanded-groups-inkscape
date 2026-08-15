"""
Assign the current canvas selection to an Expanded Group. Unlike the
browse/Apply/Rename/Remove panel, this stays a normal effect extension
(briefly blocking, same as every other one-click button in this family)
because it genuinely needs live selection + each selected object's current
group memberships at the moment it runs -- and there's no D-Bus query for
either of those, only the document Inkscape hands a normal effect script.
"""

import inkex
import warnings
warnings.filterwarnings('ignore')  # before the gi import below

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk

from expanded_groups_data import GROUP_SEP, add_group_name


class AssignGroup(inkex.EffectExtension):

    def effect(self):
        selected = list(self.svg.selection)
        if not selected:
            inkex.errormsg("Select at least one object on the canvas first.")
            return

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

    def prompt_for_name(self):
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
