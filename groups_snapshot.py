"""
Hidden helper for the Map's Edge Tool Panel's Groups tab. Writes the open
document's groups to EMBED_SNAPSHOT_PATH, which the panel watches and
reloads its list from. Run by the tab's Refresh button over D-Bus
(EMBED_SNAPSHOT_ACTION). Makes no document edits.
"""
import inkex

from expanded_groups_data import write_snapshot, EMBED_SNAPSHOT_PATH


class GroupsSnapshot(inkex.EffectExtension):
    def effect(self):
        write_snapshot(self.svg, EMBED_SNAPSHOT_PATH)


if __name__ == '__main__':
    GroupsSnapshot().run()
