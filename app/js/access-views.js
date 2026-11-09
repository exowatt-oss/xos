// Shared navigation policy for the Xos sidebar and the Admin preview workspace.
export const ADMIN_TABS = [
  ['today', 'Today', 'views.overview'],
  ['overview', 'Overview', 'views.overview'],
  ['dashboard', 'Dashboard', 'views.dashboard'],
  ['content', 'Content', 'views.content'],
  ['screens', 'Screens', 'views.screens'],
  ['demos', 'Demo Studio', 'demos.view'],
  ['rooms', 'Rooms', 'views.rooms', 'setup'],
  ['integrations', 'Integrations', 'views.integrations', 'integrations'],
  ['people', 'People & roles', 'views.people', 'people'],
  ['howto', 'How to', 'views.howto'],
  ['previews', 'Views & previews', 'views.previews', 'people'],
];
export function viewAllowed(perms, tab) {
  return perms.includes('admin.view') && perms.includes(tab[2]) && (!tab[3] || perms.includes(tab[3]));
}
export function visibleTabs(perms = []) {
  return ADMIN_TABS.filter(tab => viewAllowed(perms, tab));
}
