// Only observed headset state can complete setup; a pairing response is not a heartbeat.
export function setupChecks(unit, enabled, showPreview, now) {
  const t = unit?.telemetry || {};
  const checks = [
    {id:'paired', label:'Paired to '+(unit?.name || 'this slot'), ok:Boolean(unit?.paired)},
    {id:'online', label:'Connected with the viewer open', ok:Boolean(unit?.online && t.visibility==='visible')},
    {id:'vr', label:'Entered VR on the headset', ok:Boolean(unit?.online && t.immersive)},
    {id:'enabled', label:'Demo mode and headset enabled', ok:Boolean(enabled && unit?.enabled)},
  ];
  if(showPreview) checks.push({id:'preview',label:'App view received',ok:Boolean(unit?.online && unit.preview_at && now-unit.preview_at<15)});
  return checks;
}
export const freeSlots = (units, office) => units.filter(u=>u.site===office&&!u.paired&&u.enabled);
export const siteChoices = (units) => [...new Set(units.map(u=>u.site).filter(Boolean))]
  .map((id, index) => ({id, label:`Site ${index + 1}`}));
