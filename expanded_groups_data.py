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
import json
import os
import tempfile

# Arbitrary namespace identifier for this extension's own bookkeeping
# attribute, following the same pattern Ink/Stitch uses for its own
# inkstitch: namespace -- it doesn't need to resolve to a real URL.
GROUP_NS = 'http://theroadlessordinary.com/namespace'
inkex.NSS['trlo'] = GROUP_NS
GROUP_ATTR = inkex.addNS('group', 'trlo')
# Set by the Stitch Order extension (stitch-order/); shown here as its own entry.
ORDER_ATTR = inkex.addNS('stitch_order', 'trlo')
# On the root <svg>: the order the window lists groups in (dragged by the
# user), "|"-separated. Groups not in it are listed after, alphabetically.
GROUP_ORDER_ATTR = inkex.addNS('group_order', 'trlo')
# On the root <svg>: each styled group's fill and stroke, as JSON
# {group name: {property: value}}. Objects get it when the style is set
# and whenever they are added to the group; the latest one applied wins.
# Taking an object out of a group leaves its style as it is.
GROUP_STYLES_ATTR = inkex.addNS('group_styles', 'trlo')
# The properties a group style carries. Fill and stroke are always taken
# (black and none when the source object doesn't set them); the rest only
# when the source object sets them.
STYLE_PROPS = ('fill', 'fill-opacity', 'fill-rule', 'stroke', 'stroke-opacity',
               'stroke-width', 'stroke-dasharray', 'stroke-dashoffset',
               'stroke-linecap', 'stroke-linejoin', 'stroke-miterlimit', 'paint-order')

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


def scan_stitch_order(svg):
    """Ids of the objects numbered by the Stitch Order extension, in stitch
    order (number, then document order), same rule as stitch_order.py."""
    numbered = []
    for i, node in enumerate(svg.iter()):
        value = node.get(ORDER_ATTR) if isinstance(node.tag, str) else None
        if value is None or not node.get('id'):
            continue
        try:
            numbered.append((float(value), i, node.get('id')))
        except ValueError:
            pass
    return [node_id for _, _, node_id in sorted(numbered)]


def scan_labels(svg):
    """{id: label} for every trlo:group or stitch-order node that has an
    Inkscape label, so the panel can show labels instead of ids."""
    labels = {}
    for node in svg.xpath('//*[@trlo:group or @trlo:stitch_order]', namespaces=inkex.NSS):
        label = node.get(inkex.addNS('label', 'inkscape'))
        if label and node.get('id'):
            labels[node.get('id')] = label
    return labels


def scan_group_order(svg):
    """The group names in the order the user dragged them into."""
    value = svg.get(GROUP_ORDER_ATTR) or ''
    return [n for n in (part.strip() for part in value.split(GROUP_SEP)) if n]


def get_group_styles(svg):
    """{group name: {property: value}} stored on the root <svg>."""
    try:
        styles = json.loads(svg.get(GROUP_STYLES_ATTR) or '{}')
    except ValueError:
        return {}
    return styles if isinstance(styles, dict) else {}


def set_group_styles(svg, styles):
    styles = {name: style for name, style in styles.items() if style}
    if styles:
        svg.set(GROUP_STYLES_ATTR, json.dumps(styles, sort_keys=True))
    else:
        svg.attrib.pop(GROUP_STYLES_ATTR, None)


def style_from(node):
    """The fill and stroke a group takes from an object. A native group
    gives the style of the first shape inside it."""
    if isinstance(node, inkex.Group):
        shapes = [d for d in node.descendants() if isinstance(d, inkex.ShapeElement)
                  and not isinstance(d, inkex.Group)]
        if not shapes:
            return None
        node = shapes[0]
    specified = node.specified_style()
    style = {'fill': specified.get('fill') or 'black',
             'stroke': specified.get('stroke') or 'none'}
    for prop in STYLE_PROPS:
        if prop not in style and specified.get(prop) is not None:
            style[prop] = specified.get(prop)
    return style


def apply_style(node, style):
    """Put a group style on an object. On a native group it goes on every
    shape inside it, as Inkscape's own Fill and Stroke does."""
    if isinstance(node, inkex.Group):
        targets = [d for d in node.descendants() if isinstance(d, inkex.ShapeElement)
                   and not isinstance(d, inkex.Group)]
    else:
        targets = [node]
    for target in targets:
        for prop, value in style.items():
            target.style[prop] = value


def scan_states(svg):
    """{id: {'hidden': bool, 'locked': bool}} for every trlo:group node and
    every stitch-order node. Only the node's own display/lock state, not
    inherited from ancestors."""
    states = {}
    for node in svg.xpath('//*[@trlo:group or @trlo:stitch_order]', namespaces=inkex.NSS):
        node_id = node.get('id')
        if not node_id:
            continue
        display = node.style.get('display') if hasattr(node, 'style') else None
        states[node_id] = {
            'hidden': display == 'none',
            'locked': node.get('sodipodi:insensitive') == 'true',
        }
    return states


def snapshot(svg):
    """Everything the groups list needs from the document, as plain data."""
    by_name, by_id = scan_groups_by_id(svg)
    return {'by_name': by_name, 'by_id': by_id,
            'stitch_order': scan_stitch_order(svg),
            'group_order': scan_group_order(svg),
            'group_styles': get_group_styles(svg),
            'labels': scan_labels(svg),
            'states': scan_states(svg)}


def write_snapshot(svg, path):
    """Write snapshot(svg) to path in one step, so a reader never sees half a file."""
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(snapshot(svg), f)
    os.replace(tmp, path)


# The Map's Edge Tool Panel shows the groups list in its own Groups tab.
# tool_panel.py and groups_snapshot.py write the document's groups here,
# and the panel reloads its list whenever this file changes.
EMBED_SNAPSHOT_PATH = os.path.join(tempfile.gettempdir(), 'maps_edge_panel_groups.json')
EMBED_SNAPSHOT_ACTION = 'trlo.groups-snapshot.noprefs'


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

# The browse panel writes its own PID here on startup and removes it on
# exit, so the launcher can tell whether a panel window is already open
# and, if so, ask that instance to raise itself instead of spawning a
# second one. PANEL_FOCUS_REQUEST_PATH's mtime is the signal the running
# panel polls for -- there's no D-Bus server in the panel process, so a
# touched file is the simplest cross-process nudge available.
# Written by stitch_order.py after each change to the stitch order; the panel
# polls it the same way as LAST_ASSIGN_PATH.
ORDER_RECORD_PATH = os.path.join(tempfile.gettempdir(), 'stitch_order_last.json')
# The panel writes a dragged stitch order here, then runs Stitch Order's
# hidden Reorder action, which reads it (same file name as stitch_order.py).
ORDER_PENDING_PATH = os.path.join(tempfile.gettempdir(), 'stitch_order_pending.json')
# Same handoff for group changes made in the panel (new group lists per
# object, and the group order), read by group_apply.py.
GROUP_APPLY_PENDING_PATH = os.path.join(tempfile.gettempdir(), 'expanded_groups_pending_apply.json')
# group_apply.py writes the document's group styles here after it changes
# one, and the window polls it to know which groups have a style.
STYLES_RECORD_PATH = os.path.join(tempfile.gettempdir(), 'expanded_groups_styles.json')

PANEL_PID_PATH = os.path.join(tempfile.gettempdir(), 'expanded_groups_panel.pid')
PANEL_FOCUS_REQUEST_PATH = os.path.join(tempfile.gettempdir(), 'expanded_groups_panel_focus_request')
