import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
const source = readFileSync(new URL("../app/js/groups.js", import.meta.url), "utf8");
const {members,displayMembers,selectedDisplay} = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));
const rooms=[{id:"atrium",name:"Atrium"}];
const stick={id:"old-tv",room:"atrium",role:"page",online:false};
const pc={id:"room-pc",room:"atrium",role:"roompc",online:true,client:"xosroom-linux-0.1.0"};
test("offline stick falls back to the working Linux TV, including a saved old selection",()=>{
  const screens=displayMembers([stick,pc],rooms,"atrium");
  assert.equal(screens[0].id,pc.id);
  assert.equal(selectedDisplay(screens,stick.id).id,pc.id);
  assert.equal(selectedDisplay(screens,stick.id,stick).id,pc.id);
});
test("online separate TV and other rooms retain their controls",()=>{
  const online={...stick,online:true};
  assert.deepEqual(displayMembers([online,pc],rooms,"atrium"),[online]);
  assert.deepEqual(displayMembers([stick,{...pc,room:"another"}],rooms,"atrium"),[stick]);
  assert.deepEqual(displayMembers([stick,{...pc,online:false}],rooms,"atrium"),[stick]);
});


test("More renders content commands for the working room computer without TV-only controls",async()=>{
  const {runInNewContext}=await import("node:vm");
  const remote=readFileSync(new URL("../app/js/views/remote.js",import.meta.url),"utf8")
    .replace(/^import .*;$/gm,"").replace(/export function /g,"function ");
  class Events { static CLOSED=2; constructor(){this.readyState=1;} }
  const html=runInNewContext(remote+`
    devices=fixture; zoomInfo.atrium={};mode="more";
    renderRoomRemote("atrium");
  `,{
    fixture:[stick,pc],members,displayMembers,selectedDisplay,
    S:{isHub:()=>true,rooms:()=>rooms,room:()=>({name:"Atrium",display:{idle_page:"atrium-room"}}),poke(){}},
    groupRoom:d=>d.room,esc:String,clock:()=>"10:00",getPages:()=>[{id:"atrium-room",name:"Atrium room",kind:"url"}],
    window:{addEventListener(){}},document:{addEventListener(){}},
    location:{hash:"#/tablet/atrium"},EventSource:Events,setInterval(){},
    localStorage:{getItem:()=>stick.id},
  });
  assert.match(html,/data-dev="room-pc" data-type="route"/);
  assert.match(html,/data-dev="room-pc" data-type="refresh"/);
  assert.doesNotMatch(html,/data-dev="room-pc" data-type="(?:display_on|display_off|page_key|message|screenshot)"/);
  assert.match(html,/The room computer drives this TV/);
});
