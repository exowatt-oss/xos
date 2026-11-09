// Small, durable execution journal. A lost heartbeat must never repeat a recenter/reload.
export function journal(storage) {
  const key='xos-quest-commands-v1';
  let entries;
  try{entries=JSON.parse(storage.getItem(key)||'[]');if(!Array.isArray(entries))entries=[];}catch{entries=[];}
  const save=()=>storage.setItem(key,JSON.stringify(entries.slice(-100)));
  return {
    seen:id=>entries.some(c=>c.id===id),
    start(id){entries.push({id,status:'running'});save();},
    finish(id,status='applied',error=''){const c=entries.find(c=>c.id===id);if(c){Object.assign(c,{status,error});save();}},
    acks:()=>entries.filter(c=>c.status==='applied'||c.status==='failed').slice(-20),
    pendingReload(){for(const c of entries)if(c.status==='reloading')c.status='applied';save();},
    reloading(id){const c=entries.find(c=>c.id===id);c.status='reloading';save();},
  };
}
// Rotate the app rig so the current head faces forward, then translate in the floor plane.
// This changes only app coordinates; it never changes Meta's physical boundary.
export function recenterPose(headX,headZ,headYaw,rigX,rigZ,rigYaw,distance) {
  const delta=-headYaw,c=Math.cos(delta),s=Math.sin(delta);
  const x=c*(headX-rigX)+s*(headZ-rigZ)+rigX;
  const z=-s*(headX-rigX)+c*(headZ-rigZ)+rigZ;
  return {x:rigX-x,z:rigZ+distance-z,yaw:rigYaw+delta};
}
