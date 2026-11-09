// Keep capture requests bounded even when a TV is slow or has stopped uploading.
export const canCapture = (d) => Boolean(d.online && d.connected && d.client?.startsWith("firetv") && d.native?.displayOn !== false);

export function createDashboardCapture({ request, changed, now = Date.now }) {
  const attempts = new Map();
  return {
    error(id) { return attempts.get(id)?.error || ""; },
    async tick(devices, active) {
      if (!active) return;
      const ids = new Set(devices.map((d) => d.id));
      for (const id of attempts.keys()) if (!ids.has(id)) attempts.delete(id);
      await Promise.all(devices.filter(canCapture).map(async (d) => {
        const previous = attempts.get(d.id);
        if (previous?.pending || (previous && now() - previous.at < 10000)) return;
        // A command response only means queued. Wait for the image to arrive, with a
        // deadline so a failed uploader cannot stop future captures forever.
        if (previous?.waiting && now() - previous.at < 30000 &&
            !(d.screenshot?.at > previous.snapshotAt)) return;
        const attempt = { at: now(), snapshotAt: d.screenshot?.at || 0, pending: true, waiting: false, error: "" };
        attempts.set(d.id, attempt);
        try {
          const res = await request(d.id);
          if (!res.ok) throw new Error("Capture request failed");
          attempt.waiting = true;
        } catch (_) {
          attempt.error = "Couldn't request a snapshot. Retrying…";
        } finally {
          attempt.pending = false;
          changed();
        }
      }));
    },
  };
}
