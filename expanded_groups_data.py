"""
Pure document/data logic for the Expanded Groups family -- no gi/GTK import
here at all, deliberately. This is what the launcher (expanded_groups.py)
imports, and it must stay free of gi: importing gi triggers PyGIDeprecationWarning
on stderr (a known upstream PyGObject wart, unrelated to anything we do),
and the launcher's stderr is exactly what Inkscape watches to decide
whether to pop its "script produced additional output" dialog -- which,
per live testing, resets canvas selection when dismissed. The launcher
doesn't need GTK or D-Bus at all (it only reads the document and writes a
JSON snapshot), so it should never trigger that path in the first place.
See expanded_groups_dbus.py for the D-Bus helpers used by the panel and
assign_group.py, which do need gi and so suppress the warning explicitly.
"""

import inkex
import os
import tempfile

# Arbitrary namespace identifier for this extension's own bookkeeping
# attribute, following the same pattern Ink/Stitch uses for its own
# inkstitch: namespace -- it doesn't need to resolve to a real URL.
GROUP_NS = 'http://theroadlessordinary.com/namespace'
inkex.NSS['trlo'] = GROUP_NS
GROUP_ATTR = inkex.addNS('group', 'trlo')

# An object can belong to more than one group at once, so trlo:group holds a
# "|"-separated list of names (not comma or space -- a group name like
# "All Blues" already contains a space, and commas are common in typed
# names too).
GROUP_SEP = '|'


def get_group_names(node):
    """The list of group names a node currently belongs to."""
    value = node.get(GROUP_ATTR)
    if not value:
        return []
    return [n for n in (part.strip() for part in value.split(GROUP_SEP)) if n]


def set_group_names(node, names):
    """Store this node's group-name list, removing the attribute entirely
    if the list is empty (rather than leaving trlo:group="")."""
    names = [n for n in names if n]
    if names:
        node.set(GROUP_ATTR, GROUP_SEP.join(names))
    else:
        node.attrib.pop(GROUP_ATTR, None)


def add_group_name(node, name):
    names = get_group_names(node)
    if name not in names:
        names.append(name)
        set_group_names(node, names)


def merge_names(names, remove=None, rename_from=None, rename_to=None):
    """Pure helper: apply a remove or rename to a name list, deduping."""
    result = list(names)
    if remove is not None:
        result = [n for n in result if n != remove]
    if rename_from is not None:
        result = [rename_to if n == rename_from else n for n in result]
    seen = []
    for n in result:
        if n not in seen:
            seen.append(n)
    return seen


def scan_groups(svg):
    """Return {group_name: [nodes]} for every element carrying trlo:group.
    A node with multiple names appears once per name it carries."""
    groups = {}
    for node in svg.xpath('//*[@trlo:group]', namespaces=inkex.NSS):
        for name in get_group_names(node):
            groups.setdefault(name, []).append(node)
    return groups


def scan_groups_by_id(svg):
    """Like scan_groups, but {group_name: [id, ...]} and a companion
    {id: [name, ...]} map -- what the detached panel needs, since it can't
    hold onto lxml Element objects across the JSON handoff."""
    by_id = {}
    for node in svg.xpath('//*[@trlo:group]', namespaces=inkex.NSS):
        node_id = node.get('id')
        if not node_id:
            continue
        names = get_group_names(node)
        if names:
            by_id[node_id] = names

    by_name = {}
    for node_id, names in by_id.items():
        for name in names:
            by_name.setdefault(name, []).append(node_id)

    return by_name, by_id


def scan_states(svg):
    """{id: {'hidden': bool, 'locked': bool}} for every trlo:group node.
    Only the node's own display/lock state, not inherited from ancestors."""
    states = {}
    for node in svg.xpath('//*[@trlo:group]', namespaces=inkex.NSS):
        node_id = node.get('id')
        if not node_id:
            continue
        display = node.style.get('display') if hasattr(node, 'style') else None
        states[node_id] = {
            'hidden': display == 'none',
            'locked': node.get('sodipodi:insensitive') == 'true',
        }
    return states


# assign_group.py records what it just did here so an open browse panel (a
# separate, detached process with no way to query Inkscape) can update its
# list after its Assign button triggers the extension.
LAST_ASSIGN_PATH = os.path.join(tempfile.gettempdir(), 'expanded_groups_last_assign.json')

# The panel's "Add Selection to <group>" context-menu item writes the target
# group name here (with a timestamp) right before running assign_group.py,
# which then skips its name prompt. Ignored if older than PENDING_MAX_AGE
# seconds, so a run that never happened can't hijack a later menu use.
PENDING_TARGET_PATH = os.path.join(tempfile.gettempdir(), 'expanded_groups_pending_target.json')
PENDING_MAX_AGE = 30
