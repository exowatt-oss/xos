// Tiny rendering helpers. Views return HTML strings; events are delegated via data-act attributes.

export const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export const time = (t) => new Date(t).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const clockText = () => new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
// Live elements: main.js refreshes their text every second without re-rendering the view.
export const clock = () => `<span data-clock>${clockText()}</span>`;
export const clockStr = clockText;
export const since = (ts) => `<span data-since="${ts}">${dur(Date.now() - ts)}</span>`;
export const dateLong = () => new Date().toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
export const dayShort = (t) => new Date(t).toLocaleDateString([], { month: "short", day: "numeric" });

export function dur(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}

// A booking's time in words, for the room remote, the room TV and Today. Same-day bookings are
// "2:00 PM – 3:00 PM" (with "Tomorrow" or the date in front when they aren't today); a booking
// that runs across days says when it ends ("Until Fri, Nov 6 · 11:00 PM") once it has started.
const dayWord = (s, now) => {
  const day = (t) => new Date(t * 1000).setHours(0, 0, 0, 0);
  const n = Math.round((day(s) - day(now)) / 86400000);
  return n === 0 ? "" : n === 1 ? "Tomorrow" : new Date(s * 1000).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
};
const hm = (s) => new Date(s * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
export function bookingWhen(b, now = Date.now() / 1000) {
  const startDay = dayWord(b.start, now), endDay = dayWord(b.end, now);
  if (startDay === endDay) return `${startDay ? `${startDay} · ` : ""}${hm(b.start)} – ${hm(b.end)}`;
  if (b.start <= now) return `Until ${endDay ? `${endDay} · ` : ""}${hm(b.end)}`;
  return `${startDay ? `${startDay} · ` : ""}${hm(b.start)} – ${endDay ? `${endDay} · ` : ""}${hm(b.end)}`;
}
/** "until 3:00 PM", or "until Fri, Nov 6 · 11:00 PM" when it ends on another day. */
export const untilWhen = (end, now = Date.now() / 1000) => {
  const d = dayWord(end, now);
  return `until ${d ? `${d} · ` : ""}${hm(end)}`;
};
