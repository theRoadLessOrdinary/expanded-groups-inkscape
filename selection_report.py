"""
Hidden helper for the Expanded Groups list's Highlight Selected button.
Writes the ids of the objects selected on the canvas, plus everything
inside them (a click on the canvas often selects a whole native group),
to SELECTION_REPORT_PATH. Makes no document edits.
"""
import json
import os

import inkex

from expanded_groups_data import SELECTION_REPORT_PATH


class SelectionReport(inkex.EffectExtension):
    def effect(self):
        ids = []
        for elem in self.svg.selection.values():
            for node in [elem] + list(elem.descendants()):
                node_id = node.get('id')
                if node_id and node_id not in ids:
                    ids.append(node_id)
        tmp = SELECTION_REPORT_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'ids': ids}, f)
        os.replace(tmp, SELECTION_REPORT_PATH)


if __name__ == '__main__':
    SelectionReport().run()
