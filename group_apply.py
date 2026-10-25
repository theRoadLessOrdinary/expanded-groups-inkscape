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
  item_order: {group name: [object id, ...]} - the order the window lists
              these groups' objects in (list order only, the drawing is
              not touched); other groups keep the order they have
  rename_group: [old, new] - a renamed group keeps its object order
  drop_groups: [group name, ...] - deleted groups lose their object order
An object that joins a styled group gets that group's style. An object
that joins a group whose members are all hidden (or all locked) is hidden
(or locked) too; a group with mixed members, or no members yet, changes
nothing about the objects joining it.
Ids of objects that are no longer in the document are skipped, and after
each run the document's groups are written to EMBED_SNAPSHOT_PATH, so the
windows drop rows of objects that were deleted on the canvas.
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
                                  style_from, apply_style, scan_groups, scan_groups_by_id,
                                  get_item_order, set_item_order, write_snapshot,
                                  EMBED_SNAPSHOT_PATH, group_states, take_group_state)


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

        # Each group's hidden/locked state from its members before any
        # change, keyed by the name it has after a rename.
        states = {renamed.get(n, n): st for n, st in group_states(self.svg).items()}
        for node_id, names in rec.get('members', {}).items():
            node = self.svg.getElementById(node_id)
            if node is None:
                continue
            had = {renamed.get(n, n) for n in get_group_names(node)}
            set_group_names(node, names)
            for name in names:
                if name not in had:
                    if name in styles:
                        apply_style(node, styles[name])
                    take_group_state(node, states.get(name))

        if 'order' in rec:
            names = [n for n in rec['order'] if n and GROUP_SEP not in n]
            if names:
                self.svg.set(GROUP_ORDER_ATTR, GROUP_SEP.join(names))
            else:
                self.svg.attrib.pop(GROUP_ORDER_ATTR, None)

        self.update_item_order(rec)

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

        try:
            write_snapshot(self.svg, EMBED_SNAPSHOT_PATH)
        except OSError:
            pass

    def update_item_order(self, rec):
        """Change the stored object order of groups as asked, then keep only
        ids that are still members of their group, and only groups that
        still exist."""
        order = get_item_order(self.svg)
        before = dict(order)
        if rec.get('rename_group'):
            old, new = rec['rename_group']
            if old in order:
                moved = order.pop(old)
                order[new] = order.get(new, []) + [i for i in moved if i not in order.get(new, [])]
        for name in rec.get('drop_groups', []):
            order.pop(name, None)
        for name, ids in (rec.get('item_order') or {}).items():
            if isinstance(ids, list):
                order[name] = [i for i in ids if isinstance(i, str)]
        if not order and not before:
            return
        by_name, _ = scan_groups_by_id(self.svg)
        kept = {}
        for name, ids in order.items():
            if name in by_name:
                members = set(by_name[name])
                ids = [i for i in dict.fromkeys(ids) if i in members]
                if ids:
                    kept[name] = ids
        if kept != before:
            set_item_order(self.svg, kept)

    def style_members(self, name, style):
        for node in scan_groups(self.svg).get(name, []):
            apply_style(node, style)


if __name__ == '__main__':
    GroupApply().run()
