import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
const source = readFileSync(new URL("../os/mac/xos-room.py", import.meta.url), "utf8");
const stateJS = source.match(/STATE_JS = r"""([\s\S]*?)"""/)[1];
function state(text, labels, hostname = "meet.google.com") {
  return JSON.parse(runInNewContext(stateJS, {
    document: { body: { innerText: text }, title: "Meet - test", querySelectorAll: () => labels.map(label => typeof label === "string" ? ({ getAttribute: () => label }) : ({getAttribute: attr => attr === "title" ? label.title : label.aria, innerText:label.text})) },
    location: { hostname, pathname: "/abc-defg-hij" },
  }));
}
test("a waiting room with a Leave call button is not an active meeting", () => {
  const s = state("Please wait until a meeting host brings you into the call", ["Leave call"]);
  assert.equal(s.waiting, true);
  assert.equal(s.in_call, false);
});
test("admission switches to an active meeting and reads the controls", () => {
  const s = state("People", ["Leave call", "Turn on microphone", "Turn off camera"]);
  assert.equal(s.waiting, false);
  assert.equal(s.in_call, true);
  assert.equal(s.muted, true);
  assert.equal(s.camera_off, false);
});

// The booking-time helpers the views import from ui.js, as plain declarations for these sandboxes.
const bookingHelpers = readFileSync(new URL("../app/js/ui.js", import.meta.url), "utf8")
  .split("// A booking's time in words")[1].replace(/^.*\n/, "").replace(/export /g, "");
const remoteSource = bookingHelpers + readFileSync(new URL("../app/js/views/remote.js", import.meta.url), "utf8")
  .replace(/^import .*;$/gm, "").replace(/export function /g, "function ");
function renderRemote(meeting, expression = 'homeView({roomId:"atrium", roomName:"Atrium", d:null, group:"atrium"})') {
  return runInNewContext(remoteSource + `
    devices = [{id:"mac-test", role:"roompc", online:true, native:{meeting,volume:31,meeting_providers:["meet","zoom"]}}];
    zoomInfo.atrium = {};
    ${expression};`, {
    meeting, S: { rooms: () => [], poke() {} }, members: devices => devices,
    window: { addEventListener() {} }, document: { addEventListener() {} }, setInterval() {},
    esc: value => String(value), clock: () => "10:00 AM",
  });
}
test("unknown Meet media state displays unavailable controls and recovery instructions", () => {
  const html = renderRemote({in_call:true, muted:null, camera_off:null});
  assert.match(html, /Microphone unavailable/);
  assert.match(html, /Camera unavailable/);
  assert.match(html, /access in the room browser/);
  assert.match(html, /data-zoom="mute"[^>]*disabled/);
  assert.match(html, /data-zoom="camera_off"[^>]*disabled/);
});
test("available media controls preserve the reported mute and camera states", () => {
  const html = renderRemote({in_call:true, muted:true, camera_off:false});
  assert.match(html, /data-zoom="unmute"/);
  assert.match(html, /data-label="Camera off"/);
  assert.doesNotMatch(html, /unavailable|access in the room browser/);
});

test("Google's video-call rejection is reported as denied", () => {
  assert.equal(state("You can't join this video call", []).denied, true);
});

// Wording seen on the real services (2026-10), against made-up links and Zoom's public test meeting.
test("real Webex pages for an invitation that isn't a meeting are reported as missing", () => {
  assert.equal(state("English Download Sign in Join a meeting\nThis meeting has ended or been canceled.\nContact the host for details.", ["Join a meeting"], "meetingsamer.webex.com").missing, true);
  assert.equal(state("This meeting link isn't valid.\nPlease double-check the link or contact the host for help.", ["Join a meeting"], "meetingsamer.webex.com").missing, true);
});
test("Teams' light meetings that can't connect the room is reported as denied", () => {
  const s = state("Sorry, we couldn't connect you.", ["Rejoin call", "Dismiss"], "teams.microsoft.com");
  assert.equal(s.denied, true);
  assert.equal(s.in_call, false);
});
test("Zoom's bot check is human verification, not a passcode or waiting room", () => {
  const s = state("Enter Meeting Info\nAutomated bots aren't allowed to join this meeting.\nIf this was a mistake and you are a human, sign in to join the meeting. Or report a problem to us.\nYour Name", ["Mute", "Stop Video", "Sign in to join", "Join from Zoom Workplace App"], "app.zoom.us");
  assert.equal(s.verification, true);
  assert.equal(s.needs_passcode, false);
  assert.equal(s.waiting, false);
  assert.equal(s.in_call, false);
});

test("Meet join dialog puts code/link entry first and hides optional Zoom fields", () => {
  const html = renderRemote({}, 'roomPc.atrium = "mac-test"; joinSheet("atrium")');
  assert.match(html, /label[^>]*for="rm-join"/);
  assert.match(html, /Meeting code or link/);
  assert.match(html, /aria-expanded="false"/);
  assert.match(html, /id="rm-zoom-options"[^>]*hidden/);
  assert.match(html, /aria-label="Zoom passcode"/);
  const expanded = renderRemote({}, 'roomPc.atrium = "mac-test"; handleRemote("rm-join-options", {}); joinSheet("atrium")');
  assert.match(expanded, /aria-expanded="true"/);
  assert.doesNotMatch(expanded, /id="rm-zoom-options"[^>]*hidden/);
});
test("pending meeting and volume actions disable repeated taps until settled", () => {
  const html = renderRemote({in_call:true, muted:false, camera_off:true}, 'pending.set("zoom:mute", "Muting"); pending.set("zoom:volume", "Volume"); homeView({roomId:"atrium", roomName:"Atrium", d:null, group:"atrium"})');
  assert.match(html, /data-zoom="mute"[^>]*disabled/);
  assert.match(html, /aria-label="Quieter"[^>]*disabled/);
  assert.match(html, /aria-label="Louder"[^>]*disabled/);
});

test("Zoom labels report actual audio, video and sharing state", () => {
  const s = state("Meeting", ["Leave", "Unmute", "Stop Video", "Stop Share"]);
  assert.equal(s.in_call, true);
  assert.equal(s.muted, true);
  assert.equal(s.camera_off, false);
  assert.equal(s.sharing, true);
});
test("Teams lobby stays waiting and missing media stays unknown", () => {
  const s = state("Someone will let you in", ["Leave"]);
  assert.equal(s.in_call, false);
  assert.equal(s.waiting, true);
  assert.equal(s.muted, null);
});
test("ready room has three service choices and active call hides switching", () => {
  const ready = renderRemote({in_call:false});
  for (const provider of ["meet", "teams", "zoom"]) assert.match(ready, new RegExp(`data-service="${provider}"`));
  const active = renderRemote({in_call:true, muted:false, camera_off:false});
  assert.doesNotMatch(active, /data-act="rm-service"/);
});
test("sharing gives laptop guidance without opening a room-screen picker", () => {
  const html = renderRemote({in_call:true}, 'shareSheet(null,true,true,"pc","atrium",{provider:"zoom",sharing:false})');
  assert.doesNotMatch(html, /data-zoom="share_start"|room Mac/);
  assert.match(html, /data-act="rm-invite-refresh"/);
  assert.match(html, /microphone and speakers off/);
});


test("ordinary screen commands retain their own successful completion feedback", async () => {
  const context = {
    S: {poke(){}}, window:{addEventListener(){}}, document:{addEventListener(){}},
    location:{hash:"#/tablet/atrium"}, navigator:{}, setInterval(){}, setTimeout(){},
  };
  const result = await runInNewContext(remoteSource + `
    post = async () => ({id:"cmd"});
    waitFor = async () => ({status:"done"});
    refresh = () => {};
    (async () => {
      await send({id:"tv",connected:true}, "display_off", {}, "TV off");
      return toast;
    })();`, context);
  assert.equal(result.text, "TV off ✓");
  assert.equal(result.kind, "ok");
});


test("live Zoom labels and text-only sharing toolbar report actual state", () => {
  const s = state("Meeting", ["End", "unmute my microphone", "start my video", {aria:null,text:"Stop Share\n"}], "app.zoom.us");
  assert.equal(s.provider, "zoom");
  assert.equal(s.in_call, true);
  assert.equal(s.muted, true);
  assert.equal(s.camera_off, true);
  assert.equal(s.sharing, true);
});


test("Teams remains setup-needed until the room advertises it as enabled", () => {
  const html = renderRemote({in_call:false});
  assert.match(html, /data-service="teams"[^>]*disabled[^>]*>Teams<small>Setup needed/);
  assert.doesNotMatch(html, /data-service="zoom"[^>]*disabled/);
});


test("Webex is offered once the room computer advertises it", () => {
  assert.match(renderRemote({in_call:false}), /data-service="webex"[^>]*disabled[^>]*>Webex<small>Setup needed/);
  const html = renderRemote({in_call:false}, 'devices[0].native.meeting_providers.push("webex"); homeView({roomId:"atrium", roomName:"Atrium", d:null, group:"atrium"})');
  assert.doesNotMatch(html, /data-service="webex"[^>]*disabled/);
});


test("starting a meeting disables both start and join until acknowledged", () => {
  const html = renderRemote({in_call:false}, 'pending.set("zoom:meet_now", "Starting"); homeView({roomId:"atrium", roomName:"Atrium", d:null, group:"atrium"})');
  assert.match(html, /data-sheet="join"[^>]*disabled/);
  assert.match(html, /data-zoom="meet_now"[^>]*disabled/);
});


test("Zoom host handoff is visible and ending for everyone requires confirmation", () => {
  const s = state("Assign a New Host", ["End", "unmute my microphone"], "app.zoom.us");
  assert.equal(s.needs_host, true);
  assert.equal(s.can_end, true);
  const html = renderRemote({in_call:true,provider:"zoom",can_end:true,needs_host:true,muted:true,camera_off:true});
  assert.match(html, /choose the next host on the room computer/i);
  assert.match(html, /data-zoom="end"[^>]*data-confirm="1"/);
});


test("meeting invitation exposes QR, copy and laptop join without room presentation", () => {
  const html = renderRemote({in_call:true}, `meetingInvites.atrium = {url:"https://meet.google.com/abc-defg-hij",qr:"data:image/svg+xml;base64,test"}; shareSheet(null,true,true,"pc","atrium",{provider:"meet",sharing:false})`);
  assert.match(html, /Scan to join this meeting/);
  assert.match(html, /data-act="rm-copy-invite"/);
  assert.match(html, /href="https:\/\/meet.google.com\/abc-defg-hij"/);
  assert.match(html, /Open meeting on this device/);
  assert.doesNotMatch(html, /data-zoom="share_start"/);
  const sharing = renderRemote({in_call:true}, 'shareSheet(null,true,true,"pc","atrium",{provider:"zoom",sharing:true})');
  assert.match(sharing, /data-zoom="share_stop"/);
});


test("Zoom text-only buttons report a live call and actual AV state", () => {
  const s = state("Meeting", [{aria:null,text:"End meeting"},{aria:null,text:"Unmute"},{aria:null,text:"Start Video"}], "app.zoom.us");
  assert.equal(s.in_call, true);
  assert.equal(s.can_end, true);
  assert.equal(s.muted, true);
  assert.equal(s.camera_off, true);
});


test("generic AV labels still read their actionable titles", () => {
  const s = state("Meeting", [{aria:"Leave"},{aria:"Mic",title:"Unmute mic",text:"Mic"},{aria:"Camera",title:"Turn camera on",text:"Camera"}], "teams.microsoft.com");
  assert.equal(s.muted, true);
  assert.equal(s.camera_off, true);
});

test("Meet and Zoom use one meeting stage with actual media state and no provider switch", () => {
  for (const provider of ["meet", "zoom"]) {
    const html = renderRemote({in_call:true, provider, title:"Weekly planning", muted:true, camera_off:false});
    assert.match(html, new RegExp(`data-provider="${provider}"`));
    assert.match(html, /aria-label="Current meeting"/);
    assert.match(html, /Weekly planning/);
    assert.match(html, /Microphone off/);
    assert.match(html, /Camera on/);
    assert.doesNotMatch(html, /data-act="rm-service"|<img[^>]*qr/);
  }
});

test("next booking remains visible and joinable even more than ten minutes away", () => {
  const html = renderRemote({}, `calInfo.atrium = {configured:true, calendar:"room", bookings:[
    {title:"Later Zoom", start:Date.now()/1000+7200, end:Date.now()/1000+10800, join_url:"https://zoom.us/j/123456789"},
    {title:"Next Meet", start:Date.now()/1000+3600, end:Date.now()/1000+5400, join_url:"https://meet.google.com/abc-defg-hij"}
  ]}; calCard("atrium", true)`);
  assert.match(html, /Up next/);
  assert.ok(html.indexOf("Next Meet") < html.indexOf("Later Zoom"));
  assert.equal((html.match(/data-act="rm-cal-join"/g) || []).length, 2);
  assert.match(html, /Google Meet/);
});

test("agenda excludes ended meetings, blocks duplicate joins and omits unauthorized join controls", () => {
  const expr = `calInfo.atrium = {configured:true, calendar:"room", bookings:[
    {title:"Ended", start:1, end:2, join_url:"https://zoom.us/j/123456789"},
    {title:"Private meeting", start:Date.now()/1000-60, end:Date.now()/1000+1800, join_url:""},
    {title:"Upcoming", start:Date.now()/1000+3600, end:Date.now()/1000+5400, join_url:"https://meet.google.com/abc-defg-hij"}
  ]}; pending.set("zoom:meet_now", "Starting"); `;
  const html = renderRemote({}, expr + 'calCard("atrium", true)');
  assert.doesNotMatch(html, /Ended/);
  assert.match(html, /Private meeting/);
  assert.match(html, /data-act="rm-cal-join"[^>]*disabled/);
  assert.doesNotMatch(renderRemote({}, expr + 'calCard("atrium", false)'), /data-act="rm-cal-join"/);
});

test("missing, failed and empty calendar states never invent room availability", () => {
  const missing = renderRemote({}, 'calInfo.atrium = {configured:false, calendar:"room"}; calCard("atrium",true)');
  assert.match(missing, /calendar not connected/);
  assert.doesNotMatch(missing, /Available|Free/);
  const failed = renderRemote({}, 'calInfo.atrium = {error:true}; calCard("atrium",true)');
  assert.match(failed, /temporarily unavailable/);
  const empty = renderRemote({}, 'calInfo.atrium = {configured:true,calendar:"room",bookings:[]}; calCard("atrium",true)');
  assert.match(empty, /No upcoming bookings/);
});


test("provider device and connection notices appear inline on the controller", () => {
  const html = renderRemote({in_call:true, provider:"meet", muted:true, camera_off:true, display_attention:"connection"});
  assert.match(html, /class="rm-note" role="status">The meeting connection needs attention/);
  const media = renderRemote({in_call:true, provider:"zoom", muted:true, camera_off:true, display_attention:"media"});
  assert.match(media, /meeting reports a microphone or camera issue/);
});


test("native live meeting offers provider controls; offline and idle do not", () => {
  assert.match(renderRemote({in_call:true,provider:"meet"}), /data-sheet="controls"/);
  assert.doesNotMatch(renderRemote({in_call:false,provider:"meet"}), /data-sheet="controls"/);
});
test("provider controls identify destructive actions and reflect unavailable permissions", () => {
  const html = renderRemote({}, `roomControls.atrium = {controls:[{id:"token",label:"Remove",context:"Alice",destructive:true}]}; controlsSheet("atrium")`);
  assert.match(html, /Alice/);
  assert.match(html, /data-destructive="1"/);
  const empty = renderRemote({}, `roomControls.atrium = {controls:[]}; controlsSheet("atrium")`);
  assert.match(empty, /No additional controls/);
  assert.doesNotMatch(empty, /data-act="rm-provider-control"/);
});

test("pending provider admissions are prominent without exposing a participant on the public view", () => {
  const html = renderRemote({in_call:true,provider:"meet",admission_pending:true});
  assert.match(html, /Admission requests/);
  assert.match(html, /Review people waiting to join/);
});

test("meeting Back is available only when the appliance supports it", () => {
  const supported = renderRemote({}, `roomControls.atrium = {back:true,controls:[]}; controlsSheet("atrium")`);
  assert.match(supported, /data-act="rm-controls-back"/);
  const older = renderRemote({}, `roomControls.atrium = {controls:[]}; controlsSheet("atrium")`);
  assert.doesNotMatch(older, /data-act="rm-controls-back"/);
});

test("the meeting home has five large reactions and a More button", () => {
  const html = renderRemote({in_call:true,provider:"meet"}, `
    roomControls.atrium={controls:[{id:"open",label:"Send a reaction",group:"Meeting"}]};
    homeView({roomId:"atrium",roomName:"Atrium",d:null,group:"atrium"});`);
  assert.equal((html.match(/class="rm-reaction"/g)||[]).length,5);
  for (const icon of ["👍","👏","❤️","😂","🎉"]) assert.ok(html.includes(icon));
  assert.match(html,/data-act="rm-reactions-more"[^>]*aria-expanded="false"/);
  assert.doesNotMatch(html,/data-reaction="sad"|data-act="wb-start"/);
});

test("expanded reactions show only choices offered by the provider", () => {
  const html = renderRemote({}, `
    roomControls.atrium={controls:[
      {id:"clap",label:"clapping hands",group:"Reactions"},
      {id:"sad",label:"😢",group:"Reactions"},
      {id:"unrelated",label:"Thumbs down",group:"People",context:"Alice"}
    ]};reactionsExpanded.atrium=true;reactionsPanel("atrium");`);
  assert.match(html,/aria-expanded="true"/);
  assert.match(html,/data-reaction="sad"/);
  assert.doesNotMatch(html,/data-reaction="thumbs-down"/);
  assert.match(html,/data-reaction="heart"[^>]*disabled/);
});

test("Zoom and Meet reaction names use the same display and separate people controls", () => {
  const html = renderRemote({}, `
    roomControls.atrium={controls:[
      {id:"emoji",label:"💖",group:"Reactions"},
      {id:"zoom",label:"joy",group:"Reactions",reaction:{key:"laugh"}},
      {id:"people",label:"Show everyone (2)",group:"Meeting"},
      {id:"remove",label:"Remove",group:"People",context:"Alice",destructive:true},
      {id:"captions",label:"Turn on captions",group:"Meeting"}
    ]};controlsSheet("atrium");`);
  assert.match(html,/People & host controls/);
  assert.match(html,/Meeting options/);
  assert.match(html,/data-reaction="heart"/);
  assert.doesNotMatch(html,/data-act="rm-provider-control"[^>]*data-control="emoji"/);
  assert.match(html,/data-control="remove"[^>]*data-destructive="1"/);
});

test("one reaction tap opens a closed palette and sends only its fresh token", async () => {
  const result = await renderRemote({}, `(async()=>{
    roomPc.atrium="pc";const calls=[];let reads=0;
    loadControls=async()=> (++reads===1 ? {controls:[{id:"open",label:"Reactions",group:"Meeting"}]} : {controls:[{id:"fresh",label:"👍",group:"Reactions"}]});
    pcCall=async(pc,action,args)=>{calls.push({pc,action,token:args.control});return true;};
    await sendReaction("atrium","thumbs-up");return {calls,locked:pending.size};
  })()`);
  assert.deepEqual(JSON.parse(JSON.stringify(result.calls)),[
    {pc:"pc",action:"control",token:"open"}, {pc:"pc",action:"control",token:"fresh"},
  ]);
  assert.equal(result.locked,0);
});

test("reaction permission or opening failure never sends a guessed reaction", async () => {
  const result=await renderRemote({}, `(async()=>{
    roomPc.atrium="pc";const calls=[],errors=[];
    say=(message)=>errors.push(message);
    loadControls=async()=>({controls:[]});
    pcCall=async(pc,action,args)=>{calls.push(args.control);return false;};
    await sendReaction("atrium","heart");
    loadControls=async()=>({controls:[{id:"open",label:"Reactions",group:"Meeting"}]});
    await sendReaction("atrium","heart");return {calls,errors,locked:pending.size};
  })()`);
  assert.deepEqual(JSON.parse(JSON.stringify(result.calls)),["open"]);
  assert.equal(result.errors.length,2);
  assert.equal(result.locked,0);
});

test("extra reaction display survives palette closure without retaining action tokens", () => {
  const html = renderRemote({}, `
    roomControls.atrium={controls:[{id:"open-fresh",label:"Reactions",group:"Meeting"}]};
    reactionCatalog.atrium=new Set(["thumbs-up","clap","heart","laugh","celebrate","sad"]);
    reactionsExpanded.atrium=true;reactionsPanel("atrium");`);
  assert.match(html,/data-reaction="sad"/);
  assert.doesNotMatch(html,/Opening more reactions|data-control=/);
  assert.doesNotMatch(html,/data-reaction="sad"[^>]*disabled/);
});

test("ambiguous Yes and No buttons are never exposed as legacy emoji choices", () => {
  const html=renderRemote({}, `
    roomControls.atrium={controls:[{id:"unsafe",label:"Yes",group:"Reactions"}]};
    reactionsExpanded.atrium=true;controlsSheet("atrium");`);
  assert.doesNotMatch(html,/data-reaction="yes"|data-control="unsafe"/);
});


test("guest rooms grey out starting calls while keeping invitation joining available", () => {
  const html = renderRemote({in_call:false});
  assert.match(html, /data-zoom="meet_now"[^>]*disabled/);
  assert.match(html, /Unavailable · Room joins as a guest/);
  assert.match(html, /data-sheet="join"(?![^>]*disabled)/);
});

test("meeting view omits recording controls without a notes service", () => {
  const html = renderRemote({in_call:true,provider:"meet"});
  assert.doesNotMatch(html, /rm-notes|rm-capture|Stop recording|Start notes/);
});

test("calendar code is visible on the authorized controller alongside Join", () => {
  const html = renderRemote({in_call:false}, `(() => {
    calInfo.atrium={configured:true,calendar:"room",bookings:[{title:"Invite",start:Date.now()/1000,end:Date.now()/1000+600,join_url:"https://meet.google.com/abc-defg-hij"}]};
    return calCard("atrium",true);
  })()`);
  assert.match(html, /Code abc-defg-hij/);
  assert.match(html, /data-act="rm-cal-join"/);
});

test("human verification and required sign-in are distinct guest outcomes", () => {
  const bot = state("Automated bots aren't allowed to join", [], "app.zoom.us");
  assert.equal(bot.verification, true);
  assert.equal(bot.signin, false);
  const signin = state("Sign in to join the meeting", [], "app.zoom.us");
  assert.equal(signin.signin, true);
  assert.equal(signin.verification, false);
});

test("Zoom invalid-link response is detected before the join timeout", () => {
  assert.equal(state("This meeting link is invalid (3,001)", [], "app.zoom.us").missing, true);
});

test("room home keeps whiteboard without a notes tile", () => {
  const html = renderRemote({in_call:false}, 'homeView({roomId:"atrium",roomName:"Atrium",d:null,group:"atrium"})');
  assert.match(html, /class="rm-home-tools"/);
  assert.match(html, /data-act="wb-start"/);
  assert.doesNotMatch(html, /rm-notes|rm-capture|Start notes|Stop recording/);
});

test("only a confirmed provider end screen signals the room to return home", () => {
  assert.equal(state("The host has ended this meeting", []).ended,true);
  assert.equal(state("You left the meeting", []).ended,true);
  assert.equal(state("Meeting", ["Leave call"]).ended,false);
  assert.equal(state("Someone will let you in", []).ended,false);
});

test("advanced Zoom controls disable only the pending action", () => {
  const html = renderRemote({}, `zoomInfo.atrium = {configured:true, mapped:true, status:"Available"};
    pending.set("zoom:join", "Joining"); zoomPanel("atrium")`);
  assert.match(html, /data-zoom="join"[^>]*disabled/);
  assert.doesNotMatch(html, /data-zoom="mute"[^>]*disabled/);
});

test("Zoom ignores repeated actions in flight, paints pending immediately, and permits retry", async () => {
  let paints = 0;
  const result = await runInNewContext(remoteSource + `
    (async () => {
      let requests = 0, release;
      post = async () => { requests++; await new Promise(resolve => { release = resolve; }); };
      refreshZoom = () => {};
      say = () => {};
      const first = zoomCall("atrium", "mute", {}, "Mute");
      const repeated = await zoomCall("atrium", "mute", {}, "Mute");
      const during = {requests, repeated, busy: pending.has("zoom:mute")};
      release();
      await first;
      const settled = !pending.has("zoom:mute");
      post = async () => { requests++; throw new Error("Network unavailable"); };
      await zoomCall("atrium", "mute", {}, "Mute");
      const failed = !pending.has("zoom:mute");
      post = async () => { requests++; };
      await zoomCall("atrium", "mute", {}, "Mute");
      return {during, settled, failed, requests};
    })();`, {
    S: { poke() { paints++; } },
    window: { addEventListener() {} }, document: { addEventListener() {} },
    setInterval() {},
  });
  assert.equal(result.during.requests, 1);
  assert.equal(result.during.repeated, false);
  assert.equal(result.during.busy, true);
  assert.equal(result.settled, true);
  assert.equal(result.failed, true);
  assert.equal(result.requests, 3);
  assert.equal(paints, 3);
});

test("a late Zoom status cannot overwrite a newer room status or another room's refresh", async () => {
  const reads = [];
  let paints = 0;
  const result = await runInNewContext(remoteSource + `
    (async () => {
      const old = refreshZoom("atrium");
      const current = refreshZoom("atrium");
      const other = refreshZoom("lamar");
      reply(1, {status:"InMeeting"});
      await current;
      reply(2, {status:"Available"});
      await other;
      reply(0, {status:"Available"});
      await old;
      return {atrium:zoomInfo.atrium.status, lamar:zoomInfo.lamar.status};
    })();`, {
    fetch: () => new Promise(resolve => reads.push(resolve)),
    reply: (index, status) => reads[index]({ json: async () => status }),
    S: { poke() { paints++; } },
    window: { addEventListener() {} }, document: { addEventListener() {} }, setInterval() {},
  });
  assert.equal(result.atrium, "InMeeting");
  assert.equal(result.lamar, "Available");
  assert.equal(paints, 2, "stale responses do not repaint the meeting controls");
});

test("Escape closes the join dialog immediately even while its input is focused", () => {
  const listeners = {};
  let paints = 0, blurs = 0, prevented = 0;
  const document = { addEventListener(type, listener) { listeners[type] = listener; } };
  const input = {
    id: "rm-join", tagName: "INPUT", closest: () => true,
    blur() { blurs++; document.activeElement = null; },
  };
  document.activeElement = input;
  const result = runInNewContext(remoteSource + `
    sheet = "join";
    escape();
    sheet;
  `, {
    document, window: { addEventListener() {} }, setInterval() {},
    location: { hash: "#/tablet/atrium" }, S: { poke() { paints++; } },
    escape: () => listeners.keydown({ key: "Escape", target: input, preventDefault() { prevented++; } }),
  });
  assert.equal(result, "");
  assert.equal(blurs, 1);
  assert.equal(paints, 1, "the typing guard must not leave the closed dialog visible");
  assert.equal(prevented, 1);
});


test("Zoom's waiting room is waiting, not a call", () => {
  const s = state("Please wait, the meeting host will let you in soon.", ["Leave"], "app.zoom.us");
  assert.equal(s.provider, "zoom");
  assert.equal(s.waiting, true);
  assert.equal(s.in_call, false);
});

test("a passcode prompt, a browser-join block and sign-in-only meetings are reported before joining", () => {
  assert.equal(state("Enter meeting passcode", ["Join"], "app.zoom.us").needs_passcode, true);
  assert.equal(state("This meeting does not support joining from a browser", [], "app.zoom.us").browser_blocked, true);
  assert.equal(state("This meeting is for authorized attendees only", [], "app.zoom.us").signin, true);
  const inCall = state("Enter meeting passcode", ["Leave", "Mute"], "app.zoom.us");
  assert.equal(inCall.in_call, true);
  assert.equal(inCall.needs_passcode, false, "chat text in a live call never reads as a prompt");
});

test("Webex pages are Webex, and unknown hosts are no provider rather than Teams", () => {
  const s = state("People", ["Leave meeting", "Unmute", "Start video"], "acme.webex.com");
  assert.equal(s.provider, "webex");
  assert.equal(s.in_call, true);
  assert.equal(s.muted, true);
  assert.equal(s.camera_off, true);
  assert.equal(s.code, "");
  assert.equal(state("", [], "example.com").provider, null);
});

test("a Webex site asking guests for an email is reported, an optional one is not", () => {
  const field = (aria) => ({ type: "email", value: "", getAttribute: (a) => a === "aria-label" ? aria : null, getClientRects: () => [1] });
  const webex = (inputs, labels = ["Next"]) => JSON.parse(runInNewContext(stateJS, {
    document: { body: { innerText: "Enter your information" }, title: "Webex",
      querySelectorAll: (sel) => sel === "input" ? inputs : labels.map(label => ({ getAttribute: () => label })) },
    location: { hostname: "acme.my.webex.com", pathname: "/webappng/sites/acme/meeting/info/x" },
  }));
  assert.equal(webex([field("Email address")]).needs_email, true);
  assert.equal(webex([field("Email address (optional)")]).needs_email, false);
  assert.equal(webex([]).needs_email, false);
  assert.equal(webex([field("Email address")], ["Leave meeting"]).needs_email, false, "never once in the call");
});
