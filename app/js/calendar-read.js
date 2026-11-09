// A stalled request or response body must not block all later room-calendar refreshes.
export async function readCalendar(url, primary, fallback = null, timeoutMs = 15000) {
  const controller = new AbortController();
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new Error("Calendar request timed out"));
    }, timeoutMs);
  });
  const read = async () => {
    const options = { cache: "no-store", signal: controller.signal };
    let response;
    try { response = await primary(url, options); }
    catch (error) { if (!fallback || controller.signal.aborted) throw error; }
    if (!response?.ok && fallback && !controller.signal.aborted) response = await fallback(url, options);
    if (!response?.ok) throw new Error("Calendar unavailable");
    return response.json();
  };
  try { return await Promise.race([read(), deadline]); }
  finally { clearTimeout(timer); }
}
