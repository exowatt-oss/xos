// What every view shares: whether a Xos server is serving this page, the rooms and sites it
// knows (Admin → Rooms), and a way to ask the current view to re-render.

const listeners = new Set();
let hub = false;
let config = { rooms: [], sites: [] };

export const isHub = () => hub;
export const rooms = () => config.rooms;
export const sites = () => config.sites;
export const room = (id) => config.rooms.find((r) => r.id === id);
export const site = (id) => config.sites.find((s) => s.id === id);

// On a kiosk screen: this device's record from its last heartbeat (name, share link, AirPlay).
let me = null;
export const self = () => me;
export function setMe(record) {
  const changed = !me || me.share_code !== record.share_code || me.share_off !== record.share_off || me.airplay !== record.airplay;
  me = record;
  if (changed) poke();
}

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// Re-render without changing anything (e.g. after fetching data a view shows).
export const poke = () => listeners.forEach((fn) => fn());

export async function loadConfig() {
  try {
    const res = await fetch("/api/config", { cache: "no-store" });
    if (res.ok) {
      const body = await res.json();
      config = { rooms: body.rooms || [], sites: body.sites || [] };
      poke();
    }
  } catch (_) { /* keep the last config */ }
}

async function connectHub() {
  try {
    const health = await fetch("/api/health", { cache: "no-store" });
    if (!health.ok) return;
    hub = true;
    await loadConfig();
  } catch (_) { /* opened as a static file: no server */ }
}
connectHub();
// Rooms renamed or added in Admin reach screens left on for weeks.
setInterval(() => { if (hub) loadConfig(); }, 5 * 60 * 1000);
