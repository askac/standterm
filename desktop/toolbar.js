'use strict';

const byId = id => document.getElementById(id);
let lastNoticeId = 0;
let noticeTimer;
function showNotice(message, error = false) {
  clearTimeout(noticeTimer);
  const area = byId('notice-area');
  byId('notice').textContent = message;
  byId('notice-indicator').textContent = error ? '!' : '\u2713';
  area.title = message;
  area.classList.toggle('error', error);
  area.classList.add('visible');
  noticeTimer = setTimeout(() => {
    area.classList.remove('visible');
    noticeTimer = setTimeout(() => {
      byId('notice').textContent = '';
      byId('notice-indicator').textContent = '';
      area.title = '';
    }, 350);
  }, error ? 10000 : 5000);
}
for (const button of document.querySelectorAll('[data-action], [data-menu]')) {
  button.addEventListener('click', () => {
    const action = button.dataset.action || `menu:${button.dataset.menu}`;
    window.desktopToolbar.invoke(action).catch(() => {
      showNotice('Action unavailable. Please retry.', true);
    });
  });
}
window.desktopToolbar.onState(state => {
  if (typeof state.mac === 'boolean') {
    byId('menus').hidden = state.mac;
  }
  if (typeof state.notice === 'string' && Number.isSafeInteger(state.noticeId) && state.noticeId !== lastNoticeId) {
    lastNoticeId = state.noticeId;
    showNotice(state.notice, state.error === true);
  }
  if (!['idle', 'starting', 'recording', 'paused', 'stopping'].includes(state.state)) return;
  const active = state.state !== 'idle';
  const stoppable = ['recording', 'paused'].includes(state.state);
  byId('record').hidden = active;
  byId('pause').hidden = !stoppable;
  byId('stop').hidden = !active;
  byId('stop').disabled = !stoppable;
  byId('save').disabled = byId('copy').disabled = state.screenshotBusy === true;
  byId('recording-status').textContent = active ? state.label : '';
  const pauseLabel = state.state === 'paused' ? 'Resume recording' : 'Pause recording';
  byId('pause').title = pauseLabel;
  byId('pause').setAttribute('aria-label', pauseLabel);
  byId('pause-shape').setAttribute('d', state.state === 'paused' ? 'M7 4l13 8-13 8z' : 'M8 5v14M16 5v14');
});
window.desktopToolbar.invoke('ready').catch(() => {});
