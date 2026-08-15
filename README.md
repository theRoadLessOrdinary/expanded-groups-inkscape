# Expanded Groups

An Inkscape extension for tagging scattered, non-adjacent objects with one
or more shared virtual group names — without moving them in the document
or using Inkscape's native `<g>` grouping.

## Why

Say you have several identical stitched pieces, each made of four colors.
For stitching purposes, every "blue" part across all the pieces needs to be
handled together (native-grouped, recolored, etc.) even though each one
lives inside a different native group, in a different part of the
document. Expanded Groups lets you select all the blue pieces at once,
give them a shared name ("All Blues"), and later re-select that exact set
with one click — as many times as you like, without re-hunting for them.

An object can belong to more than one group at once.

## How it works

Membership is stored as a `trlo:group` attribute (a `|`-separated list of
names) directly on each tagged object — nothing about the object's
position, parents, or native grouping changes.

The extension adds two menu entries under **Extensions → Expanded Groups**:

- **Assign To Group...** — select objects on the canvas, run this, type a
  name. Adds that name to whatever groups those objects already belong to.
- **Browse Groups...** — opens a non-blocking panel listing every group
  name found in the document with its member count. From there:
  - **Apply (Select)** — selects every member of the chosen group on the
    live canvas.
  - **Rename** — renames that one membership across all its members
    (other group memberships those objects have are left alone).
  - **Remove** — strips that one membership from all its members.

## Architecture notes

- **Assign To Group...** is a normal (briefly blocking) Inkscape effect
  script, because it's the only operation that needs to know the live
  canvas selection and each selected object's *current* group memberships
  at the moment it runs — and Inkscape has no way to query either of those
  from outside a normal effect invocation.
- **Browse Groups...** is a *launcher* that reads the document, snapshots
  the current `trlo:group` state to a scratch JSON file, and spawns the
  actual panel as a fully detached process (a real double-fork, not
  `subprocess.Popen`) before returning immediately. This matters because
  Inkscape runs effect extensions synchronously and doesn't service its
  own event loop while waiting on the child process — a naive
  long-running interactive dialog launched the normal way blocks the
  entire Inkscape window for as long as it stays open. Detaching sidesteps
  that entirely.
- The panel never touches the SVG file. Every live mutation (Apply/Rename/
  Remove) goes out through Inkscape's own D-Bus Actions interface
  (`select-by-id`, `object-set-attribute` — the same actions Inkscape's
  own menus call), so edits land in Inkscape's in-memory document and stay
  correctly tracked by undo/unsaved-changes, instead of writing the file
  directly and risking a conflict with whatever unsaved state Inkscape is
  holding.
- Two modules, deliberately split: `expanded_groups_data.py` (pure
  document logic, no GTK import) and `expanded_groups_dbus.py` (the D-Bus
  helpers, needs GTK). The launcher only imports the former. Importing
  `gi` unconditionally would leak a `PyGIDeprecationWarning` onto the
  launcher's stderr, which Inkscape treats as reason to show its own
  "script produced additional output" dialog — and dismissing that dialog
  was found to reset the canvas selection Apply had just set.
- Verified to work on both the Flatpak and a native (apt) Inkscape
  install — the D-Bus mechanism isn't Flatpak-specific, it's inherent to
  Inkscape's own GTK/GApplication architecture (confirmed on Inkscape
  1.2.2 through 1.4.4).

## Install

Copy this folder into your Inkscape user extensions directory, e.g.:

- Flatpak: `~/.var/app/org.inkscape.Inkscape/config/inkscape/extensions/`
- Native (Linux): `~/.config/inkscape/extensions/`

Restart Inkscape. Requires Python GTK bindings (`python3-gi`, GTK 3) to be
available to whichever Python interpreter Inkscape uses to run extensions.

## License

GPL-3.0-or-later, matching Inkscape's own extension license.
