// Switch TV to Xos: Display off, then Display on, so HDMI-CEC switches the TV back to the stick's
// input after someone changed it. The server runs the steps (switch_input in src/app/main.py) and
// says the same as switchInputProblem when a stick can't. No imports, so node tests load it alone.

const MIN = [0, 6, 0];

/** The Xos TV version a stick reports, as [major, minor, patch], or null. */
export function tvAppVersion(d) {
  let raw = String(d?.native?.app || "");
  const client = String(d?.client || "");
  if (!raw && client.startsWith("firetv-")) raw = client.slice("firetv-".length);
  const m = raw.match(/^(\d+)\.(\d+)(?:\.(\d+))?/);
  return m ? [Number(m[1]), Number(m[2]), Number(m[3] || 0)] : null;
}

function older(a, b) {
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] < b[i];
  return false;
}

/** Why Switch TV to Xos can't run on this screen, or "" when it can. */
export function switchInputProblem(d) {
  if (!String(d?.client || "").startsWith("firetv") || !d?.native) return "Switch TV to Xos needs the Xos TV app on a Fire TV stick.";
  const v = tvAppVersion(d);
  if (!v || older(v, MIN)) return "Switch TV to Xos needs Xos TV 0.6.0 or later. Update the stick with Admin → Add a screen → by IP.";
  if (!d.native.displayControl) return "Switch TV to Xos needs display control, which is off on this stick. Run firetv/provision.sh to turn it on.";
  return "";
}

export const SWITCH_LABEL = "Switch TV to Xos";
export const SWITCH_HELP = "Turns the TV off and on again so it switches back to Xos, when someone changed its input.";
// The server waits for Display off, the TV going to standby and Display on: about 10 s, up to a minute.
export const SWITCH_WAIT_MS = 75000;
