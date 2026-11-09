// The room tablet (an iPad in landscape), tapped like a person, then a screenshot.
//   node tablet.mjs <out.png> [steps]      e.g. "await pg.click('text=Join a meeting')"
// Env: XOS_SERVER (default http://127.0.0.1:8796), ROOM (default atrium), TZ_ID (America/Denver),
// PLAYWRIGHT (path to playwright-core's index.mjs), CHROME (a Chromium binary).
const { chromium } = await import(process.env.PLAYWRIGHT || "playwright-core");
const [out, steps = ""] = process.argv.slice(2);
const server = process.env.XOS_SERVER || "http://127.0.0.1:8796";
const b = await chromium.launch(process.env.CHROME ? { executablePath: process.env.CHROME } : {});
const ctx = await b.newContext({ viewport: { width: 1180, height: 820 }, deviceScaleFactor: 2, hasTouch: true, isMobile: true,
  timezoneId: process.env.TZ_ID || "America/Denver",
  userAgent: "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1" });
const pg = await ctx.newPage();
await pg.goto(`${server}/app/#/remote/${process.env.ROOM || "atrium"}`);
await pg.waitForTimeout(2500);
if (steps) await new Function("pg", `return (async () => { ${steps} })()`)(pg);
await pg.screenshot({ path: out });
console.log((await pg.locator("body").innerText()).replace(/\s+/g, " ").slice(0, 400));
await b.close();
