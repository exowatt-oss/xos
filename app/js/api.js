// Where requests go. Screens and the room iPad call /api/… directly (no one signs in on a TV).
// People call /u/api/…: the same API, reached through the platform's Google sign-in, so the
// server knows who is asking (src/app/access.py). A trusted remote instead sends the device key
// it was given once in a heartbeat reply.

const KEY = "xos-device-key";

/** {id, key} for this kiosk, if the server trusted it as a remote; else null. */
export function deviceKey() {
  try { return JSON.parse(localStorage.getItem(KEY) || "null"); } catch (_) { return null; }
}
export function saveDeviceKey(id, key) {
  try { localStorage.setItem(KEY, JSON.stringify({ id, key })); } catch (_) { /* private mode */ }
}

// Save before sending the first enrollment heartbeat: a lost reply cannot lose the key.
let storageError = "";
export function prepareDeviceKey(id) {
  try {
    const existing = deviceKey();
    if (existing?.id === id && existing?.key) return existing.key;
    const key = Array.from(crypto.getRandomValues(new Uint8Array(24)), (b) => b.toString(16).padStart(2, "0")).join("");
    saveDeviceKey(id, key);
    if (deviceKey()?.key !== key) throw new Error("storage unavailable");
    storageError = "";
    return key;
  } catch (_) {
    storageError = "Allow website storage on this tablet, then reload Xos.";
    return "";
  }
}

const remoteError = () => new Error(storageError || "This room remote needs access restored. In Xos Admin → Screens, select this tablet and choose Reset remote access.");
const isRoomKiosk = () => new URLSearchParams(location.search).has("kiosk") && /^#\/(tablet|remote)\//.test(location.hash);

export const signInUrl = (next = location.pathname + location.hash) => `/u/signin?next=${encodeURIComponent(next)}`;

export class SignedOut extends Error {
  constructor() { super("You're signed out. Sign in again."); this.signedOut = true; }
}

/**
 * fetch() for a person: /api/x goes to /u/api/x. Signed out, the sign-in gate answers with a
 * redirect to Google, which a fetch can't follow; that surfaces as SignedOut.
 */
export async function userFetch(url, opts = {}) {
  const path = typeof url === "string" && url.startsWith("/api/") ? `/u${url}` : url;
  const res = await fetch(path, { ...opts, redirect: "manual", credentials: "same-origin" });
  if (res.type === "opaqueredirect" || res.status === 0) throw new SignedOut();
  return res;
}

/** fetch() for a remote: with the device key when this kiosk has one, else as the signed-in person. */
export async function controlFetch(url, opts = {}) {
  const dk = deviceKey();
  if (dk?.key) {
    const res = await fetch(url, { ...opts, headers: { ...(opts.headers || {}), "X-Xos-Device": dk.id, "X-Xos-Key": dk.key } });
    if (res.status === 401) throw remoteError();
    return res;
  }
  if (isRoomKiosk()) throw remoteError();
  return userFetch(url, opts);
}

let meAsk = null;
let meAt = 0;
/** Who is signed in: {email, name, role, role_name, perms, preview}, {email: ""} when signed out. Cached for a minute. */
export function whoAmI(fresh = false) {
  if (!meAsk || fresh || Date.now() - meAt > 60000) {
    meAt = Date.now();
    meAsk = userFetch("/api/me", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : { email: "" }))
      .catch(() => ({ email: "" }));
  }
  return meAsk;
}
