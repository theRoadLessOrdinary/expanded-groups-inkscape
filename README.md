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

The eye and lock icons also appear on each object row. Deleting a group or
removing an object only removes the tag; it never deletes the object.

There is no drag-and-drop and no nested groups. Groups are flat by design.

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
- **The panel never writes the SVG file.** Every live change (select,
  rename, remove, hide, lock) goes through Inkscape's own D-Bus Actions
  interface (`select-by-id`, `object-set-attribute`, `object-set-property`,
  `selection-lock`, and so on), the same actions Inkscape's menus call.
  Edits land in Inkscape's in-memory document and are tracked correctly by
  undo and unsaved-changes.
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
| `expanded_groups_data.py` | Document scanning and group-name logic |
| `expanded_groups_dbus.py` | Live D-Bus helpers |

## License

GPL-3.0-or-later, matching Inkscape's own extension license.
