// The guided setup uses the same authenticated APIs as Screens. A queued command or a
// redeemed pairing code is not success: the chosen device must check in with its new role.
export function createSetupGuide({ request, changed = () => {} }) {
  let generation = 0;
  let checking = null;
  const state = {};
  function start(kind = "") {
    generation++;
    Object.assign(state, { kind, method: "pair", step: 1, fields: { name: "", room: "", code: "", ip: "" },
      busy: false, error: "", checkError: "", job: null, deviceId: "", device: null, appliance: null, assigned: false, ready: false, waiting: [] });
    changed();
  }
  start();
  const route = () => "page/welcome";
  async function connect() {
    if (state.busy || state.step !== 2) return;
    const fields = state.fields;
    const name = fields.name.trim();
    const code = fields.code.replace(/\s/g, "");
    state.error = !name ? "Give this device a name so people can find it."
      : state.kind === "room" && !fields.room ? "Choose the room this computer will run meetings for."
      : state.method === "pair" && !/^\d{6}$/.test(code) ? "Enter the six-digit code shown on the device."
      : state.method === "ip" && !fields.ip.trim() ? "Enter the TV’s IP address from its network settings." : "";
    if (state.error) { changed(); return; }
    const version = generation;
    state.busy = true;
    changed();
    try {
      let result;
      if (state.method === "ip") {
        result = await request("POST", "/api/provision", { ip: fields.ip.trim(), name, route: route() });
        if (version !== generation) return;
        state.job = result;
      } else if (state.method === "install") {
        result = await request("POST", "/api/appliances", { role: "room", room: fields.room, name, show: "" });
        if (version !== generation) return;
        state.appliance = result;
        state.deviceId = result.device_id;
      } else {
        result = await request("POST", "/api/devices/claim", { code, name, room: fields.room, route: route(), use: state.kind === "room" ? "room" : "screen" });
        if (version !== generation) return;
        state.deviceId = result.id;
      }
      state.step = 3;
    } catch (error) {
      if (version === generation) state.error = error.message;
    } finally {
      if (version === generation) { state.busy = false; changed(); }
    }
  }
  async function check() {
    if (checking === generation || state.step !== 3 || state.ready) return;
    checking = generation;
    const version = generation;
    try {
      if (state.job) {
        const jobs = await request("GET", "/api/provision");
        if (version !== generation) return;
        const job = jobs.find((j) => j.id === state.job.id);
        if (!job) throw new Error("The setup job is no longer available. Check Screens before starting another setup.");
        state.job = job;
        if (job.state === "failed") throw new Error(job.error || job.step || "TV setup failed.");
        if (job.state !== "done" || !job.device) return;
        state.deviceId = job.device;
        if (state.fields.room && !state.assigned) {
          await request("PUT", `/api/devices/${encodeURIComponent(job.device)}`, { room: state.fields.room });
          if (version !== generation) return;
          state.assigned = true;
        }
      }
      const devices = await request("GET", "/api/devices");
      if (version !== generation) return;
      state.device = devices.find((d) => d.id === state.deviceId) || null;
      const d = state.device;
      state.ready = Boolean(d?.online && d.connected && (state.kind === "room" ? d.role === "roompc"
        : d.role === "page" && d.arg === "welcome") && (!state.fields.room || d.room === state.fields.room));
      state.checkError = "";
    } catch (error) {
      if (version === generation) state.checkError = error.message;
    } finally {
      if (checking === version) checking = null;
      if (version === generation) changed();
    }
  }
  return { state, start, connect, check };
}
