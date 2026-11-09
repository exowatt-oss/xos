import {setupChecks, freeSlots, siteChoices} from './pairing-state.js';

export function createPairing({dialog, api, can, previewing, changed, useHeadset}) {
  let data=null, office='', unitId='', code='', connected=false, busy=false, error='', connectionError='';
  const url=location.origin+'/quest';
  const el=(tag,attrs={},...children)=>{
    const n=document.createElement(tag);
    for(const [k,v] of Object.entries(attrs)){
      if(k.startsWith('on'))n.addEventListener(k.slice(2),v);
      else if(['disabled','selected','value'].includes(k))n[k]=v;
      else n.setAttribute(k,v);
    }
    for(const c of children.flat())if(c!=null)n.append(c instanceof Node?c:document.createTextNode(String(c)));
    return n;
  };
  const button=(label,run,cls='',disabled=false)=>el('button',{type:'button',class:cls,onclick:run,disabled},label);
  const unit=()=>data?.units.find(u=>u.id===unitId);
  const allowed=()=>can('demos.pair')&&!previewing()&&!connectionError;
  function chooseOffice(next){office=next;unitId=freeSlots(data.units,office)[0]?.id||'';render();}
  function open(id,preferredOffice){
    if(!data)return;
    const chosen=data.units.find(u=>u.id===id);
    const sites=siteChoices(data.units).map(s=>s.id);
    office=chosen?.site || (sites.includes(preferredOffice)?preferredOffice:sites.find(s=>freeSlots(data.units,s).length)||sites[0]||'');
    unitId=chosen?.id || freeSlots(data.units,office)[0]?.id || '';
    connected=Boolean(chosen?.paired);code='';error='';busy=false;render();dialog.showModal();
  }
  function render(){
    const u=unit();
    dialog.replaceChildren(el('header',{},el('div',{},el('p',{class:'eyebrow'},'Quest setup'),el('h2',{},connected?'Finish headset setup':'Add a headset')),button('Close',()=>dialog.close(),'',busy)));
    if(connected){
      dialog.append(el('div',{class:'pair-success'},el('h3',{},u?.name||'Headset paired'),el('p',{},'The code is accepted. Keep the viewer open and tap Enter VR in the headset.')),
        el('div',{id:'pair-checks','aria-live':'polite'}),el('div',{id:'pair-help'}),
        el('div',{class:'actions'},button('Use this headset',()=>{useHeadset(unitId);dialog.close();},'primary'),
          button('Add another headset',()=>{connected=false;code='';error='';unitId=freeSlots(data.units,office)[0]?.id||'';render();},'',!allowed()),button('Done',()=>dialog.close())));
      updateChecks();return;
    }
    const officeSelect=el('select',{'aria-label':'Site',disabled:busy,onchange:e=>chooseOffice(e.target.value)},
      ...siteChoices(data.units).map(s=>el('option',{value:s.id,selected:s.id===office},s.label)));
    const slots=freeSlots(data.units,office);
    const slotSelect=el('select',{'aria-label':'Headset slot',disabled:busy,onchange:e=>{unitId=e.target.value;updateConnect();}},
      ...slots.map(u=>el('option',{value:u.id,selected:u.id===unitId},u.name)));
    if(!slots.length)slotSelect.append(el('option',{value:''},'No enabled slots available'));
    const input=el('input',{id:'pair-code',disabled:busy,type:'text',inputmode:'numeric',autocomplete:'off',maxlength:'9',placeholder:'123456','aria-label':'Six-digit headset code',value:code,
      oninput:e=>{code=e.target.value.replace(/\D/g,'').slice(0,6);e.target.value=code;error='';updateConnect();}});
    const submit=el('button',{type:'submit',id:'pair-connect',class:'primary',disabled:busy||!allowed()||!unitId||code.length!==6},busy?'Connecting…':'Connect headset');
    dialog.append(el('ol',{class:'pair-steps'},
      el('li',{},el('h3',{},'Choose the site'),el('div',{class:'row'},officeSelect,slotSelect)),
      el('li',{},el('h3',{},'Open the Xos Quest app'),el('p',{},'Install the APK once, connect to office Wi-Fi, then open Xos Quest from Library → Unknown Sources. The pairing code appears inside the app.'),
        el('div',{class:'actions'},el('a',{class:'button',href:'/quest/xos-quest.apk'},'Download Quest APK'),el('a',{class:'button',href:'/quest/install',target:'_blank',rel:'noopener'},'Install guide')),
        el('p',{},'Browser fallback / desktop preview:'),el('div',{class:'pair-link'},el('strong',{},url),button('Copy link',async()=>{
          try{await navigator.clipboard.writeText(url);const n=dialog.querySelector('#pair-copy');if(n)n.textContent='Link copied';}
          catch(_){const n=dialog.querySelector('#pair-copy');if(n)n.textContent='Select and copy the address above.';}
        })),el('small',{id:'pair-copy','aria-live':'polite'},'No Google sign-in needed on the headset.')),
      el('li',{},el('h3',{},'Enter the code shown in the headset'),el('p',{},'Match the six-digit code, then connect. Xos will check the connection automatically.'),
        el('form',{class:'pair-form',onsubmit:e=>{e.preventDefault();connect();}},input,submit),
        el('p',{id:'pair-error',class:'error',role:'status'},error),el('div',{id:'pair-pending'}))));
    if(!slots.length)dialog.append(el('p',{class:'error'},'All enabled slots at this site are used. Choose another site, or close setup and enable or unpair a slot first.'));
    if(previewing())dialog.append(el('p',{class:'muted'},'This is a read-only preview. Exit preview in Xos before connecting a headset.'));
    updatePending();
  }
  function updateConnect(){
    const b=dialog.querySelector('#pair-connect');if(b)b.disabled=busy||!allowed()||!unitId||code.length!==6;
    const n=dialog.querySelector('#pair-error');if(n){n.textContent=connectionError||error;n.hidden=!(connectionError||error);}
  }
  function updatePending(){
    const container=dialog.querySelector('#pair-pending');if(!container)return;
    const pending=data.pending.filter(p=>data.now-p.created<900).slice(0,6);
    container.replaceChildren();
    if(pending.length)container.append(el('p',{class:'muted'},'Headsets waiting for setup — choose the code you see:'),
      el('div',{class:'actions'},...pending.map(p=>button(p.code.slice(0,3)+' '+p.code.slice(3),()=>{
        code=p.code;dialog.querySelector('#pair-code').value=code;error='';updateConnect();dialog.querySelector('#pair-connect').focus();
      },'',busy))));
    updateConnect();
  }
  async function connect(){
    if(busy||!allowed()||!unitId||code.length!==6)return;
    busy=true;error='';render();
    try{
      const paired=await api('pair','POST',{unit_id:unitId,code});
      data={...data,units:data.units.map(u=>u.id===unitId?{...u,...paired,paired:true,online:false,telemetry:{},preview_at:0}:u)};
      connected=true;await changed();
    }catch(e){error=e.message;}
    finally{busy=false;render();}
  }
  function updateChecks(){
    const u=unit(), container=dialog.querySelector('#pair-checks'),help=dialog.querySelector('#pair-help');
    if(!container||!help)return;
    if(!u?.paired){container.replaceChildren(el('p',{class:'error'},'This slot has been unpaired. Close setup and connect it again.'));help.replaceChildren();return;}
    const checks=setupChecks(connectionError?{...u,online:false}:u,data.enabled,can('demos.previews'),data.now);
    container.replaceChildren(...checks.map(c=>el('div',{class:'pair-check '+(c.ok?'complete':'waiting')},el('span',{},c.ok?'✓':'○'),el('span',{},c.label),el('small',{},c.ok?'Confirmed':'Waiting'))));
    const ready=checks.every(c=>c.ok);
    const tip=ready?'Ready for a demo. Choose Use this headset, pick a walkthrough, and Start selected.':
      !u.online?'Open '+url+' in the headset and keep it awake. If it still does not connect, reload Quest Browser and check the local Wi-Fi.':
      u.telemetry?.visibility==='hidden'?'Bring the Xos viewer back to the front in Quest Browser.':
      u.telemetry?.vr_supported===false?'This browser is a desktop preview. Open the link on a Quest and tap Enter VR there.':
      !u.telemetry?.immersive?'In the headset, tap the large Enter VR button once. The presenter can then control the demo.':
      !data.enabled||!u.enabled?'Enable demo mode and this headset to present.':'Waiting for the first app-view image. Keep the headset awake.';
    help.replaceChildren(el('p',{class:ready?'pair-ready':'pair-tip',role:'status'},connectionError?'Waiting for Xos: '+connectionError:tip));
    if(u.telemetry?.error)help.append(el('p',{class:'error'},u.telemetry.error));
    if(can('demos.enable')&&!previewing()){
      if(!data.enabled)help.append(button('Enable demo mode',()=>enable('enabled','PUT',{enabled:true}),'primary'));
      if(!u.enabled)help.append(button('Enable this headset',()=>enable('units/'+u.id,'PATCH',{enabled:true}),'primary'));
    }
  }
  async function enable(path,method,body){try{await api(path,method,body);await changed();}catch(e){error=e.message;dialog.querySelector('#pair-help')?.append(el('p',{class:'error'},error));}}
  dialog.addEventListener('cancel',e=>{if(busy)e.preventDefault();});
  return {open,update(next){data=next;connectionError='';if(dialog.open){connected?updateChecks():updatePending();}},unavailable(message){connectionError=message;if(dialog.open){connected?updateChecks():updateConnect();}}};
}
