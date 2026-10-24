# Expanded Groups

An Inkscape extension for tagging scattered, non-adjacent objects with one
or more shared virtual group names, without moving them in the document
or using Inkscape's native `<g>` grouping.

**Built for [Ink/Stitch](https://inkstitch.org/) users.** Machine embroidery
designs are often built from many small objects (satin columns, fills,
running stitches) spread across nested native groups, and the objects that
need to be handled together (same thread color, same stitch order, same
parameters) are rarely next to each other in the document. Other Inkscape
users with the same "select these scattered things again" problem may find
it useful too; nothing in it is specific to embroidery.

## Why

Say you have several identical stitched pieces, each made of four colors.
For stitching purposes, every "blue" part across all the pieces needs to be
handled together (set to one thread color, reordered, given the same
parameters) even though each one lives inside a different native group, in
a different part of the document. Expanded Groups lets you select all the
blue pieces at once, give them a shared name ("All Blues"), and later
re-select that exact set with one click, as many times as you like,
without re-hunting for them.

Because the objects never move or get re-parented, Ink/Stitch's stitch
order, which depends on document order, is not disturbed by tagging.

An object can belong to more than one group at once.

## Using it

Two menu entries appear under **Extensions > Expanded Groups**:

- **Assign To Group...** Select objects on the canvas, run this, and type
  a name. That name is added to whatever groups those objects already
  belong to.
- **Browse Groups...** Opens a non-blocking panel styled after Inkscape's
  own Layers dialog. It stays open while you keep working.

### The Browse panel

Every group name in the document is a top-level row. Expand a group to
see its member objects as child rows.

| Action | How |
| --- | --- |
| Select all members of a group on the canvas | Click the group row |
| Select one member | Click the object row |
| Rename a group | Double-click it, or use the toolbar button or right-click menu |
| Delete a group (removes the tag from all members) | Select it, then Delete key, toolbar button, or right-click menu. Asks for confirmation. |
| Remove one object from a group | Select the object row, then Delete, or right-click it and choose "Remove from ..." |
| Add the current canvas selection to a group | Right-click the group and choose "Add Selection to ..." |
| Create a new group from the canvas selection | Toolbar "Assign To Group..." button (same as the menu item) |
| Hide or show a whole group | Eye icon on the group row |
| Lock or unlock a whole group | Lock icon on the group row |
| Change the sewing order | Drag rows up or down inside Stitch Order |
| Change the order groups are listed in | Drag a group row up or down |
| Move an object to another group | Drag its row onto the other group, or onto one of that group's objects |
| Add an object to another group and keep it in its own | Hold Ctrl while you drop it |
| Select several rows | Ctrl+click adds or removes one row; Shift+click selects every row from the last one clicked |
| Act on several rows | Right-click or press Delete with them selected; the menu and Delete apply to all of them |
| Drag several rows at once | Select rows of the same kind, then drag one of them |

The eye and lock icons also appear on each object row. Deleting a group or
removing an object only removes the tag; it never deletes the object.

There are no nested groups. Groups are flat by design.

The group order is saved in the document, on the root `<svg>` element as
`trlo:group_order`. Groups that were never dragged are listed after the
others, alphabetically.

### Group styles

A group can carry a fill and stroke. Style one object the way you want it
(with Inkscape's Fill and Stroke dialog), select it on the canvas, then
right-click the group and choose **Set Style from Selection**. Every object
in the group takes that fill and stroke, and so does any object added to the
group later (Assign To Group, Add Selection, or a drag). On a native group,
the style goes on every shape inside it.

- An object in more than one styled group has the style of the group it
  most recently got a style from.
- Taking an object out of a group leaves its style as it is.
- **Reapply Style** puts the group's style back on all its objects.
- **Clear Style** forgets the group's style; the objects keep theirs.
- Renaming a group keeps its style; deleting a group drops it.

The styles are saved in the document, on the root `<svg>`, as
`trlo:group_styles`.

### In the Map's Edge Tool Panel

The same list also sits in the Map's Edge Tool Panel, under **General > Groups**, so it can stay in one window with the other tools. It works the same way and adds a Refresh button, which reads the groups from the document again (through the hidden `groups_snapshot` extension). The menu's Browse Groups... still opens the list in a window of its own.

## How it works

Membership is stored as a `trlo:group` attribute (a `|`-separated list of
names) directly on each tagged object. It is part of the document itself,
so it is saved with the file and travels with it. Nothing about an
object's position, parents, or native grouping changes.

The attribute uses the namespace URI
`http://theroadlessordinary.com/namespace`. When a document is saved without
that namespace declared on its root, Inkscape may write the prefix as
`ns0:` instead of `trlo:`. That is cosmetic; the code matches by namespace
URI, not by prefix.

## Architecture notes

- **Assign To Group...** is a normal, briefly blocking Inkscape effect
  script. It is the only operation that needs the live canvas selection
  and each selected object's current memberships at the moment it runs,
  and Inkscape offers no way to query either from outside an effect.
- **Browse Groups...** is a launcher. It reads the document, snapshots the
  current group state to a scratch JSON file, and spawns the real panel as
  a fully detached process (a true double-fork with `setsid()`, not
  `subprocess.Popen`), then returns immediately. Inkscape runs effect
  extensions synchronously and does not service its own event loop while
  waiting for the child, so a long-lived dialog launched the normal way
  would freeze the whole Inkscape window.
- **The panel never writes the SVG file.** Every live change goes through
  Inkscape's own D-Bus Actions interface, the same actions Inkscape's menus
  call. Select, hide and lock use `select-by-id`, `object-set-property` and
  `selection-lock`. Rename, delete, remove, moving objects between groups
  and the group order run the hidden `group_apply.py` extension, which the
  panel tells what to change through a small JSON file. Changes to the
  stitch order run the Stitch Order extension's own actions (its hidden
  Reorder action for a drag). Edits land in Inkscape's in-memory document
  and are tracked correctly by undo and unsaved-changes; each change is one
  undo step.
- **Why not `object-set-attribute` for group names.** That action takes the
  attribute name literally. Given `{namespace}group` it saves a broken
  attribute, and a prefixed name like `trlo:group` only works when
  Inkscape knows that prefix for the document (many files use `ns0:`).
  An extension writes the attribute by namespace, whatever the prefix.
- **Any installed extension is reachable over that same D-Bus interface**
  as an action named after its `.inx` id, with dots and underscores turned
  into hyphens (`trlo.assign_group` becomes `trlo.assign-group`). The
  panel's Assign button uses this to run Assign To Group in the live
  window.
- **Two modules, deliberately split.** `expanded_groups_data.py` (document
  logic, no GTK import) and `expanded_groups_dbus.py` (D-Bus helpers,
  needs GTK). The launcher imports only the former. Importing `gi` emits a
  `PyGIDeprecationWarning` on stderr, and Inkscape treats any stderr from
  an effect as a reason to show its "script produced additional output"
  dialog. Dismissing that dialog was found to reset the canvas selection
  the panel had just made.
- **GTK 3, not Qt.** Inkscape is itself a GTK3 app, so its bundled Python
  already has GTK and the panel runs as an ordinary subprocess. Qt would
  not be available inside the Flatpak sandbox.
- **The panel's list can drift** if something outside it changes the same
  objects' group attribute while it is open, because there is no D-Bus
  call to read attribute values back. Close and reopen Browse Groups to
  re-scan the document. Changes made through Assign To Group are picked up
  automatically.
- Verified on both the Flatpak and a native (apt) Inkscape install,
  Inkscape 1.2.2 through 1.4.4. The D-Bus mechanism is inherent to
  Inkscape's GApplication design, not Flatpak-specific.

## Install

Copy this folder into your Inkscape user extensions directory:

- Flatpak: `~/.var/app/org.inkscape.Inkscape/config/inkscape/extensions/`
- Native (Linux): `~/.config/inkscape/extensions/`

Restart Inkscape. Requires the Python GTK bindings (`python3-gi`, GTK 3)
for whichever Python interpreter Inkscape uses to run extensions. The
Flatpak build already includes them.

Linux only. It relies on D-Bus, which Inkscape on Windows and macOS does
not expose in the same way.

## Files

| File | Purpose |
| --- | --- |
| `assign_group.inx` / `assign_group.py` | Assign To Group extension |
| `expanded_groups.inx` / `expanded_groups.py` | Browse Groups launcher |
| `expanded_groups_panel.py` | The detached GTK tree panel |
| `group_apply.inx` / `group_apply.py` | Hidden extension the panel runs to apply group changes |
| `expanded_groups_data.py` | Document scanning and group-name logic |
| `expanded_groups_dbus.py` | Live D-Bus helpers |

## License

GPL-3.0-or-later, matching Inkscape's own extension license.
