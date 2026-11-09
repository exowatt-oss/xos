import { userFetch, whoAmI, signInUrl } from '../js/api.js';
import {createPairing} from './pairing.js';
import {siteChoices} from './pairing-state.js';
const root=document.getElementById('studio'), dialog=document.getElementById('editor');
let data=null, me=null, meAt=0, site='all', selected=new Set(), pickDemo='', draft=null, busy=false, uploading=false, polling=false;
// 'all' or a session id while the full-screen watch view is open.
let watching=null;
// Which sections are open survives the two-second refresh; headsets are picked for you until you
// change the selection yourself.
const open={headsets:false,library:false,setup:false};
let picked=false;
const previewURLs=new Map(), previewTimes=new Map();
const can=p=>me?.perms?.includes(p);
const pairing=createPairing({dialog:document.getElementById('pairing'),api,can,previewing:()=>Boolean(me?.preview),changed:()=>refresh(true),useHeadset:id=>{picked=true;selected=new Set([id]);render();window.scrollTo({top:0,behavior:'smooth'});}});
function el(tag,attrs={},...children){
  const n=document.createElement(tag);
  for(const [k,v] of Object.entries(attrs)){
    if(k.startsWith('on')) n.addEventListener(k.slice(2),v);
    else if(k==='class') n.className=v;
    else if(k==='checked'||k==='disabled'||k==='selected') n[k]=Boolean(v);
    else if(k==='value') n.value=v;
    else if(v!=null) n.setAttribute(k,v);
  }
  for(const c of children.flat(Infinity)) if(c!=null) n.append(c instanceof Node?c:document.createTextNode(String(c)));
  return n;
}
const btn=(text,run,cls='',disabled=false)=>el('button',{type:'button',class:cls,onclick:run,disabled},text);
const row=(...c)=>el('div',{class:'row'},...c);
function notice(t){const n=document.getElementById('notice');n.textContent=t;clearTimeout(notice.timer);notice.timer=setTimeout(()=>n.textContent='',6500);}
async function api(path,method='GET',body){
  const r=await userFetch('/api/demos/'+path,{method,cache:'no-store',...(body!==undefined?{headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
  const j=await r.json().catch(()=>({}));if(!r.ok)throw Error(j.detail||'Request failed ('+r.status+')');return j;
}
async function action(path,method='POST',body){
  if(busy)return;if(me?.preview){notice('Preview is read-only. Use Exit preview in Xos to make changes.');return;}busy=true;
  try{await api(path,method,body);await refresh(true);return true;}
  catch(e){notice(e.message);return false;}finally{busy=false;}
}
// What a paired headset has saved of the files the demo library uses.
function storageLine(u){
  const t=u.telemetry,needed=[...new Set(data.demos.flatMap(d=>d.steps.map(s=>s.asset_id)))];
  if(!u.paired||!t?.app_mode||!needed.length)return null;
  if(t.download){const a=data.assets?.find(a=>a.id===t.download.id);
    return el('small',{},'Downloading '+(a?.name||'a demo file')+(t.download.total?' · '+Math.floor(100*t.download.received/t.download.total)+'%':''));}
  const have=needed.filter(id=>t.cached?.includes(id)).length;
  return el('small',{},have===needed.length?'All '+needed.length+' demo files on the headset':'On the headset · '+have+' / '+needed.length+' demo files');
}
function sessionFor(u){return data.sessions.find(s=>s&&s.id===u.session_id);}
function activeSessions(){return data.sessions.filter(s=>s&&data.units.some(u=>u.session_id===s.id));}
function shown(){return data.units.filter(u=>site==='all'||u.site===site);}
function chooseSite(s){site=s;render();}
function watchUnits(){return data.units.filter(u=>sessionFor(u)&&(watching==='all'||u.session_id===watching));}
function watch(id){watching=id;render();document.documentElement.requestFullscreen?.().catch(()=>{});}
function stopWatching(){watching=null;if(document.fullscreenElement)document.exitFullscreen().catch(()=>{});render();}
document.addEventListener('fullscreenchange',()=>{if(!document.fullscreenElement&&watching){watching=null;render();}});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&watching)stopWatching();});
// Every headset in the running demo, full screen, for the people in the room who aren't wearing one.
function watchView(){
  const units=watchUnits(), sessions=data.sessions.filter(s=>s&&units.some(u=>u.session_id===s.id)), now=Date.now()/1000;
  const cols=Math.ceil(Math.sqrt(units.length||1)), rows=Math.ceil((units.length||1)/cols);
  const tile=u=>{
    const fresh=u.settings.values.preview_enabled&&can('demos.previews')&&u.online&&now-u.preview_at<10;
    const img=el('img',{'data-preview':u.id,alt:'Live view from '+u.name,class:fresh?'':'hide'});
    if(fresh&&previewURLs.has(u.id))img.src=previewURLs.get(u.id);
    return el('figure',{class:'watch-tile'},
      el('span',{},!u.online?'Headset offline':!u.settings.values.preview_enabled?'Preview sharing is off':!can('demos.previews')?'No preview access':'Waiting for the headset view…'),img,
      el('figcaption',{},fresh?el('span',{class:'watch-live'},'Live'):null,el('b',{},u.name)));
  };
  return el('section',{class:'watch','aria-label':'Live headset views'},
    el('header',{},el('div',{class:'grow'},...(sessions.length?sessions.map(s=>el('p',{},el('b',{},s.demo_name),
        ' · Scene '+(s.step_index+1)+' of '+s.steps+' · '+s.step.title+' · '+(s.preparing?'Getting ready':s.playing?'Playing':'Paused'))):[el('p',{},el('b',{},'No demo running'))])),
      el('span',{class:'watch-count'},units.length+' headset'+(units.length===1?'':'s')),
      sessions.length===1&&can('demos.control')&&!sessions[0].preparing?btn(sessions[0].playing?'❚❚ Pause':'▶ Play',()=>command(sessions[0],sessions[0].playing?'pause':'play'),'primary'):null,
      btn('Exit full screen',stopWatching)),
    units.length?el('div',{class:'watch-grid',style:`grid-template-columns:repeat(${cols},minmax(0,1fr));grid-template-rows:repeat(${rows},minmax(0,1fr))`},...units.map(tile))
      :el('p',{class:'watch-empty'},'Start a demo and every headset in it shows here live.'));
}
function command(s,actionName,position=0){action('sessions/'+s.id+'/control','POST',{action:actionName,position});}
function unitCard(u){
  const s=sessionFor(u), t=u.telemetry, isFresh=u.settings.values.preview_enabled&&can('demos.previews')&&u.online&&Date.now()/1000-u.preview_at<10;
  const img=el('img',{'data-preview':u.id,alt:'App view from '+u.name,class:isFresh?'':'hide'});
  if(isFresh&&previewURLs.has(u.id))img.src=previewURLs.get(u.id);
  const frame=el('div',{class:'preview'},el('span',{},!u.settings.values.preview_enabled?'Preview sharing disabled':!can('demos.previews')?'Preview access disabled':!u.paired?'Pair this headset':u.online?'Waiting for app preview':'Headset offline'),img,
    el('span',{class:'stamp'},isFresh?'App view · '+Math.max(0,Math.round(Date.now()/1000-u.preview_at))+'s ago':'No fresh preview'));
  const check=el('input',{type:'checkbox',checked:selected.has(u.id),disabled:!u.enabled||!u.paired,'aria-label':'Select '+u.name,
    onchange:e=>{picked=true;e.target.checked?selected.add(u.id):selected.delete(u.id);render();}});
  const meta=[row(el('span',{class:'pill '+(u.online?'on':'off')},u.online?'Online':'Offline'),el('small',{},siteChoices(data.units).find(s=>s.id===u.site)?.label||'Site')),
    el('div',{},s?(s.step_index+1)+' / '+s.steps+' · '+s.step.title:'Ready for a guided demo'),
    el('small',{},u.paired?(t?.state||'Waiting for check-in')+(t?.immersive?' · In VR':' · Enter VR on headset'):'Open the Quest viewer to get a pairing code')];
  if(u.paired)meta.push(el('small',{},(t?.app_mode==='installed'?'Quest app':'Browser viewer')+' · Settings '+(u.online&&t?.settings_revision===u.settings.revision?'applied':'waiting for headset')));
  const latest=u.commands?.at(-1);
  if(latest)meta.push(el('small',{role:'status'},latest.action.replaceAll('_',' ')+' · '+latest.status+(latest.error?' · '+latest.error:'')));
  const stored=storageLine(u);if(stored)meta.push(stored);
  if(t?.storage_error)meta.push(el('div',{class:'error'},t.storage_error));
  if(t?.error)meta.push(el('div',{class:'error'},t.error));
  const tools=[];
  if(can('demos.enable'))tools.push(btn(u.enabled?'Disable':'Enable',()=>action('units/'+u.id,'PATCH',{enabled:!u.enabled})));
  if(can('demos.pair')){
    if(!u.paired)tools.push(btn('Set up',()=>pairing.open(u.id),'primary',!u.enabled));
    else tools.push(btn('Check setup',()=>pairing.open(u.id)));
    if(u.paired)tools.push(btn('Unpair',()=>{if(confirm('Unpair '+u.name+'? It will need a new pairing code.'))action('units/'+u.id+'/pair','DELETE');},'danger'));
  }
  if(u.paired&&can('demos.recover'))tools.push(btn('Recenter',()=>recover([u.id],'recenter'),'',!u.online),btn('Fix / settings',()=>headsetSettings([u.id])));
  else if(u.paired&&can('demos.settings'))tools.push(btn('Settings',()=>headsetSettings([u.id])));
  if(can('demos.rename')){
    tools.push(btn('Rename',()=>{const n=prompt('Headset name',u.name);if(n?.trim())action('units/'+u.id,'PATCH',{name:n.trim()});}));
  }
  return el('article',{class:'unit '+(!u.enabled?'disabled':'')},
    el('div',{class:'unit-head'},check,el('div',{class:'name'},el('b',{},u.name),el('small',{},u.enabled?'Enabled':'Disabled'))),
    frame,el('div',{class:'unit-meta'},...meta,el('div',{class:'actions'},...tools)));
}
const mmss=t=>{t=Math.max(0,Math.floor(t||0));return Math.floor(t/60)+':'+String(t%60).padStart(2,'0');};
const neededFor=d=>[...new Set((d?.steps||[]).map(s=>s.asset_id))];
// One headset in plain words: ok / wait / err / off, and what it is doing.
function headsetStatus(u,s){
  const t=u.telemetry||{};
  if(!u.online)return ['off','Offline'];
  if(t.storage_error)return ['err',t.storage_error];
  if(t.state==='error')return ['err',t.error||'Needs attention'];
  if(t.download&&(!s||t.ready_token!==s.step_token))return ['wait','Downloading'+(t.download.total?' '+Math.floor(100*t.download.received/t.download.total)+'%':'…')];
  if(s){
    if(t.ready_token!==s.step_token)return ['wait','Loading'];
    return ['ok',(s.playing?'Playing':'Ready')+(s.asset?.kind==='video'&&t.local===false?' · streaming':'')];
  }
  const missing=neededFor(data.demos.find(d=>d.id===pickDemo)).filter(id=>!t.cached?.includes(id)).length;
  return missing?['wait',missing+' file'+(missing>1?'s':'')+' to download']:['ok','Ready'];
}
const chip=(u,s,tag='span',attrs={})=>{const [k,t]=headsetStatus(u,s);return el(tag,{...attrs,class:'hs '+k+' '+(attrs.class||'')},el('i',{}),el('b',{},u.name),el('small',{},t));};
function presenter(s){
  const units=data.units.filter(u=>u.session_id===s.id), ctl=can('demos.control'), video=s.asset?.kind==='video';
  const ready=units.filter(u=>headsetStatus(u,s)[0]==='ok').length;
  const duration=Math.max(0,...units.map(u=>u.telemetry?.duration||0));
  // The session clock runs on past the end of the video; the timeline stops at its length.
  const elapsed=s.position+(s.playing?Math.max(0,data.now-s.anchor_at):0), position=duration?Math.min(elapsed,duration):elapsed;
  const ended=video&&units.length&&units.every(u=>u.telemetry?.state==='ended');
  const toggle=s.preparing?btn('Getting ready · '+ready+' of '+units.length,null,'big primary wide',true)
    :ended?btn('↺  Play again',()=>command(s,'seek',0),'big primary wide',!ctl)
    :btn(s.playing?'❚❚  Pause':'▶  Play',()=>command(s,s.playing?'pause':'play'),'big primary wide',!ctl);
  return el('section',{class:'presenter','aria-label':'Now presenting '+s.demo_name},
    row(el('div',{class:'grow'},el('small',{class:'live-tag'},'Now presenting'),el('h2',{},s.demo_name),
      el('p',{class:'muted'},'Scene '+(s.step_index+1)+' of '+s.steps+' · '+s.step.title)),btn('Watch full screen',()=>watch(s.id))),
    video&&duration?el('div',{class:'timeline'},el('span',{},mmss(position)),
      el('div',{class:'bar'},el('i',{style:'width:'+(duration?Math.min(100,position/duration*100):0)+'%'})),el('span',{},duration?mmss(duration):'')):null,
    el('div',{class:'transport'},btn('⏮  Previous',()=>command(s,'previous'),'big',!ctl||s.step_index===0),toggle,
      btn('Next  ⏭',()=>command(s,'next'),'big',!ctl||s.step_index>=s.steps-1)),
    el('div',{class:'actions'},...(video&&ctl?[btn('↺ Restart scene',()=>command(s,'seek',0)),btn('−10 s',()=>command(s,'seek',Math.max(0,position-10))),
      btn('+10 s',()=>command(s,'seek',position+10))]:[]),el('span',{class:'grow'}),
      el('small',{class:'muted'},ctl?'Space bar plays and pauses':''),
      ctl?btn('Stop demo',()=>{if(confirm('Stop '+s.demo_name+'? Every headset goes back to standby.'))command(s,'stop');},'danger'):null),
    el('div',{class:'hs-list'},...units.map(u=>chip(u,s))));
}
function starter(again){
  const demo=data.demos.find(d=>d.id===pickDemo);
  const free=data.units.filter(u=>u.paired&&u.enabled&&!sessionFor(u));
  if(!picked)selected=new Set(free.filter(u=>u.online).map(u=>u.id));
  const chosen=free.filter(u=>selected.has(u.id)), need=neededFor(demo);
  const saved=chosen.filter(u=>need.every(id=>u.telemetry?.cached?.includes(id))).length;
  const pick=ids=>{picked=true;selected=new Set(ids);render();};
  const blocked=!can('demos.present')?'Your role can watch demos but not start them.':!data.enabled?'Turn on demo mode to start.':!demo?'Choose a demo.':!chosen.length?'Choose at least one headset.':'';
  return el('section',{class:'launch'},
    el('h2',{},again?'Start another demo':'Start a demo'),
    el('p',{class:'step-label'},'1 · Choose a demo'),
    data.demos.length?el('div',{class:'demo-pick'},...data.demos.map(d=>btn([el('b',{},d.name),el('small',{},d.steps.length+' scene'+(d.steps.length===1?'':'s'))],()=>{pickDemo=d.id;render();},d.id===pickDemo?'pick on':'pick')))
      :el('div',{class:'empty'},'No demos yet. Create one under Demo library below.'),
    row(el('p',{class:'step-label grow'},'2 · Choose headsets'),...(free.length?[
      btn('All',()=>pick(free.map(u=>u.id)),'link'),...siteChoices(data.units).map(s=>btn(s.label,()=>pick(free.filter(u=>u.site===s.id).map(u=>u.id)),'link')),
      btn('None',()=>pick([]),'link')]:[])),
    free.length?el('div',{class:'hs-list'},...free.map(u=>chip(u,null,'button',{type:'button','aria-pressed':String(selected.has(u.id)),class:'choose'+(selected.has(u.id)?' on':''),
      onclick:()=>{picked=true;selected.has(u.id)?selected.delete(u.id):selected.add(u.id);render();}})))
      :el('div',{class:'empty'},'No free headsets. Pair one under Headsets below, or stop a running demo.'),
    demo&&chosen.length?el('p',{class:'muted'},saved===chosen.length?'Saved on '+(chosen.length===1?'the headset':'all '+chosen.length+' headsets')+', so it plays without streaming.'
      :(chosen.length-saved)+' of '+chosen.length+' headsets are still downloading it. The demo starts once every headset has it saved.'):null,
    el('div',{class:'actions'},btn('▶  Start demo'+(chosen.length?' on '+chosen.length+' headset'+(chosen.length===1?'':'s'):''),()=>start(true),'big primary',Boolean(blocked)),
      btn('Load paused',()=>start(false),'',Boolean(blocked)),el('small',{class:'muted'},blocked)));
}
// A collapsible section; it stays open or closed across refreshes.
function panel(key,title,note,...body){
  const d=el('details',{class:'panel'},el('summary',{},el('b',{},title),note?el('small',{class:'muted'},note):null),...body);
  d.open=open[key];d.addEventListener('toggle',()=>{open[key]=d.open;});
  return d;
}
function render(){
  if(!data)return;
  const units=shown(), online=units.filter(u=>u.online).length;
  const running=activeSessions(), paired=data.units.filter(u=>u.paired);
  const shell=el('div',{class:'shell'},
    el('header',{class:'top'},el('div',{},el('h1',{},'Demo Studio'),el('p',{class:'muted'},'Pick a demo, pick headsets, press start. Headsets play from their own saved copy; you control play and pause.')),
      el('div',{class:'actions'},btn('Watch live'+(running.length?' · '+data.units.filter(sessionFor).length:''),()=>watch('all')),
        can('demos.enable')?btn(data.enabled?'Demo mode on':'Demo mode off',()=>action('enabled','PUT',{enabled:!data.enabled}),'mode '+(data.enabled?'on':'off')):
          el('span',{class:'pill '+(data.enabled?'on':'off')},data.enabled?'Demo mode on':'Demo mode off'))),
    !data.enabled?el('div',{class:'error'},'Demo mode is off, so headsets stay in standby. ',can('demos.enable')?btn('Turn it on',()=>action('enabled','PUT',{enabled:true}),'primary'):null):null,
    can('demos.pair')&&!paired.length?el('section',{class:'pair-intro'},el('h2',{},'Get your headsets ready'),el('p',{},'Install Xos Quest once, open it, and enter its six-digit code. We’ll check the connection for you.'),btn('Set up a headset',()=>pairing.open('',site),'primary')):null,
    ...running.map(presenter),
    !running.length||data.units.some(u=>u.paired&&u.enabled&&!sessionFor(u))?starter(running.length>0):null,
    panel('headsets','Headsets',paired.length+' paired · '+data.units.filter(u=>u.online).length+' online',
      el('div',{class:'filters'},...([{id:'all',label:'All sites'},...siteChoices(data.units)]).map(s=>btn(s.label,()=>chooseSite(s.id),site===s.id?'active':'')),
        el('span',{class:'grow'}),el('small',{},online+' / '+units.length+' online'),
        can('demos.pair')?btn('+ Add headset',()=>pairing.open('',site),'primary'):null,
        row(can('demos.recover')?btn('Recenter selected',()=>recover([...selected],'recenter'),'',!selected.size):null,
          can('demos.recover')||can('demos.settings')?btn('Headset tools & settings',()=>headsetSettings([...selected]),'',!selected.size):null)),
      el('section',{class:'fleet','aria-label':'Headset views'},...units.map(unitCard))),
    panel('library','Demo library',data.demos.length+' demo'+(data.demos.length===1?'':'s'),
      row(el('p',{class:'muted grow'},'Mix video, immersive media, and 3D scenes in the same walkthrough. Every paired headset downloads each file a demo uses.'),
        can('demos.edit')?btn('+ Create demo',()=>editDemo(),'primary'):null,
        can('demos.upload')||can('demos.delete')?btn('Manage media',()=>editDemo()):null),
      data.demos.length?el('div',{class:'demo-list'},...data.demos.map(d=>el('article',{class:'demo-card'},el('h3',{},d.name),el('p',{class:'muted'},d.description||d.steps.length+' steps'),
        el('div',{class:'actions'},btn('Choose',()=>{pickDemo=d.id;render();window.scrollTo({top:0,behavior:'smooth'});}),
          can('demos.edit')?btn('Edit',()=>editDemo(d)):null,
          can('demos.delete')?btn('Delete',()=>{if(confirm('Delete '+d.name+'? Uploaded assets remain.'))action('library/'+d.id,'DELETE');},'danger'):null)))):
        el('div',{class:'empty'},'Create your first demo using existing Xos media or upload videos, panoramas, and GLB scenes.')),
    panel('setup','Set up a Quest','',
      el('ol',{},el('li',{},'Install Xos Quest on each developer-mode headset, then open it from Library → Unknown Sources.'),
        el('li',{},'Choose Add headset under Headsets, match its code, then tap Enter VR in the headset.'),
        el('li',{},'Leave it on Wi-Fi and charging: it downloads every demo file in the background.')),
      el('p',{class:'muted'},'Previews show the current rendered app view about every two seconds. They do not capture the Quest home screen, other apps, or the physical room. No view history is recorded.'),
      el('div',{class:'actions'},el('a',{class:'button',href:'/quest/xos-quest.apk'},'Download Quest app (APK)'),
        el('a',{class:'button',href:'/quest/install'},'Installation guide'),
        el('a',{class:'button',href:'/quest',target:'_blank',rel:'noopener'},'Open Quest viewer ↗'),
        !new URLSearchParams(location.search).has('embed')?el('a',{class:'button',href:'/app/#/admin/demos',target:'_top'},'Open in Xos Admin'):null)));
  root.replaceChildren(shell,watching?watchView():'');
  updatePreviews();
}
async function recover(ids,actionName){
  if(!ids.length)return;
  if(actionName==='reload_app'&&!confirm('Reload Xos on '+ids.length+' headset(s)? Their current app view will briefly close. If Meta requests it, tap Enter VR again in the headset.'))return;
  if(await action('recovery','POST',{unit_ids:ids,action:actionName}))notice('Check each headset card for confirmation. Queued actions expire in 20 seconds.');
}
function headsetSettings(ids){
  const unit=data.units.find(u=>u.id===ids[0]);if(!unit)return;
  const values=structuredClone(unit.settings.values);
  const d=document.createElement('dialog');d.className='editor quest-tools';d.setAttribute('aria-label','Headset tools and settings');document.body.append(d);
  d.addEventListener('close',()=>{d.remove();refresh(true);});
  const status=el('p',{role:'status',class:'muted'},ids.length===1?'Changes apply when this headset checks in.':'Starting with '+unit.name+' settings. Apply replaces all app settings on the selected headsets.');
  const fields=[];
  for(const [key,label,min,max,step] of [['volume','App audio volume',0,1,.05],['view_distance','Viewing distance (metres)',1,8,.25],['scene_scale','3D scene scale',.25,3,.25],['render_scale','Render quality (next VR launch)',.5,1.5,.25]]){
    const number=el('input',{type:'number',min,max,step,value:values[key],required:true,'aria-label':label,oninput:e=>values[key]=Number(e.target.value)});
    fields.push(el('label',{},el('span',{},label),number));
  }
  for(const [key,label] of [['muted','Mute app audio'],['show_grid','Show floor grid'],['allow_teleport','Allow controller teleport'],['preview_enabled','Share app preview with permitted presenters']]){
    fields.push(el('label',{class:'row'},el('input',{type:'checkbox',checked:values[key],onchange:e=>values[key]=e.target.checked}),el('span',{},label)));
  }
  const form=el('form',{class:'form',onsubmit:async e=>{
    e.preventDefault();if(me?.preview){status.textContent='Exit read-only preview before saving.';return;}
    const save=form.querySelector('button[type=submit]');save.disabled=true;
    try{await api('settings','PUT',{unit_ids:ids,settings:values});status.textContent='Saved. Check headset cards for Settings applied.';await refresh(true);}
    catch(error){status.textContent=error.message;}finally{save.disabled=false;}
  }},...fields,el('button',{type:'submit',class:'primary'},'Apply to '+ids.length+' headset(s)'));
  d.append(el('header',{},el('h2',{},ids.length===1?unit.name:'Tools for '+ids.length+' headsets'),btn('Close',()=>d.close())),
    can('demos.recover')?el('section',{},el('h3',{},'Quick recovery'),el('div',{class:'actions'},
      ...[['recenter','Reset app position'],['reload_scene','Retry current scene'],['reload_app','Reload Xos app'],['identify','Show headset name']].map(([a,label])=>btn(label,()=>recover(ids,a)))),
      el('p',{class:'muted'},'Wake the headsets and keep Xos open. Recenter changes the scene position; it does not reset the physical safety boundary.')):null,
    can('demos.settings')?form:null,status,
    el('p',{class:'muted'},'Audio controls affect Xos media volume. Wi-Fi, headset volume, boundary setup, power and OS restart use Meta’s headset settings. If Xos is fully closed, reopen it in the headset.'));
  d.showModal();
}
async function start(auto_start){
  const ids=[...selected].filter(id=>data.units.some(u=>u.id===id&&u.enabled&&u.paired));
  await action('sessions','POST',{demo_id:pickDemo,unit_ids:ids,auto_start});
}
// Fetches every headset's latest view at once. Each frame is fetched only when it is new (ETag),
// and swapped in only once it has decoded, so views update cleanly without flicker.
const previewTags=new Map();
let previewing=false;
async function updatePreviews(force=false){
  if(previewing)return;previewing=true;
  try{
    await Promise.all([...new Set([...shown(),...(watching?watchUnits():[])])].map(async u=>{
      if(!u.settings.values.preview_enabled){if(previewURLs.has(u.id))URL.revokeObjectURL(previewURLs.get(u.id));previewURLs.delete(u.id);previewTimes.delete(u.id);previewTags.delete(u.id);return;}
      if(!can('demos.previews')||!u.online||!u.preview_at||(!force&&previewTimes.get(u.id)===u.preview_at))return;
      previewTimes.set(u.id,u.preview_at);
      try{
        const tag=previewTags.get(u.id);
        const r=await userFetch('/api/demos/units/'+u.id+'/preview',{cache:'no-store',headers:tag?{'If-None-Match':tag}:{}});
        if(r.status!==200||!can('demos.previews')||!can('demos.view'))return;
        previewTags.set(u.id,r.headers.get('ETag')||'');
        const url=URL.createObjectURL(await r.blob());
        const probe=new Image();probe.src=url;await probe.decode().catch(()=>{});
        if(previewURLs.has(u.id))URL.revokeObjectURL(previewURLs.get(u.id));
        previewURLs.set(u.id,url);
        for(const img of document.querySelectorAll('img[data-preview="'+u.id+'"]')){img.src=url;img.classList.remove('hide');}
      }catch(_){previewTimes.delete(u.id);}
    }));
  }finally{previewing=false;}
}
// While the full-screen view is open, headsets send a view every second; fetch at the same pace.
setInterval(()=>{if(watching&&data&&can('demos.previews'))updatePreviews(true);},1000);
async function refresh(force=false){
  if(polling)return;polling=true;
  try{
    if(Date.now()-meAt>10000){me=await whoAmI(true);meAt=Date.now();}
    if(!can('demos.previews')){for(const url of previewURLs.values())URL.revokeObjectURL(url);previewURLs.clear();previewTimes.clear();}
    if(!can('demos.view')){if(dialog.open)dialog.close();document.getElementById('pairing').close();root.replaceChildren(el('section',{class:'gate'},el('h1',{},'Demo Studio access'),el('p',{},'This user does not have permission to open Demo Studio.')));return;}
    data=await api('studio');
    pairing.update(data);
    selected=new Set([...selected].filter(id=>data.units.some(u=>u.id===id&&u.paired&&u.enabled)));
    if(!dialog.open&&(force||!document.activeElement?.matches('input,select,textarea')))render();
    else if(!dialog.open)updatePreviews();
  }catch(e){pairing.unavailable(e.message);notice(e.message);}finally{polling=false;}
}
function input(label,value,change,type='text'){
  return el('label',{},el('span',{},label),el('input',{type,value,oninput:e=>change(e.target.value)}));
}
function editDemo(d){
  draft=d?structuredClone(d):{id:'demo-'+crypto.randomUUID().slice(0,8),name:'',description:'',steps:[]};
  renderEditor();dialog.showModal();
}
function renderEditor(){
  const steps=el('div',{class:'steps'});
  draft.steps.forEach((s,i)=>{
    const asset=data.assets.find(a=>a.id===s.asset_id);
    const projection=el('select',{'aria-label':'Projection for step '+(i+1),onchange:e=>s.projection=e.target.value},
      ...['flat','360','180'].map(p=>el('option',{value:p,selected:s.projection===p},p==='flat'?'Screen':p+'° immersive')));
    const stereo=el('select',{'aria-label':'Stereo layout for step '+(i+1),onchange:e=>s.stereo=e.target.value},
      ...[['mono','Mono'],['sbs','Stereo · side by side'],['top-bottom','Stereo · top / bottom']].map(([v,t])=>el('option',{value:v,selected:s.stereo===v},t)));
    steps.append(el('article',{class:'step'},row(el('b',{class:'grow'},'Step '+(i+1)+' · '+(asset?.name||'Missing asset')),
      btn('↑',()=>{[draft.steps[i-1],draft.steps[i]]=[draft.steps[i],draft.steps[i-1]];renderEditor();},'',i===0),
      btn('↓',()=>{[draft.steps[i+1],draft.steps[i]]=[draft.steps[i],draft.steps[i+1]];renderEditor();},'',i===draft.steps.length-1),
      btn('Remove',()=>{draft.steps.splice(i,1);renderEditor();},'danger')),
      input('Step title',s.title,v=>s.title=v),
      asset?.kind==='model'?el('small',{},'3D scene · automatically fitted to a comfortable viewing scale'):row(projection,stereo),
      input('Presenter note (optional)',s.note||'',v=>s.note=v)));
  });
  const file=el('input',{disabled:!can('demos.upload'),type:'file',multiple:true,accept:'.mp4,.webm,.jpg,.jpeg,.png,.webp,.glb','aria-label':'Upload demo assets',
    onchange:e=>upload([...e.target.files])});
  const assets=el('div',{class:'asset-list'},...data.assets.map(a=>el('div',{class:'asset'},
    el('div',{},el('b',{},a.name),el('small',{},a.kind==='model'?'3D scene · GLB':a.kind==='video'?'Video':'Image / panorama')),
    can('demos.edit')?btn('+ Step',()=>{draft.steps.push({asset_id:a.id,title:a.name,projection:'flat',stereo:'mono',note:''});renderEditor();},'',draft.steps.length>=60):null,
    a.id.startsWith('xr-')&&can('demos.delete')?btn('×',async()=>{if(!confirm('Delete this uploaded asset?'))return;try{await api('assets/'+a.id,'DELETE');data=await api('studio');renderEditor();}catch(e){notice(e.message);}},'danger'):null)));
  const form=el('div',{class:'form'},input('Demo name',draft.name,v=>draft.name=v),input('Description (optional)',draft.description,v=>draft.description=v));
  const save=btn('Save demo',async()=>{
    if(me?.preview){notice('Preview is read-only. Exit preview to save changes.');return;}
    if(uploading){notice('Wait for the upload to finish.');return;}
    if(!draft.name.trim()||!draft.steps.length){notice('Give the demo a name and add at least one step.');return;}
    try{await api('library/'+draft.id,'PUT',{name:draft.name.trim(),description:draft.description,steps:draft.steps});dialog.close();draft=null;await refresh(true);notice('Demo saved');}catch(e){notice(e.message);}
  },'primary',uploading);
  dialog.replaceChildren(el('header',{},el('h2',{},can('demos.edit')?'Build your walkthrough':'Media & scenes'),btn('Close',()=>closeEditor())),can('demos.edit')?form:null,
    el('div',{class:can('demos.edit')?'editor-layout':'media-manager'},can('demos.edit')?el('section',{},el('h3',{},'Demo sequence'),steps,draft.steps.length?null:el('p',{class:'empty'},'Add a step from the library.')):null,
      el('section',{},el('h3',{},'Media & scenes'),file,el('p',{class:'muted'},'MP4 / WebM up to 1 GB; self-contained GLB up to 200 MB. Export GLB without Draco, Meshopt or KTX2 compression.'),
        el('div',{id:'upload-status'}),assets)),el('div',{class:'actions',style:'margin-top:20px'},can('demos.edit')?save:null,btn('Cancel',()=>closeEditor())));
}
function closeEditor(){if(uploading){notice('Wait for the upload to finish.');return;}dialog.close();}
dialog.addEventListener('cancel',e=>{if(uploading){e.preventDefault();notice('Wait for the upload to finish.');}});
// Sends a file in pieces the server asks for, so large videos get past upload size limits and a
// dropped connection resumes from the last piece the server has.
async function sendFile(file,show){
  const start=await api('uploads','POST',{filename:file.name,name:file.name.replace(/\.[^.]+$/,''),size:file.size});
  let received=0;
  while(received<file.size){
    for(let tries=1;;tries++){
      let r;
      try{r=await userFetch('/api/demos/uploads/'+start.upload+'?offset='+received,{method:'PUT',headers:{'Content-Type':'application/octet-stream'},body:file.slice(received,received+start.chunk)});}
      catch(_){r=null;}
      if(r?.ok){received=(await r.json()).received;break;}
      if(r&&r.status<500){const j=await r.json().catch(()=>({}));throw Error(j.detail||'Upload failed ('+r.status+')');}
      if(tries>=6)throw Error('Upload interrupted. Check your network and upload it again.');
      await new Promise(done=>setTimeout(done,2000*tries));
    }
    show(Math.floor(received/file.size*100)+'%');
  }
  return api('uploads/'+start.upload+'/done','POST');
}
async function upload(files){
  if(uploading||!can('demos.upload')||me?.preview)return;uploading=true;
  for(const file of files){
    const status=document.getElementById('upload-status');
    try{await sendFile(file,t=>{if(status?.isConnected){status.className='upload-status';status.textContent=file.name+' · '+t;}});}
    catch(e){notice(file.name+': '+e.message);}
  }
  uploading=false;try{data=await api('studio');if(dialog.open)renderEditor();}catch(e){notice(e.message);}
}
dialog.addEventListener('close',()=>{draft=null;refresh(true);});
document.addEventListener('keydown',e=>{
  if(e.code!=='Space'||e.target.closest?.('input,textarea,select,button,summary')||document.querySelector('dialog[open]'))return;
  const s=data&&activeSessions()[0];
  if(!s||s.preparing||!can('demos.control'))return;
  e.preventDefault();command(s,s.playing?'pause':'play');
});
window.addEventListener('xos-me',e=>{me=e.detail;meAt=Date.now();refresh(true);});
async function init(){
  me=await whoAmI(true);
  if(!can('demos.view')){
    root.replaceChildren(el('section',{class:'gate'},el('h1',{},'Xos Demo Studio'),
      el('p',{},me.email?'Your Xos role does not include the demo dashboard. Ask an Xos Admin for access.':'Sign in with your organization account to run headset demos.'),
      me.email?el('a',{href:'/app/'},'Back to Xos'):el('a',{class:'button',href:signInUrl('/app/demos/')},'Sign in with Google')));
    return;
  }
  await refresh(true);setInterval(()=>refresh(),2000);
}
init().catch(e=>{root.replaceChildren(el('section',{class:'gate'},el('h2',{},'Could not open Demo Studio'),el('p',{},e.message),btn('Try again',()=>location.reload())));});
