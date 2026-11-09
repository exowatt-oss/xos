// Admin → People & roles: people who have signed in or been assigned roles, the Xos role
// each has, the role matrix, department/team defaults, the change log, and Preview as.
// The matrix itself lives on the server (src/app/access.py); this page only shows it.
//
// Preview as: the server keeps the preview for this Admin and answers every request as the
// previewed role or person, refusing all writes (src/app/people.py). The page re-reads /api/me
// and renders from its permissions, so it looks exactly like that perspective.

import * as S from "../state.js";
import { ADMIN_TABS as VIEW_LIST, viewAllowed, visibleTabs } from "../access-views.js";
import { esc, dayShort, time } from "../ui.js";
import { userFetch, whoAmI } from "../api.js";

const fetch = userFetch;
let data = null; // GET /api/people
let roles = null; // GET /api/roles
let audit = null; // GET /api/people/audit
let section = "people";
let note = "";
const roleDrafts = new Map();
let permissionPerson = '', personDraft = null, permissionSaving = false;
let previewPerson = '', previewRole = 'member', previewView = 'demos';

const filters = { q: "", department: "", team: "", role: "", inactive: false };

const SOURCE = { super: "super admin", bootstrap: "starting Admin", person: "assigned", team: "team default", department: "department default", inactive: "inactive", default: "everyone" };

async function load() {
  try {
    const [p, r, a] = await Promise.all([
      fetch("/api/people", { cache: "no-store" }), fetch("/api/roles", { cache: "no-store" }), fetch("/api/people/audit?limit=200", { cache: "no-store" }),
    ]);
    if (p.ok) data = await p.json();
    if (r.ok) roles = await r.json();
    if (a.ok) audit = (await a.json()).items;
  } catch (_) { note = "Can't reach Xos right now."; }
  S.poke();
}

const roleName = (id) => roles?.roles.find((r) => r.id === id)?.name || id || "—";
const roleOpts = (current, blank, noAdmin = false) => `${blank ? `<option value="">${esc(blank)}</option>` : ""}${(roles?.roles || []).filter((r) => !noAdmin || r.id !== "admin").map((r) => `<option value="${r.id}" ${r.id === current ? "selected" : ""}>${esc(r.name)}</option>`).join("")}`;

/** Tell every view who we are now (after a preview starts or ends). */
export async function refreshWho() {
  const me = await whoAmI(true);
  window.dispatchEvent(new CustomEvent("xos-me", { detail: me }));
  syncPreviewBanner(me);
  document.querySelectorAll('iframe[title="Quest Demo Studio"]').forEach(f=>f.contentWindow?.dispatchEvent(new CustomEvent("xos-me",{detail:me})));
  S.poke();
  return me;
}

export async function startPreview(body, target = "") {
  const res = await fetch("/api/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).catch(() => null);
  if (!res?.ok) { note = res ? (await res.json()).detail : "Network error"; S.poke(); return; }
  try{sessionStorage.setItem("xos-preview-return", location.hash);}catch(_){}
  const me = await refreshWho();
  const first=visibleTabs(me.perms)[0];
  location.hash = target || (first ? "#/admin/"+first[0] : "#/control");
}

export async function exitPreview() {
  const res=await fetch("/api/preview", { method: "DELETE" }).catch(() => null);
  if(!res?.ok){note="Could not exit preview. Check the connection and try again.";S.poke();return;}
  data = roles = audit = null;
  await refreshWho();
  let back="";try{back=sessionStorage.getItem("xos-preview-return")||"";sessionStorage.removeItem("xos-preview-return");}catch(_){}
  location.hash = /^#\/admin\/[a-z-]+$/.test(back)?back:"#/admin/people";
}

// The banner on every signed-in page while previewing (main.js calls this on each render).
export function syncPreviewBanner(me) {
  let el = document.getElementById("xos-preview");
  const p = me?.preview;
  if (!p) { el?.remove(); return; }
  if (!el) {
    el = document.createElement("div");
    el.id = "xos-preview";
    el.setAttribute("role", "status");
    el.addEventListener("click", (e) => { if (e.target.closest("[data-preview-exit]")) exitPreview(); });
    document.body.appendChild(el);
  }
  const who = p.kind === "role" ? `${esc(p.label)}` : `${esc(p.label)} (${esc(me.role_name)})`;
  const html = `<span>Previewing as <b>${who}</b> — read-only</span><button data-preview-exit>Exit preview</button>`;
  if (el.innerHTML !== html) el.innerHTML = html;
}

function matches(p) {
  const f = filters;
  if (!f.inactive && p.active === false) return false;
  if (f.department && p.department !== f.department) return false;
  if (f.team && p.team !== f.team) return false;
  if (f.role && p.role !== f.role) return false;
  return true;
}
const searchText = (p) => [p.name, p.email, p.department, p.team, p.title].filter(Boolean).join(" ").toLowerCase();

function personRow(p) {
  const status = p.active === false ? `<span class="dc-st dc-failed">Inactive</span>`
    : p.source === "signin" ? `<span class="dc-st">Signed in</span>` : p.source === "manual" ? `<span class="dc-st">Added here</span>` : "";
  const why = p.role_source === "person" ? "" : `<div class="muted small">${esc(roleName(p.role))} · ${esc(SOURCE[p.role_source] || p.role_source)}</div>`;
  // Only a super admin grants or removes Admin, so for anyone else an Admin's role is fixed.
  const fixed = !data.you_super && (p.role === "admin" || (!p.assigned && p.bootstrap));
  const picker = p.super ? `<b>Admin</b><div class="muted small">Super admin (XOS_SUPER_ADMINS)</div>`
    : fixed ? `<b>${esc(roleName(p.role))}</b>${why}<div class="muted small">Only a super admin can change this.</div>`
    : `<select data-pp-role="${esc(p.email)}" aria-label="Role for ${esc(p.name || p.email)}">${roleOpts(p.assigned, p.bootstrap ? "Default (Admin)" : "Default", !data.you_super)}</select>${why}`;
  const q = searchText(p);
  return `<tr class="pp-row" data-q="${esc(q)}" ${filters.q && !q.includes(filters.q.toLowerCase()) ? "hidden" : ""}>
    <td><div class="pp-who">${p.picture ? `<img class="pp-photo" src="${esc(p.picture)}" alt="" loading="lazy" referrerpolicy="no-referrer" />` : `<span class="pp-photo pp-initial">${esc((p.name || p.email)[0].toUpperCase())}</span>`}
      <div><b>${esc(p.name || p.email.split("@")[0])}</b><div class="muted small">${esc(p.email)}</div></div></div></td>
    <td class="small">${esc(p.department || "—")}${p.team ? `<div class="muted">${esc(p.team)}</div>` : ""}</td>
    <td class="small">${esc(p.title || "")} ${status}</td>
    <td>${picker}</td>
    <td class="nowrap"><button class="btn-sm" data-act="pp-permissions-person" data-email="${esc(p.email)}">Access</button> <button class="btn-sm" data-act="pp-preview-person" data-email="${esc(p.email)}">Preview</button></td></tr>`;
}

function peopleSection() {
  const opts = (list, cur, all) => `<option value="">${all}</option>${list.map((x) => `<option ${x === cur ? "selected" : ""}>${esc(x)}</option>`).join("")}`;
  const rows = data.people.filter(matches);
  return `<div class="dc-row dc-tools pp-filters">
      <input id="pp-q" type="search" placeholder="Search name, email, title" value="${esc(filters.q)}" />
      <select id="pp-f-department" aria-label="Department">${opts(data.departments, filters.department, "All departments")}</select>
      <select id="pp-f-team" aria-label="Team">${opts(data.teams, filters.team, "All teams")}</select>
      <select id="pp-f-role" aria-label="Role"><option value="">All roles</option>${roleOpts(filters.role)}</select>
      <label class="small"><input type="checkbox" id="pp-f-inactive" ${filters.inactive ? "checked" : ""} /> Include inactive people</label>
      <span class="muted small">${rows.length} of ${data.people.length}</span></div>
    <div class="tbl-wrap"><table class="tbl pp-tbl"><thead><tr><th>Person</th><th>Department · team</th><th>Title</th><th>Xos role</th><th></th></tr></thead>
      <tbody>${rows.map(personRow).join("") || `<tr><td colspan="5" class="muted">No one matches.</td></tr>`}</tbody></table></div>
    <div class="ex-card"><h3>Give someone a role by email</h3>
      <div class="dc-row"><input id="pp-add-email" type="email" placeholder="name@example.com" />
        <select id="pp-add-role">${roleOpts("content_editor", "", !data.you_super)}</select>
        <button class="btn-sm btn-primary" data-act="pp-add">Assign</button></div>
      <p class="muted small">For someone who hasn't signed in yet. Choosing <b>Default</b> above removes an assignment.</p></div>`;
}

function rolesSection() {
  const chosen = r => roleDrafts.get(r.id) || r.perms;
  return `<p class="small muted">Edit each role's permissions below and save that role. Changes apply to everyone inheriting it, including department/team defaults. Admin always retains every permission. Use per-person overrides for exceptions. Tab visibility controls navigation; action permissions control what the server allows.</p>
    <div class="tbl-wrap"><table class="tbl pp-matrix"><thead><tr><th>Permission</th>${roles.roles.map(r=>`<th>${esc(r.name)}<div class="pp-count">${r.people} people${r.customized?' · customized':''}</div></th>`).join('')}</tr></thead>
      <tbody>${Object.entries(roles.permissions).map(([id,text])=>`<tr><td><b>${esc(id)}</b><div class="muted small">${esc(text)}</div></td>${roles.roles.map(r=>`<td class="pp-cell"><input type="checkbox" data-pp-role-permission="${r.id}" data-permission="${id}" aria-label="${esc(text)} for ${esc(r.name)}" ${chosen(r).includes(id)?'checked':''} ${r.id==='admin'||permissionSaving?'disabled':''}/></td>`).join('')}</tr>`).join('')}
      <tr><td>Save this role</td>${roles.roles.map(r=>`<td>${r.id==='admin'?'Always all permissions':`<button class="btn-sm btn-primary" data-act="pp-save-role-permissions" data-role="${r.id}" ${permissionSaving?'disabled':''}>Save ${esc(r.name)}</button><button class="btn-sm" data-act="pp-reset-role-permissions" data-role="${r.id}" ${permissionSaving?'disabled':''}>Restore defaults</button>`}</td>`).join('')}</tr>
      <tr><td>Check the experience</td>${roles.roles.map(r=>`<td><button class="btn-sm" data-act="pp-preview-role" data-role="${r.id}">Preview as ${esc(r.name)}</button></td>`).join('')}</tr></tbody></table></div>`;
}

function selectedAccessPerson() {
  return data.people.find(p=>p.email===permissionPerson) || data.people[0];
}
function permissionsSection() {
  const p = selectedAccessPerson();
  if(!p)return '<p>Add a person first.</p>';
  if(p.email!==permissionPerson||personDraft===null){permissionPerson=p.email;personDraft={...p.permission_overrides};}
  const fixed = p.role==='admin';
  const base = roles.roles.find(r=>r.id===p.role)?.perms || [];
  const current = id => fixed || (p.role_source==='inactive'?p.perms.includes(id):(id in personDraft?personDraft[id]:base.includes(id)));
  return `<h2>Permissions for one person</h2><p class="muted">Inherit follows their role. Allow and Deny override that role for this person. An inactive account cannot gain access through overrides. Admins always have full access.</p>
    <div class="dc-row"><select id="pp-permission-person" aria-label="Person to configure">${data.people.map(q=>`<option value="${esc(q.email)}" ${q.email===p.email?'selected':''}>${esc(q.name||q.email)} · ${esc(q.email)}</option>`).join('')}</select><span>${esc(roleName(p.role))}</span><button class="btn-sm" data-act="pp-preview-access">Preview this person's view</button></div>
    <div class="dc-row">${["viewer","presenter","editor"].map(v=>`<button class="btn-sm" data-act="pp-demo-preset" data-preset="${v}" ${fixed||permissionSaving?"disabled":""}>Demo ${v}</button>`).join("")}<span class="small muted">Apply a starting point, adjust details, then Save.</span></div>
    <label>Find a permission <input id="pp-permission-q" type="search" placeholder="Search demo, upload, tabs…" /></label>
    <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Permission</th><th>Override</th><th>Effective access</th></tr></thead><tbody>${Object.entries(roles.permissions).map(([id,text])=>`<tr data-permission-row data-q="${esc((id+' '+text).toLowerCase())}"><td><b>${esc(id)}</b><div class="small muted">${esc(text)}</div></td><td><select data-pp-person-permission="${id}" aria-label="${esc(text)} for ${esc(p.email)}" ${fixed||permissionSaving?'disabled':''}><option value="inherit" ${!(id in personDraft)?'selected':''}>Inherit (${base.includes(id)?'allowed':'denied'})</option><option value="allow" ${personDraft[id]===true?'selected':''}>Allow</option><option value="deny" ${personDraft[id]===false?'selected':''}>Deny</option></select></td><td data-effective="${id}">${current(id)?'Allowed':'Denied'}</td></tr>`).join('')}</tbody></table></div>
    <div class="dc-row"><button class="btn-sm btn-primary" data-act="pp-save-person-permissions" ${fixed||permissionSaving?'disabled':''}>Save permissions</button><button class="btn-sm" data-act="pp-reset-person-permissions" ${fixed||permissionSaving?'disabled':''}>Use role defaults</button></div>`;
}

export function renderPreviews() {
  if(!data||!roles){if(!data)load();return '<h1>Views & previews</h1><p>Loading people and access…</p>';}
  const person=data.people.find(p=>p.email===previewPerson);
  const role=roles.roles.find(r=>r.id===previewRole)||roles.roles[0];
  const perms=person?person.perms:role.perms;
  return `<h1>Views & previews</h1><p class="lead">Keep your development views in Xos. Inspect access, then preview the actual experience as a role or person. Preview is read-only; Exit preview restores your Admin workspace.</p>
    ${note?`<div class="ex-card" role="status">${esc(note)}</div>`:''}
    <div class="ex-card"><h2>Open your workspace</h2><div class="dc-row"><a class="btn-sm btn-primary" href="#/admin/demos">Demo Studio</a><a class="btn-sm" href="#/admin/dashboard">TV dashboard</a><a class="btn-sm" href="#/admin/people">People & permissions</a></div></div>
    <div class="ex-card"><h2>Preview another user's view</h2><div class="dc-row"><label>Role <select id="pp-view-role">${roleOpts(previewRole)}</select></label><label>Person <select id="pp-view-person"><option value="">Use role above</option>${data.people.map(p=>`<option value="${esc(p.email)}" ${p.email===previewPerson?'selected':''}>${esc(p.name||p.email)}</option>`).join('')}</select></label><label>View <select id="pp-view-target">${VIEW_LIST.map(v=>`<option value="${v[0]}" ${v[0]===previewView?'selected':''}>${esc(v[1])} · ${viewAllowed(perms,v)?'visible':'no access'}</option>`).join('')}</select></label><button class="btn-sm btn-primary" data-act="pp-launch-view-preview">Preview view</button></div><p class="small muted">This uses saved permissions, including per-person overrides. No device commands or development test data are created.</p></div>
    <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Xos tab</th><th>${esc(person?.name||person?.email||role.name)}</th><th>Required access</th></tr></thead><tbody>${VIEW_LIST.map(v=>`<tr><td>${esc(v[1])}</td><td>${viewAllowed(perms,v)?'Visible':'Hidden'}</td><td class="small">admin.view · ${esc(v[2])}${v[3]?' · '+esc(v[3]):''}</td></tr>`).join('')}</tbody></table></div>`;
}

function defaultsSection() {
  const row = (kind, name) => `<tr><td>${esc(name)}</td><td><select data-pp-default="${kind}" data-name="${esc(name)}" aria-label="Default role for ${esc(name)}">${roleOpts(data.defaults[kind][name] || "", "No default (Member)", true)}</select></td>
    <td class="muted small">${data.people.filter((p) => p[kind] === name && p.active !== false).length} people</td></tr>`;
  const table = (kind, list) => list.length ? `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>${kind === "team" ? "Team" : "Department"}</th><th>Default role</th><th></th></tr></thead><tbody>${list.map((n) => row(kind, n)).join("")}</tbody></table></div>`
    : `<p class="muted small">No ${kind}s yet.</p>`;
  return `<p class="small muted">Everyone in a department or team gets its default role unless they're given one. A team default wins over its department's. Admin can't be a default: give it to people one by one. Inactive people are Members, and super admins are always Admin.</p>
    <h3>Departments</h3>${table("department", data.departments)}<h3>Teams</h3>${table("team", data.teams)}`;
}

function auditLine(i) {
  if(i.kind.startsWith('permissions.'))return `Updated permissions for <b>${esc(i.target)}</b><details><summary>Saved access rules</summary><pre>${esc(i.to==null?'Role defaults':JSON.stringify(i.to,null,2))}</pre></details>`;
  const from = i.from ? roleName(i.from) : "Default";
  const to = i.to ? roleName(i.to) : "Default";
  if (i.kind === "role") return `Role for <b>${esc(i.target)}</b>: ${esc(from)} → ${esc(to)}${i.effective_to && i.effective_to !== i.to ? ` <span class="muted">(now ${esc(roleName(i.effective_to))})</span>` : ""}`;
  if (i.kind === "default") return `Default for ${esc(i.target.replace(":", " "))}: ${esc(i.from ? roleName(i.from) : "none")} → ${esc(i.to ? roleName(i.to) : "none")}`;
  if (i.kind === "preview.start") return `Started previewing as <b>${esc(i.preview_kind === "role" ? roleName(i.target) : i.target)}</b>`;
  if (i.kind === "preview.end") return `Ended the preview as ${esc(i.preview_kind === "role" ? roleName(i.target) : i.target)}${i.reason === "expired" ? " (expired)" : ""}`;
  return esc(i.kind);
}

function activitySection() {
  return `<p class="small muted">Every role change and preview, newest first. The log is append-only (<code>people_audit.jsonl</code>).</p>
    <div class="tbl-wrap"><table class="tbl"><thead><tr><th>When</th><th>Who</th><th>What</th></tr></thead><tbody>${(audit || []).map((i) => `<tr><td class="small nowrap">${dayShort(i.at * 1000)} ${time(i.at * 1000)}</td><td class="small">${esc(i.by)}</td><td class="small">${auditLine(i)}</td></tr>`).join("") || `<tr><td colspan="3" class="muted">Nothing yet.</td></tr>`}</tbody></table></div>`;
}

export function renderPeople() {
  if (!S.isHub()) return `<h1>People &amp; roles</h1>`;
  if (!data || !roles) { if (!data) load(); return `<h1>People &amp; roles</h1><p class="muted">Loading…</p>`; }
  const tabs = [["people", `People · ${data.people.filter((p) => p.active !== false).length}`], ["roles", "Role permissions"], ["permissions", "Per-person permissions"], ["defaults", "Department & team defaults"], ["activity", "Activity"]];
  const body = { people: peopleSection, roles: rolesSection, permissions: permissionsSection, defaults: defaultsSection, activity: activitySection }[section]();
  return `<h1>People &amp; roles</h1>
    <p class="lead">Who can do what in Xos. Give someone a role, set defaults for a department or team, and preview Xos as any role or person to check what they'll see.</p>
    ${note ? `<div class="ex-card callout" role="status">${esc(note)}</div>` : ""}
    <div class="ct-new pp-tabs">${tabs.map(([id, label]) => `<button class="chip-sm ${section === id ? "on" : ""}" data-act="pp-section" data-id="${id}">${esc(label)}</button>`).join("")}</div>
    ${body}`;
}

async function put(path, body, done) {
  const res = await fetch(path, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).catch(() => null);
  note = res?.ok ? done : res ? (await res.json()).detail : "Network error";
  await load();
}

/** Selects and inputs on this page (main.js passes every "input" event). */
export function handlePeopleInput(el) {
  if(el.dataset.ppRolePermission){const role=el.dataset.ppRolePermission;const chosen=new Set(roleDrafts.get(role)||roles.roles.find(r=>r.id===role).perms);el.checked?chosen.add(el.dataset.permission):chosen.delete(el.dataset.permission);roleDrafts.set(role,[...chosen]);return true;}
  if(el.id==='pp-permission-person'){permissionPerson=el.value;personDraft=null;S.poke();return true;}
  if(el.id==='pp-permission-q'){document.querySelectorAll('[data-permission-row]').forEach(r=>r.hidden=!r.dataset.q.includes(el.value.toLowerCase()));return true;}
  if(el.dataset.ppPersonPermission){const id=el.dataset.ppPersonPermission;if(el.value==='inherit')delete personDraft[id];else personDraft[id]=el.value==='allow';const p=selectedAccessPerson(),base=roles.roles.find(r=>r.id===p.role)?.perms||[];const allowed=p.role_source==='inactive'?p.perms.includes(id):(id in personDraft?personDraft[id]:base.includes(id));document.querySelector(`[data-effective="${id}"]`).textContent=allowed?'Allowed':'Denied';return true;}
  if(el.id==='pp-view-role'){previewRole=el.value;previewPerson='';S.poke();return true;}
  if(el.id==='pp-view-person'){previewPerson=el.value;S.poke();return true;}
  if(el.id==='pp-view-target'){previewView=el.value;return true;}

  if (el.id === "pp-q") {
    filters.q = el.value.trim();
    const needle = filters.q.toLowerCase();
    document.querySelectorAll(".pp-row").forEach((r) => { r.hidden = Boolean(needle) && !(r.dataset.q || "").includes(needle); });
    return true;
  }
  if (["pp-f-department", "pp-f-team", "pp-f-role"].includes(el.id)) { filters[el.id.slice(5)] = el.value; S.poke(); return true; }
  if (el.id === "pp-f-inactive") { filters.inactive = el.checked; S.poke(); return true; }
  if (el.dataset.ppRole) {
    const email = el.dataset.ppRole;
    put(`/api/people/${encodeURIComponent(email)}/role`, { role: el.value }, `${email} is now ${el.value ? roleName(el.value) : "on their default role"}.`);
    return true;
  }
  if (el.dataset.ppDefault) {
    const { ppDefault: kind, name } = el.dataset;
    put("/api/people/defaults", { kind, name, role: el.value }, `Default for ${name}: ${el.value ? roleName(el.value) : "none"}.`);
    return true;
  }
  return false;
}

async function savePermissions(path, permissions, done){
  if(permissionSaving)return;permissionSaving=true;
  try{const r=await fetch(path,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({permissions})});const j=await r.json();if(!r.ok)throw Error(j.detail||'Could not save access');roleDrafts.clear();personDraft=null;note=done;await load();await refreshWho();}catch(e){note=e.message;}finally{permissionSaving=false;S.poke();}
}
export function handlePeople(act, el) {
  if(act==='pp-demo-preset'){const bits=['demos.view','demos.previews',...(el.dataset.preset==='presenter'?['demos.present','demos.control','demos.enable']:[]),...(el.dataset.preset==='editor'?['demos.edit','demos.upload','demos.delete']:[])];for(const k of Object.keys(roles.permissions).filter(k=>k.startsWith('demos.')))personDraft[k]=bits.includes(k);personDraft['admin.view']=true;return true;}
  if(act==='pp-permissions-person'){permissionPerson=el.dataset.email;personDraft=null;section='permissions';return true;}
  if(act==='pp-save-role-permissions'){savePermissions('/api/roles/'+el.dataset.role+'/permissions',roleDrafts.get(el.dataset.role)||roles.roles.find(r=>r.id===el.dataset.role).perms,'Role permissions saved.');return true;}
  if(act==='pp-reset-role-permissions'){savePermissions('/api/roles/'+el.dataset.role+'/permissions',null,'Role defaults restored.');return true;}
  if(act==='pp-save-person-permissions'||act==='pp-reset-person-permissions'){savePermissions('/api/people/'+encodeURIComponent(permissionPerson)+'/permissions',act==='pp-reset-person-permissions'?{}:personDraft,'Person permissions saved.');return true;}
  if(act==='pp-preview-access'){startPreview({email:permissionPerson},'#/admin/demos');return true;}
  if(act==='pp-launch-view-preview'){startPreview(previewPerson?{email:previewPerson}:{role:previewRole},'#/admin/'+previewView);return true;}

  if (act === "pp-section") { section = el.dataset.id; note = ""; if (section === "activity") load(); return true; }
  if (act === "pp-add") {
    const email = document.getElementById("pp-add-email").value.trim().toLowerCase();
    const role = document.getElementById("pp-add-role").value;
    if (!email) { note = "Enter an @example.com address."; return true; }
    put(`/api/people/${encodeURIComponent(email)}/role`, { role }, `${email} is now ${roleName(role)}.`);
    return true;
  }
  if (act === "pp-preview-role") { startPreview({ role: el.dataset.role }); return true; }
  if (act === "pp-preview-person") { startPreview({ email: el.dataset.email }); return true; }
  return false;
}
