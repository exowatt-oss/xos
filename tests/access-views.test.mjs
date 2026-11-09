import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source = readFileSync(new URL('../app/js/access-views.js', import.meta.url), 'utf8');
const {ADMIN_TABS, visibleTabs, viewAllowed} = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

test('a demo-only user sees one Xos tab without setup or editing access', () => {
  assert.deepEqual(visibleTabs(['admin.view','demos.view','demos.previews']).map(t=>t[0]), ['demos']);
});
test('navigation permission never implies shell or administrative actions', () => {
  assert.deepEqual(visibleTabs(['demos.view']), []);
  assert.deepEqual(visibleTabs(['admin.view','views.rooms','views.people','views.previews']), []);
  assert.deepEqual(visibleTabs(['admin.view','views.rooms','setup']).map(t=>t[0]), ['rooms']);
});
test('sidebar and preview availability use the same view policy', () => {
  const perms=['admin.view','demos.view','views.previews','people'];
  assert.deepEqual(visibleTabs(perms).map(t=>t[0]), ['demos','previews']);
  for(const tab of ADMIN_TABS) assert.equal(viewAllowed(perms,tab),visibleTabs(perms).includes(tab));
});
