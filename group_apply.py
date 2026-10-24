"""
Apply group changes made in the Expanded Groups window to the open
document. A hidden extension (no menu entry), run by the window over
D-Bus right after it writes GROUP_APPLY_PENDING_PATH:
  members: {object id: [group name, ...]} - the new list for each object
  order:   [group name, ...] - the order the window lists groups in
  rename_style: [old, new] - a renamed group keeps its style
  drop_styles: [group name, ...] - deleted groups lose their style
  set_style: group name - take the style of the selected object, store it
             for the group and put it on every member
  reapply_style: group name - put the group's style on every member again
  clear_style: group name - forget the group's style (members keep theirs)
An object that joins a styled group gets that group's style.
Running as an effect makes each change one undo step, and inkex writes
the trlo: attributes by namespace, whatever prefix the file uses.
"""

import inkex
import json
import os
import time

from expanded_groups_data import (GROUP_ORDER_ATTR, GROUP_APPLY_PENDING_PATH, GROUP_SEP,
                                  PENDING_MAX_AGE, STYLES_RECORD_PATH, set_group_names,
                                  get_group_names, get_group_styles, set_group_styles,
                                  style_from, apply_style, scan_groups)


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

        styles = get_group_styles(self.svg)
        before = dict(styles)
        renamed = {}
        if rec.get('rename_style'):
            old, new = rec['rename_style']
            renamed[old] = new
            if old in styles:
                style = styles.pop(old)
                styles.setdefault(new, style)
        for name in rec.get('drop_styles', []):
            styles.pop(name, None)

        for node_id, names in rec.get('members', {}).items():
            node = self.svg.getElementById(node_id)
            if node is None:
                continue
            had = {renamed.get(n, n) for n in get_group_names(node)}
            set_group_names(node, names)
            for name in names:
                if name not in had and name in styles:
                    apply_style(node, styles[name])

        if 'order' in rec:
            names = [n for n in rec['order'] if n and GROUP_SEP not in n]
            if names:
                self.svg.set(GROUP_ORDER_ATTR, GROUP_SEP.join(names))
            else:
                self.svg.attrib.pop(GROUP_ORDER_ATTR, None)

        if rec.get('set_style'):
            name = rec['set_style']
            source = next(iter(self.svg.selection), None)
            style = style_from(source) if source is not None else None
            if style is None:
                raise inkex.AbortExtension(
                    "Select one object on the canvas that has the fill and stroke you want, "
                    "then choose Set Style from Selection again.")
            styles[name] = style
            self.style_members(name, style)
        if rec.get('reapply_style') and rec['reapply_style'] in styles:
            self.style_members(rec['reapply_style'], styles[rec['reapply_style']])
        if rec.get('clear_style'):
            styles.pop(rec['clear_style'], None)

        if styles != before:
            set_group_styles(self.svg, styles)
            with open(STYLES_RECORD_PATH, 'w') as f:
                json.dump({'styles': styles, 'time': time.time()}, f)

    def style_members(self, name, style):
        for node in scan_groups(self.svg).get(name, []):
            apply_style(node, style)


if __name__ == '__main__':
    GroupApply().run()
