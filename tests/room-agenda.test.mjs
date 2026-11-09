import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import {readCalendar} from "../app/js/calendar-read.js";
// The booking-time helpers the views import from ui.js, as plain declarations for these sandboxes.
const bookingHelpers = readFileSync(new URL("../app/js/ui.js", import.meta.url), "utf8")
  .split("// A booking's time in words")[1].replace(/^.*\n/, "").replace(/export /g, "");
const source = bookingHelpers + readFileSync(new URL("../app/js/views/screens.js", import.meta.url), "utf8")
  .replace(/^import .*;$/gm, "").replace(/export function /g, "function ");
const esc = s => String(s).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;");
function render(calendar) {
  return runInNewContext(source + '\ncalendars.atrium={at:Date.now(),data:calendar};renderRoomTV("atrium");', {
    calendar, esc, renderPage:id=>"PAGE "+id, setInterval() {}, clock:()=>"10:00 AM", dateLong:()=>"Wednesday", S:{room:()=>({name:"Atrium",site:"hq"}),site:()=>({name:"HQ"}),self:()=>null},
  });
}
test("TV shows upcoming titles in time order without links or meeting QR", () => {
  const now = Date.now()/1000;
  const html = render({configured:true,calendar:"room",bookings:[
    {title:"Second",start:now+7200,end:now+8000,join_url:"https://zoom.us/j/123456789?pwd=secret"},
    {title:"First <script>",start:now+3600,end:now+4500},
    {title:"Expired",start:1,end:2},
  ]});
  assert.match(html,/Upcoming meetings/);
  assert.ok(html.indexOf("First &lt;script>") < html.indexOf("Second"));
  assert.doesNotMatch(html,/Expired|zoom.us|pwd=|qr.svg|<script>/);
});
test("TV distinguishes calendar setup, outages and a genuinely empty schedule", () => {
  assert.match(render({configured:false,calendar:"room"}),/calendar not connected/);
  assert.match(render({configured:true,calendar:"room",error:"provider error"}),/temporarily unavailable/);
  assert.match(render({configured:true,calendar:"room",bookings:[]}),/No upcoming bookings/);
});
test("TV labels next-day bookings and retains current meeting status", () => {
  const now=Date.now()/1000;
  const tomorrow = new Date(); tomorrow.setDate(tomorrow.getDate()+1); tomorrow.setHours(9,0,0,0);
  const html=render({configured:true,calendar:"room",bookings:[
    {title:"Current",start:now-60,end:now+60},
    {title:"Tomorrow meeting",start:tomorrow.getTime()/1000,end:tomorrow.getTime()/1000+1800},
  ]});
  assert.match(html,/In use until/);
  assert.match(html,/Current/);
  assert.match(html,/Tomorrow ·/);
});


test("calendar completion paints immediately and a minute tick reads again", async () => {
  let tick, reads=0, paints=0;
  const context = { readCalendar, esc, location:{hash:"#/tv/atrium"}, setInterval(fn) { tick=fn; },
    S:{poke(){paints++;}}, fetch:async()=>{reads++;return {ok:true,json:async()=>({configured:true,calendar:"room",bookings:[]})};},
  };
  const run = runInNewContext(source + `
    (async()=>{roomCalendar("atrium");await new Promise(r=>setTimeout(r,0));return calendars.atrium;})()
  `, {...context,setTimeout});
  await run;
  assert.equal(reads,1); assert.equal(paints,1);
  tick(); await new Promise(r=>setTimeout(r,0));
  assert.equal(reads,2); assert.equal(paints,2);
  context.location.hash="#/tablet/atrium";tick();
  assert.equal(reads,2);
});

test("room TV returns to content outside the lead window and ignores the just-ended booking", () => {
  const now=Date.now()/1000,display={idle_page:"atrium-room",meeting_lead_minutes:15};
  const base={configured:true,calendar:"room",display,bookings:[]};
  assert.match(render(base),/PAGE atrium-room/);
  const next={title:"Next",start:now+901,end:now+3600};
  assert.match(render({...base,bookings:[next]}),/PAGE atrium-room/);
  assert.doesNotMatch(render({...base,bookings:[{...next,start:now+899}]}),/PAGE atrium-room/);
  const ongoing={title:"Ended early",start:now-600,end:now+1800};
  const ended={...display,last_meeting_end:now-10};
  assert.match(render({...base,display:ended,bookings:[ongoing]}),/PAGE atrium-room/);
  assert.doesNotMatch(render({...base,display:ended,bookings:[ongoing,{...next,start:now+600}]}),/PAGE atrium-room/);
  assert.doesNotMatch(render({...base,error:"Calendar unavailable"}),/PAGE atrium-room/);
  assert.doesNotMatch(render({...base,display:{...display,idle_page:""}}),/PAGE atrium-room/);
  assert.doesNotMatch(render({...base,display:{...display,meeting_lead_minutes:20},bookings:[{...next,start:now+1100}]}),/PAGE atrium-room/);
});
