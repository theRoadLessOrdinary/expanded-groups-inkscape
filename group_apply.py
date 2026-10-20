"""
Apply group changes made in the Expanded Groups window to the open
document. A hidden extension (no menu entry), run by the window over
D-Bus right after it writes GROUP_APPLY_PENDING_PATH:
  members: {object id: [group name, ...]} - the new list for each object
  order:   [group name, ...] - the order the window lists groups in
Running as an effect makes each change one undo step, and inkex writes
the trlo: attributes by namespace, whatever prefix the file uses.
"""

import inkex
import json
import os
import time

from expanded_groups_data import (GROUP_ORDER_ATTR, GROUP_APPLY_PENDING_PATH, GROUP_SEP,
                                  PENDING_MAX_AGE, set_group_names)


class GroupApply(inkex.EffectExtension):

    def effect(self):
        try:
            with open(GROUP_APPLY_PENDING_PATH) as f:
                rec = json.load(f)
            os.remove(GROUP_APPLY_PENDING_PATH)
        except (OSError, ValueError):
            raise inkex.AbortExtension("No group changes were given.")
        if time.time() - rec.get('time', 0) > PENDING_MAX_AGE:
            raise inkex.AbortExtension("The group change arrived too late; try again.")

        for node_id, names in rec.get('members', {}).items():
            node = self.svg.getElementById(node_id)
            if node is not None:
                set_group_names(node, names)

        if 'order' in rec:
            names = [n for n in rec['order'] if n and GROUP_SEP not in n]
            if names:
                self.svg.set(GROUP_ORDER_ATTR, GROUP_SEP.join(names))
            else:
                self.svg.attrib.pop(GROUP_ORDER_ATTR, None)


if __name__ == '__main__':
    GroupApply().run()
