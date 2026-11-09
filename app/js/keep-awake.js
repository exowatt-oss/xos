// Keep a visible room tablet awake. Browsers release the lock when hidden or on low power;
// retry on return to Xos or the next touch, without a loop that fights an OS refusal.
let status = "inactive";
export const screenWakeState = () => status;

export function keepRoomRemoteAwake() {
  let lock = null;
  let pending = null;
  let stopped = false;
  const wanted = () => !stopped && document.visibilityState === "visible"
    && new URLSearchParams(location.search).has("kiosk") && /^#\/(tablet|remote)\//.test(location.hash);
  const sync = () => {
    if (!wanted()) { status = document.visibilityState === "visible" ? "inactive" : "background"; lock?.release().catch(() => {}); lock = null; return; }
    if (lock || pending) return;
    if (!navigator.wakeLock) { status = "unsupported"; return; }
    status = "requesting";
    pending = navigator.wakeLock.request("screen").then((next) => {
      if (!wanted()) return next.release();
      lock = next;
      status = "held";
      next.addEventListener("release", () => { if (lock === next) { lock = null; status = "released"; } });
    }).catch(() => { status = wanted() ? "denied" : "background"; })
      .finally(() => { pending = null; });
  };
  document.addEventListener("visibilitychange", sync);
  document.addEventListener("pointerdown", sync, { passive: true });
  window.addEventListener("hashchange", sync);
  sync();
  return () => {
    stopped = true;
    sync();
    document.removeEventListener("visibilitychange", sync);
    document.removeEventListener("pointerdown", sync);
    window.removeEventListener("hashchange", sync);
  };
}
