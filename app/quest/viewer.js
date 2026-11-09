import * as THREE from 'three';
import {GLTFLoader} from 'three/addons/loaders/GLTFLoader.js';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
import {playback,correction,clockOffset} from './sync.js';
import {journal,recenterPose} from './recovery.js';
import {createOffline} from './offline.js';

const $=id=>document.getElementById(id);
const video=$('media');
let identity, current=null, readyToken='', loadingToken='', offset=0, assetRoot=null, mixer=null;
let state='standby', error='', live=true, beatBusy=false, previewBusy=false, generation=0, lastPreview=0;
let lastPlayError=0, lastVideoStart=0, drag=null, yaw=0, pitch=0, battery=null, immersiveSupported=false;
const STORE='xos-quest-identity-v1';
const installed=typeof window.getDigitalGoodsService!=='undefined';
let commands;
let settings={volume:.7,muted:false,scene_scale:1,view_distance:3,render_scale:1.25,show_grid:true,allow_teleport:true,preview_enabled:true},settingsRevision=0;
let identifyUntil=0,recenterFrames=0,localURL='',streaming=false;
// Quest headsets keep every demo file on the headset and play it from there; other browsers stream.
const offline=navigator.storage?.getDirectory&&(installed||/Quest|OculusBrowser/.test(navigator.userAgent))
  ?createOffline({storage:localStorage,directory:()=>navigator.storage.getDirectory(),fetcher:(...a)=>fetch(...a),estimate:()=>navigator.storage.estimate()})
  :{enabled:false,cached:()=>[],progress:()=>null,error:()=>'',sync:async()=>{},file:async()=>null};
try{
  commands=journal(localStorage);commands.pendingReload();
  identity=JSON.parse(localStorage.getItem(STORE)||'null');
  if(!identity?.id||!identity?.key){
    identity={id:'quest-'+crypto.randomUUID().slice(0,12),key:Array.from(crypto.getRandomValues(new Uint8Array(24)),b=>b.toString(16).padStart(2,'0')).join('')};
    localStorage.setItem(STORE,JSON.stringify(identity));
  }
}catch(_){fatal('Allow website storage in Quest Browser, then reload this app.');throw Error('Headset storage unavailable');}
const headers=()=>({'X-Xos-Quest':identity.id,'X-Xos-Key':identity.key});
let renderer;
try{renderer=new THREE.WebGLRenderer({antialias:true,alpha:false});}
catch(e){fatal('This browser could not start the 3D viewer. Enable WebGL or use Quest Browser.');throw e;}
renderer.setPixelRatio(Math.min(devicePixelRatio,1.5));
renderer.setSize(innerWidth,innerHeight);
renderer.xr.enabled=true;
// Full resolution across the whole view: three.js defaults to maximum foveation, which softens
// everything away from the centre, and in a 360 scene people look everywhere.
renderer.xr.setFoveation(0);
renderer.outputColorSpace=THREE.SRGBColorSpace;
$('stage').append(renderer.domElement);
const scene=new THREE.Scene();
scene.background=new THREE.Color('#102d32');
const camera=new THREE.PerspectiveCamera(65,innerWidth/innerHeight,.05,120);
camera.position.set(0,1.6,3);
const rig=new THREE.Group();rig.add(camera);scene.add(rig);
const orbit=new OrbitControls(camera,renderer.domElement);orbit.target.set(0,1.2,0);orbit.enableDamping=true;orbit.maxDistance=12;orbit.minDistance=.3;
scene.add(new THREE.HemisphereLight(0xe7fff2,0x506060,2.5));
const light=new THREE.DirectionalLight(0xffffff,3);light.position.set(3,6,4);scene.add(light);
const floor=new THREE.Mesh(new THREE.PlaneGeometry(50,50),new THREE.MeshStandardMaterial({color:0x173e40,roughness:.9}));
floor.rotation.x=-Math.PI/2;floor.position.y=-.01;scene.add(floor);
const grid=new THREE.GridHelper(50,50,0x40706b,0x244c4c);grid.position.y=.002;scene.add(grid);
const boardCanvas=document.createElement('canvas');boardCanvas.width=1024;boardCanvas.height=256;
const boardTexture=new THREE.CanvasTexture(boardCanvas);boardTexture.colorSpace=THREE.SRGBColorSpace;
const board=new THREE.Mesh(new THREE.PlaneGeometry(3.8,.95),new THREE.MeshBasicMaterial({map:boardTexture,transparent:true,depthTest:false}));
board.position.set(0,1.6,-2.4);board.renderOrder=5;scene.add(board);
function message(title,detail=''){
  const c=boardCanvas.getContext('2d');c.clearRect(0,0,1024,256);
  c.fillStyle='rgba(9,34,39,.9)';c.fillRect(0,0,1024,256);
  c.textAlign='center';c.fillStyle='#d5f7df';c.font='600 44px sans-serif';c.fillText(title.slice(0,42),512,97,960);
  c.fillStyle='#aecbc2';c.font='27px sans-serif';c.fillText(detail.slice(0,85),512,163,960);boardTexture.needsUpdate=true;
  $('status').textContent=detail||title;
}
message('Xos Experiences','Connecting to your presenter');
function fatal(t){$('status').textContent=t;$('retry').hidden=false;$('retry').onclick=()=>location.reload();}
function report(t){error=t;state='error';board.visible=true;message('Check this headset',t);$('welcome').hidden=false;$('retry').hidden=false;$('retry').onclick=()=>{if(current?.session){clearContent();loadStep(current.session);}else location.reload();};}
function cleanup(root){
  root?.traverse(o=>{
    o.geometry?.dispose();
    for(const m of (Array.isArray(o.material)?o.material:[o.material]))if(m){
      for(const value of Object.values(m))if(value?.isTexture)value.dispose();
      m.dispose();
    }
  });
  if(root)scene.remove(root);
}
function clearContent(){
  generation++;video.pause();video.removeAttribute('src');video.load();cleanup(assetRoot);assetRoot=null;mixer=null;
  if(localURL)URL.revokeObjectURL(localURL);localURL='';streaming=false;
  readyToken='';loadingToken='';state='standby';error='';floor.visible=true;grid.visible=settings.show_grid;
}
function frame(media,projection,stereo){
  const group=new THREE.Group();
  const layouts=stereo==='mono'?[{eye:0,repeat:[1,1],offset:[0,0]}]:stereo==='sbs'?
    [{eye:1,repeat:[.5,1],offset:[0,0]},{eye:2,repeat:[.5,1],offset:[.5,0]}]:
    [{eye:1,repeat:[1,.5],offset:[0,.5]},{eye:2,repeat:[1,.5],offset:[0,0]}];
  // Both eyes share one texture source, so each video frame is copied to the GPU once.
  const base=media instanceof HTMLVideoElement?new THREE.VideoTexture(media):new THREE.Texture(media);
  for(const layout of layouts){
    const texture=layout===layouts[0]?base:base.clone();
    texture.colorSpace=THREE.SRGBColorSpace;texture.repeat.set(...layout.repeat);texture.offset.set(...layout.offset);texture.needsUpdate=true;
    const geometry=projection==='flat'?new THREE.PlaneGeometry(4.8,2.7):
      new THREE.SphereGeometry(45,128,64,projection==='180'?Math.PI:0,projection==='180'?Math.PI:Math.PI*2);
    if(projection!=='flat')geometry.scale(-1,1,1);
    const mesh=new THREE.Mesh(geometry,new THREE.MeshBasicMaterial({map:texture,toneMapped:false}));
    mesh.position.set(0,1.6,projection==='flat'?-1:0);
    if(layout.eye)mesh.layers.set(layout.eye);
    group.add(mesh);
  }
  // Mirror/desktop view uses the left eye for stereo media.
  camera.layers.enable(1);
  return group;
}
async function loadStep(s){
  const token=s.step_token;
  if(token===loadingToken)return;
  clearContent();loadingToken=token;state='loading';
  const gen=generation;
  message('Loading '+s.step.title,'Preparing the next part of your demo…');
  $('step-label').textContent=s.demo_name+' · '+(s.step_index+1)+' / '+s.steps+' · '+s.step.title;
  if(!s.asset){report('This demo asset is missing. Ask your presenter to edit the demo.');return;}
  const projection=s.asset.kind==='model'?'flat':s.step.projection;
  floor.visible=projection==='flat';grid.visible=floor.visible&&settings.show_grid;
  board.position.set(0,2.9,-1.5);
  orbit.enabled=!renderer.xr.isPresenting&&projection==='flat';
  if(!renderer.xr.isPresenting){rig.position.set(0,0,0);camera.position.set(0,1.6,projection==='flat'?3:0);orbit.target.set(0,1.6,projection==='flat'?0:-1);camera.lookAt(orbit.target);}
  try{
    let object;
    // A Quest plays every scene from its own saved copy, so playback never waits on the network.
    // It reports ready only once the file is saved; it streams only if it can't save files.
    let file=await offline.file(s.asset);
    while(!file&&offline.enabled&&!offline.error()){
      const p=offline.progress();
      message('Saving '+s.step.title,p?.id===s.asset.id&&p.total?'Downloading to this headset · '+Math.floor(100*p.received/p.total)+'%':'Waiting to download to this headset…');
      await new Promise(done=>setTimeout(done,1000));
      if(gen!==generation)return;
      file=await offline.file(s.asset);
    }
    if(gen!==generation)return;
    if(file)localURL=URL.createObjectURL(file);
    const src=localURL||s.asset.url;streaming=!localURL;
    if(s.asset.kind==='model'){
      const gltf=await new GLTFLoader().loadAsync(src);
      if(gen!==generation){cleanup(gltf.scene);return;}
      object=gltf.scene;
      const box=new THREE.Box3().setFromObject(object), size=box.getSize(new THREE.Vector3());
      const extent=Math.max(size.x,size.y,size.z);
      if(!Number.isFinite(extent)||extent<=0){cleanup(object);throw Error('This scene has no visible geometry.');}
      object.scale.multiplyScalar(3/extent);
      const fitted=new THREE.Box3().setFromObject(object),center=fitted.getCenter(new THREE.Vector3());
      object.position.sub(new THREE.Vector3(center.x,fitted.min.y,center.z));
      if(gltf.animations.length){mixer=new THREE.AnimationMixer(object);gltf.animations.forEach(a=>mixer.clipAction(a).play());}
    }else if(s.asset.kind==='video'){
      await new Promise((resolve,reject)=>{
        const clean=()=>{clearTimeout(timer);video.removeEventListener('loadeddata',done);video.removeEventListener('error',bad);};
        const done=()=>{clean();resolve();};
        const bad=()=>{clean();reject(Error('Video could not load. Use a supported MP4 / WebM export and check the network.'));};
        const timer=setTimeout(bad,60000);
        video.addEventListener('loadeddata',done,{once:true});video.addEventListener('error',bad,{once:true});
        video.src=src;video.load();
      });
      if(gen!==generation)return;
      object=frame(video,projection,s.step.stereo);
    }else{
      const img=new Image();img.crossOrigin='anonymous';img.src=src;await img.decode();
      if(gen!==generation)return;
      object=frame(img,projection,s.step.stereo);
    }
    if(gen!==generation){cleanup(object);return;}
    assetRoot=new THREE.Group();assetRoot.add(object);scene.add(assetRoot);applySettings();recenter();readyToken=token;state='ready';error='';
    $('welcome').hidden=true;$('retry').hidden=true;message(s.step.title,s.step.note||'Your presenter controls the next step.');
    board.visible=projection==='flat';lastVideoStart=0;
  }catch(e){if(gen===generation)report(e.message||'This scene could not load.');}
}
async function request(path,body){
  const sent=Date.now();
  const r=await fetch('/api/demos/'+path,{method:body===undefined?'GET':'POST',headers:{...headers(),...(body===undefined?{}:{'Content-Type':'application/json'})},...(body===undefined?{}:{body:JSON.stringify(body)}),cache:'no-store',signal:AbortSignal.timeout(5000)});
  const j=await r.json().catch(()=>({}));
  if(!r.ok){const err=Error(j.detail||'Connection failed');err.status=r.status;throw err;}
  if(j.now)offset=clockOffset(j.now,sent,Date.now());
  return j;
}
async function enroll(){
  const r=await fetch('/api/demos/enroll',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(identity),signal:AbortSignal.timeout(10000)});
  const j=await r.json();if(!r.ok)throw Error(j.detail||'Could not register headset');
  $('pair').hidden=j.paired;$('code').textContent=j.code?j.code.slice(0,3)+' '+j.code.slice(3):'';
  return j;
}
function applySettings(){
  video.volume=settings.volume;video.muted=settings.muted;
  grid.visible=floor.visible&&settings.show_grid;
  if(assetRoot&&current?.session?.asset?.kind==='model')assetRoot.scale.setScalar(settings.scene_scale);
  if(!renderer.xr.isPresenting){renderer.setPixelRatio(Math.min(devicePixelRatio,settings.render_scale));renderer.setSize(innerWidth,innerHeight);}
}
function recenter(){
  const s=current?.session,flat=!s||s.step.projection==='flat'||s.asset?.kind==='model';
  const distance=flat?settings.view_distance:0;
  if(renderer.xr.isPresenting){
    scene.updateMatrixWorld(true);
    const tracked=renderer.xr.getCamera(),head=new THREE.Vector3(),q=new THREE.Quaternion();
    tracked.getWorldPosition(head);tracked.getWorldQuaternion(q);
    const forward=new THREE.Vector3(0,0,-1).applyQuaternion(q);
    const pose=recenterPose(head.x,head.z,Math.atan2(-forward.x,-forward.z),rig.position.x,rig.position.z,rig.rotation.y,distance);
    rig.rotation.set(0,pose.yaw,0);rig.position.set(pose.x,0,pose.z);
  }else{
    rig.rotation.set(0,0,0);rig.position.set(0,0,0);yaw=pitch=0;
    camera.position.set(0,1.6,distance);orbit.target.set(0,1.6,flat?0:-1);camera.lookAt(orbit.target);
  }
}
async function recover(list){
  for(const command of list||[]){
    if(commands.seen(command.id)||command.expires<=Date.now()/1000+offset)continue;
    commands.start(command.id);
    try{
      if(command.action==='recenter')recenter();
      else if(command.action==='reload_scene'){
        if(current?.session){clearContent();await loadStep(current.session);if(error)throw Error(error);}
        else {clearContent();recenter();}
      }else if(command.action==='identify'){
        identifyUntil=Date.now()+7000;board.visible=true;board.position.set(0,1.6,-2.4);message(current?.unit?.name||'Xos Quest','Identified by your presenter');
      }else if(command.action==='reload_app'){
        commands.reloading(command.id);location.reload();return;
      }else throw Error('Update the headset app to use this action');
      commands.finish(command.id);
    }catch(e){commands.finish(command.id,'failed',String(e.message).slice(0,200));}
  }
}
function reconcile(j){
  current=j;
  if(j.settings&&j.settings.revision!==settingsRevision){
    const distanceChanged=settings.view_distance!==j.settings.values.view_distance;
    settings=j.settings.values;applySettings();if(distanceChanged)recenter();settingsRevision=j.settings.revision;
  }
  $('network').textContent=j.unit?j.unit.name:'Waiting for pairing';
  $('pair').hidden=Boolean(j.unit);
  $('welcome-title').textContent=j.unit?'Connected · '+j.unit.name:'Let’s connect your Quest.';
  $('setup-help').textContent=j.unit?'Tap Enter VR once. Your presenter can then start videos and guide the scenes from Xos.':'No Google sign-in needed here. Enter the code in Xos → Demo Studio → Add headset.';
  $('enter-lobby').hidden=!j.unit;
  updateVRButtons();
  $('retry').hidden=!error;
  if(!j.enabled||!j.session){
    if(assetRoot||loadingToken)clearContent();
    board.visible=true;$('welcome').hidden=false;
    if(Date.now()<identifyUntil)return;
    board.position.set(0,1.6,-2.4);
    message(j.unit?(j.enabled?'Ready · '+j.unit.name:'Demo mode is disabled'):'Pair code: '+$('code').textContent,
      j.unit?(renderer.xr.isPresenting?'Connected in VR · Your presenter can start the demo.':'Tap Enter VR to finish setup.'):'iPad / Mac: Xos → Demo Studio → Add headset');
    return;
  }
  loadStep(j.session);
}
async function beat(){
  if(beatBusy)return;beatBusy=true;
  try{
    const st=await request('headset/heartbeat',{
      app_mode:installed?'installed':'browser',settings_revision:settingsRevision,command_acks:commands.acks(),
      ready_token:readyToken,state,error,immersive:renderer.xr.isPresenting,vr_supported:immersiveSupported,
      visibility:document.visibilityState,position:Math.min(video.currentTime||0,86400),
      battery,orientation:camera.quaternion.toArray(),
      cached:offline.cached(),download:offline.progress(),storage_error:offline.error(),
      duration:Number.isFinite(video.duration)?Math.min(video.duration,86400):0,local:Boolean(localURL),
    });
    const reconnected=!live;live=true;reconcile(st);await recover(st.commands);
    const scene=st.session?.asset?.id;
    offline.sync([...(st.library||[])].sort((a,b)=>(b.id===scene)-(a.id===scene)),!(streaming&&state==='playing'));
    if(reconnected&&st.session&&readyToken===st.session.step_token){
      message(st.session.step.title,st.session.step.note||'Your presenter controls the next step.');
      board.visible=st.session.step.projection==='flat'||st.session.asset?.kind==='model';
    }
    if(!st.unit)await enroll();
  }catch(e){
    live=false;
    // A saved scene keeps playing on the session clock until the presenter's next command arrives.
    if(localURL&&readyToken&&readyToken===current?.session?.step_token)$('network').textContent='Connection lost · still playing from this headset';
    else{$('network').textContent='Connection lost · playback paused';board.visible=true;message('Connection lost','Playback paused. Reconnecting to your presenter…');}
    if(e.status===401){
      try{await enroll();current=null;clearContent();$('welcome').hidden=false;}catch(err){report(err.message);}
    }
  }finally{beatBusy=false;}
}
const mirrorCam=new THREE.PerspectiveCamera(75,16/9,.05,120);
mirrorCam.layers.enable(1);
const PW=1280,PH=720;
const target=new THREE.WebGLRenderTarget(PW,PH);
const pixels=new Uint8Array(PW*PH*4);
const previewCanvas=document.createElement('canvas');previewCanvas.width=PW;previewCanvas.height=PH;
const previewCtx=previewCanvas.getContext('2d');
const image=previewCtx.createImageData(PW,PH);
function preview(now){
  if(!settings.preview_enabled||previewBusy||!current?.unit||!live||now-lastPreview<(current.preview_every||2)*1000||document.visibilityState==='hidden')return;
  lastPreview=now;
  const tracked=renderer.xr.isPresenting?renderer.xr.getCamera():camera;
  tracked.matrixWorld.decompose(mirrorCam.position,mirrorCam.quaternion,mirrorCam.scale);
  mirrorCam.updateMatrixWorld();
  const enabled=renderer.xr.enabled,oldTarget=renderer.getRenderTarget();
  try{renderer.xr.enabled=false;renderer.setRenderTarget(target);renderer.render(scene,mirrorCam);}
  finally{renderer.setRenderTarget(oldTarget);renderer.xr.enabled=enabled;}
  previewBusy=true;
  // An asynchronous read, so a 720p frame never stalls what the wearer sees.
  renderer.readRenderTargetPixelsAsync(target,0,0,PW,PH,pixels).then(()=>{
    for(let y=0;y<PH;y++)image.data.set(pixels.subarray(y*PW*4,(y+1)*PW*4),(PH-1-y)*PW*4);
    previewCtx.putImageData(image,0,0);
    previewCanvas.toBlob(async blob=>{
      try{if(blob)await fetch('/api/demos/headset/preview',{method:'POST',headers:{...headers(),'Content-Type':'image/jpeg'},body:blob,signal:AbortSignal.timeout(5000)});}
      catch(_){}finally{previewBusy=false;}
    },'image/jpeg',.82);
  }).catch(()=>{previewBusy=false;});
}
function sync(){
  const s=current?.enabled?current.session:null;
  const plan=playback(s,Date.now()/1000+offset,video.duration);
  if(!s||(!live&&!localURL)||readyToken!==s.step_token){video.pause();return;}
  state=plan.playing?'playing':s.preparing?'ready':'paused';
  if(mixer)mixer.setTime(plan.position);
  if(s.asset?.kind!=='video')return;
  const fix=correction(video.currentTime,plan.position);
  if(fix.seek!==null&&video.readyState>=2)video.currentTime=fix.seek;
  video.playbackRate=fix.rate;
  if(plan.playing&&video.paused&&!video.ended&&Date.now()-lastVideoStart>1000){
    lastVideoStart=Date.now();video.play().then(()=>{error='';}).catch(()=>{
      error='Use Enter VR or press a controller trigger to enable video playback.';
      if(Date.now()-lastPlayError>8000){lastPlayError=Date.now();board.visible=true;message('Enable playback',error);}
    });
  }else if(!plan.playing)video.pause();
  if(plan.playing&&video.paused)state=error?'error':'ready';
  if(!error&&video.currentTime>=video.duration-.08)state='ended';
}
renderer.setAnimationLoop(now=>{
  sync();
  if(recenterFrames>0&&!--recenterFrames)recenter();
  if(identifyUntil&&Date.now()>identifyUntil){
    identifyUntil=0;
    if(current?.session){board.position.set(0,2.9,-1.5);message(current.session.step.title,current.session.step.note||'Your presenter controls the next step.');board.visible=current.session.step.projection==='flat'||current.session.asset?.kind==='model';}
  }
  if(!renderer.xr.isPresenting&&orbit.enabled)orbit.update();
  renderer.render(scene,camera);
  try{preview(now);}catch(e){error='App preview unavailable: '+e.message;}
});
const raycaster=new THREE.Raycaster(),temp=new THREE.Matrix4();
for(let i=0;i<2;i++){
  const controller=renderer.xr.getController(i);rig.add(controller);
  const line=new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(),new THREE.Vector3(0,0,-1)]),new THREE.LineBasicMaterial({color:0xb5efc3}));
  line.scale.z=7;controller.add(line);
  controller.addEventListener('select',()=>{
    if(current?.session?.playing)video.play().catch(()=>{});
    if(current?.session?.asset?.kind!=='model')return;
    temp.identity().extractRotation(controller.matrixWorld);
    raycaster.ray.origin.setFromMatrixPosition(controller.matrixWorld);raycaster.ray.direction.set(0,0,-1).applyMatrix4(temp);
    const modelHit=assetRoot&&raycaster.intersectObject(assetRoot,true)[0];
    if(modelHit){
      message(modelHit.object.name||current.session.step.title,'Scene object selected');
      return;
    }
    if(!settings.allow_teleport)return;
    const hit=raycaster.intersectObject(floor)[0];
    if(hit){const head=new THREE.Vector3().setFromMatrixPosition(camera.matrixWorld);rig.position.x+=hit.point.x-head.x;rig.position.z+=hit.point.z-head.z;}
  });
}
async function enter(){
  try{
    if(!immersiveSupported){message('Desktop preview','Drag to explore a 3D scene. Open in Quest Browser for VR.');return;}
    if(renderer.xr.isPresenting){await renderer.xr.getSession().end();return;}
    renderer.xr.setFramebufferScaleFactor(settings.render_scale);
    const session=await navigator.xr.requestSession('immersive-vr',{optionalFeatures:['local-floor']});
    renderer.xr.setReferenceSpaceType('local-floor');
    await renderer.xr.setSession(session);
  }catch(e){fatal('VR could not start: '+e.message);}
}
function updateVRButtons(){
  const title=renderer.xr.isPresenting?'Exit VR':immersiveSupported?'Enter VR':'Desktop preview';
  for(const id of ['enter','enter-lobby']){$(id).textContent=title;$(id).disabled=!current?.unit;}
}
$('enter').onclick=enter;$('enter-lobby').onclick=enter;
renderer.xr.addEventListener('sessionstart',()=>{
  orbit.enabled=false;recenterFrames=2;
  updateVRButtons();beat();
});
renderer.xr.addEventListener('sessionend',()=>{
  rig.position.set(0,0,0);camera.position.set(0,1.6,3);orbit.target.set(0,1.2,0);orbit.enabled=true;
  updateVRButtons();beat();
});
renderer.domElement.addEventListener('pointerdown',e=>{if(current?.session?.step.projection!=='flat')drag={x:e.clientX,y:e.clientY,yaw,pitch};});
window.addEventListener('pointerup',()=>drag=null);
window.addEventListener('pointermove',e=>{
  if(!drag||renderer.xr.isPresenting)return;
  yaw=drag.yaw-(e.clientX-drag.x)*.004;pitch=THREE.MathUtils.clamp(drag.pitch-(e.clientY-drag.y)*.004,-1.3,1.3);
  camera.quaternion.setFromEuler(new THREE.Euler(pitch,yaw,0,'YXZ'));
});
window.addEventListener('resize',()=>{camera.aspect=innerWidth/innerHeight;camera.updateProjectionMatrix();renderer.setSize(innerWidth,innerHeight);});
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden')video.pause();else beat();});
window.addEventListener('online',beat);
async function init(){
  immersiveSupported=Boolean(navigator.xr&&await navigator.xr.isSessionSupported('immersive-vr').catch(()=>false));
  updateVRButtons();
  if(installed&&immersiveSupported)await enter();
  navigator.storage?.persist?.().catch(()=>{});
  if(navigator.getBattery)navigator.getBattery().then(b=>{battery=b.level;b.addEventListener('levelchange',()=>battery=b.level);}).catch(()=>{});
  await enroll();await beat();setInterval(beat,1000);
}
init().catch(e=>report(e.message));
