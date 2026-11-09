// Rooms are the control groups. Each device in a room is one of these kinds:
//   screen      shows things on the wall (room TV, page, photo, signage, dashboard); remotes drive it
//   controller  a remote: an iPad on the table (#/tablet/<room>) or a phone/browser (#/remote/<screen>)
//   panel       the door panel outside the room; it shows the room's status and isn't driven
//   camera      a live camera or feed
//   computer    the room's meeting computer (a Mac running os/mac/xos-room.py): the iPad joins meetings on it
// A remote only ever sees and controls the screens of its own room.

const KINDS = { tablet: "controller", remote: "controller", panel: "panel", camera: "camera", roompc: "computer" };

export const kindOf = (d) => KINDS[d.role || ""] || "screen";

export const KIND_LABELS = {
  screen: "Screens",
  controller: "Remotes and controllers",
  panel: "Door panels",
  camera: "Cameras",
  computer: "Room computers",
};

export const slug = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

/** The screen a #/remote/<key> link points at: a device id, or its name as a slug. */
export const findScreen = (devices, key) =>
  (devices || []).find((d) => d.id === key || (key && (slug(d.name) === key || slug(d.label) === key))) || null;

/**
 * The room (control group) a device belongs to. Set in Admin, else read from what it runs:
 * #/tv/<room>, #/tablet/<room> and #/panel/<room> name the room; #/remote/<screen> belongs to its
 * screen's room. Last, a screen called "Atrium room TV" is matched to the Atrium room.
 */
export function roomOf(d, devices, rooms) {
  if (!d) return "";
  if (d.room) return d.room;
  const known = (id) => rooms.some((r) => r.id === id);
  if (["tv", "tablet", "panel", "roompc"].includes(d.role) && known(d.arg)) return d.arg;
  if (d.role === "remote") {
    const screen = findScreen(devices, d.arg);
    if (screen && screen !== d && screen.role !== "remote") return roomOf(screen, devices, rooms);
    if (!screen) { const named = findRoom(rooms, d.arg); if (named) return named.id; }
  }
  const n = slug(d.name);
  return rooms.find((r) => n && (n === slug(r.name) || n.startsWith(`${slug(r.name)}-`)))?.id || "";
}

/** The room a #/remote/<key> link names when no screen does: "atrium", or "atrium-room" for Atrium. */
export const findRoom = (rooms, key) =>
  (rooms || []).find((r) => key && (r.id === key || slug(r.name) === key || key.startsWith(`${slug(r.name)}-`))) || null;

/** A room's devices of one kind, online first, then by name. */
// A room with more than one meeting computer (a Mac kept in the room for testing beside its Xos
// box): the remote drives the one that works, and the dedicated Xos box before a Mac.
const computerRank = (d) => (d.native?.meeting?.error ? 2 : 0) + (/^xosroom-linux/.test(d.client || "") ? 0 : 1);

export function members(devices, rooms, roomId, kind) {
  return (devices || [])
    .filter((d) => kindOf(d) === kind && roomOf(d, devices, rooms) === roomId)
    .sort((a, b) => (b.online - a.online) || (kind === "computer" && computerRank(a) - computerRank(b))
      || String(a.name || a.id).localeCompare(String(b.name || b.id)));
}

// A Linux room computer also drives its attached TV.
export function displayMembers(devices, rooms, roomId) {
  const screens = members(devices, rooms, roomId, "screen");
  const pc = members(devices, rooms, roomId, "computer")
    .find(d => d.online && /^xosroom-linux/.test(d.client || ""));
  return pc && !screens.some(d => d.online) ? [pc, ...screens] : screens;
}
export function selectedDisplay(screens, saved, anchor) {
  const chosen = screens.find(d => d.id === saved);
  return (chosen?.online ? chosen : null) || (anchor?.online ? anchor : null)
    || screens.find(d => d.online) || chosen || anchor || screens[0];
}
