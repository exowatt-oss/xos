// Runs only in the room appliance's supported meeting pages, never in personal browsers.
(function install() {
  if (!/^(meet\.google\.com|(?:[^.]+\.)?zoom\.(us|com)|teams\.(microsoft|live)\.com|teams\.cloud\.microsoft|(?:[\w-]+\.)*webex\.com)$/.test(location.hostname)) return;
  if (!document.documentElement) {
    new MutationObserver((_, observer) => {
      if (document.documentElement) { observer.disconnect(); install(); }
    }).observe(document, {childList:true, subtree:true});
    return;
  }
  if (window.__xosMeetingDisplay?.refresh) { window.__xosMeetingDisplay.refresh(); return; }
  const state = window.__xosMeetingDisplay = { attention: "", hidden: 0 };
  const attr = "data-xos-passive-notice";
  // CSSOM property writes work under provider CSP without bypassing its security policy.
  const savedDisplay = new Map();
  const remotePaint = new Map();
  state.remoteSurfaces = 0;
  let geminiTarget = null;
  const controls = 'button,[role="button"]';
  const interactive = 'button,[role="button"],input,select,textarea,a[href]';
  const active = () => [...document.querySelectorAll(controls)].some(e =>
    [e.getAttribute('aria-label'), e.getAttribute('title'), e.textContent].some(s =>
      /^(leave call|leave meeting|leave|end meeting|end|hang up)(?:$|\s*\()/i.test((s || '').trim())) && e.getClientRects().length && !e.closest(`[${attr}]`));
  const invitation = /your meeting[’']?s ready|your meeting is ready|share (?:this |the )?meeting link/i;
  const promotion = /gemini\s+is\s+(?:now\s+)?available|gemini\s+uses\s+meeting\s+conversations\s+to\s+answer\s+questions|(?:try|introducing|get started with)\s+gemini/i;
  const decision = /admit|wants to join|request to join|host controls|recording|consent/i;
  const tip = /are you (?:talking|speaking)|(?:your |the )?microphone is (?:off|muted)|you(?:[’']re| are) muted|you joined|joined (?:the |this )?meeting|you can present|meeting details|meeting link copied/i;
  const attention = text => /poor connection|connection.*(?:lost|failed)|network.*(?:unavailable|error)|offline|disconnected|reconnect/i.test(text) ? "connection"
    : /(?:microphone|camera|device).*(?:unavailable|not found|blocked|failed)|permission.*(?:denied|blocked)/i.test(text) ? "media" : "";
  const captions = e => /caption|transcript/i.test([e.id, e.getAttribute('aria-label'), e.getAttribute('data-message-text')].filter(Boolean).join(' '));
  const hide = e => {
    if (e.hasAttribute(attr)) return;
    if (geminiTarget && e.contains(geminiTarget)) {
      // Keep the opt-out hit target available for trusted input while hiding the notice.
      if (!remotePaint.has(e)) remotePaint.set(e, [e.style.getPropertyValue('opacity'), e.style.getPropertyPriority('opacity')]);
      e.style.setProperty('opacity', '0', 'important'); state.hidden++;
      return;
    }
    savedDisplay.set(e, [e.style.getPropertyValue('display'), e.style.getPropertyPriority('display')]);
    e.style.setProperty('display', 'none', 'important');
    e.setAttribute(attr, '');
    state.hidden++;
  };
  const floating = e => {
    const r = e.getBoundingClientRect();
    if (!r.width || !r.height || r.width > innerWidth * .65 || r.height > innerHeight * .5) return false;
    for (let p=e; p && p !== document.body; p=p.parentElement) {
      if (/^(fixed|absolute)$/.test(getComputedStyle(p).position)) return true;
    }
    return false;
  };
  // Mirror only supported, currently enabled provider controls. Tokens bind to a DOM node
  // and its participant context, so a reordered roster cannot redirect a tablet action.
  const controlNodes = new WeakMap();
  let offered = new Map(), selectedPerson = '', selectedAt = 0;
  const labelOf = e => {
    const references = attribute => (e.getAttribute(attribute) || '').split(/\s+/).map(id => document.getElementById(id)?.textContent || '').join(' ').trim();
    let label = references('aria-labelledby') || e.getAttribute('aria-label') || e.getAttribute('title') || '';
    if (!label) {
      const clone = e.cloneNode(true);
      clone.querySelectorAll('[aria-hidden="true"],.google-symbols,.google-material-icons,.material-icons,.material-symbols-outlined').forEach(n => n.remove());
      label = clone.textContent || '';
    }
    // Meet renders the People button as a count with its name in an ARIA tooltip.
    if (!label.trim() || /^\d+$/.test(label.trim())) {
      label = references('aria-describedby') || references('data-tooltip-id') || e.getAttribute('aria-description') || e.getAttribute('data-tooltip') || e.parentElement?.getAttribute('data-tooltip') || label;
    }
    if ((!label.trim() || /^\d+$/.test(label.trim())) && [...e.querySelectorAll('[aria-hidden="true"],.google-material-icons,.material-icons')].some(n => /^(group|people)$/.test(n.textContent.trim()))) label = 'People';
    return (label || e.innerText || '').trim().replace(/\s+/g, ' ').replace(/\s*\((?:(?:ctrl|alt|shift|command|⌘)[^)]*)\)\s*$/i, '');
  };
  const geminiPreference = /^don[’']t show(?: (?:this|it|this message))? again[.!]?$|^do not show(?: (?:this|it|this message))? again[.!]?$/i;
  const noticeText = e => {
    const clone = e.cloneNode(true);
    clone.querySelectorAll('[aria-hidden="true"],.google-symbols,[class*="material-icons"]').forEach(n => n.remove());
    const walker = document.createTreeWalker(clone, NodeFilter.SHOW_TEXT), parts=[];
    while (walker.nextNode()) parts.push(walker.currentNode.textContent);
    for (const n of [e, ...e.querySelectorAll('[aria-label],[aria-labelledby],[aria-describedby]')]) {
      if (n.getAttribute('aria-label')) parts.push(n.getAttribute('aria-label'));
      for (const attr of ['aria-labelledby','aria-describedby']) for (const id of (n.getAttribute(attr)||'').split(/\s+/)) parts.push(document.getElementById(id)?.textContent || '');
    }
    return parts.join(' ').replace(/\s+/g,' ').trim();
  };
  const geminiChoice = () => {
    if (location.hostname !== 'meet.google.com' || !active()) return null;
    for (const e of document.querySelectorAll('button,[role="button"],[role="menuitem"],[role="checkbox"],input[type="checkbox"]')) {
      const label = labelOf(e) || [...(e.labels || [])].map(n => n.textContent.trim()).join(' ');
      if (!geminiPreference.test(label) || e.disabled || e.getAttribute('aria-disabled') === 'true') continue;
      let target = e.getClientRects().length ? e : [...(e.labels || [])].find(n => n.getClientRects().length);
      if (!target || getComputedStyle(target).visibility === 'hidden') continue;
      for (let card=e.parentElement, depth=0; card && card !== document.body && depth<8; card=card.parentElement, depth++) {
        const text = noticeText(card);
        // Recording can be mentioned in an informational introduction. Actual consent/host
        // decisions and acceptance controls remain outside this automatic opt-out.
        const consent = /consent|permission|admit|wants to join|request to join|host controls/i.test(text)
          || [...card.querySelectorAll(controls)].some(n => /^(accept|agree|allow|start recording|stop recording)$/i.test(labelOf(n)));
        if (text.length > 1500 || consent || card.querySelector('video,audio,canvas')) break;
        if (!promotion.test(text)) continue;
        if (card.querySelectorAll(interactive).length > 12) break;
        if (e.checked === true || e.getAttribute('aria-checked') === 'true') {
          // Some versions use a preference checkbox, then Got it. Never untick the preference.
          const done = [...card.querySelectorAll(controls)].filter(n => !n.disabled && n.getAttribute('aria-disabled') !== 'true' && n.getClientRects().length && /^(got it|ok(?:ay)?|dismiss|close|done)$/i.test(labelOf(n)));
          if (done.length !== 1) break;
          target = done[0];
        }
        return {target, card};
      }
    }
    return null;
  };
  state.geminiDismissal = () => {
    const choice = geminiChoice();
    if (!choice) return '';
    // Re-find the current, explicitly labelled opt-out each time, including after DOM reuse.
    document.querySelectorAll('[data-xos-gemini-dismiss]').forEach(n => { if (n !== choice.target) n.removeAttribute('data-xos-gemini-dismiss'); });
    const id = choice.target.getAttribute('data-xos-gemini-dismiss') || crypto.randomUUID();
    choice.target.setAttribute('data-xos-gemini-dismiss', id);
    return `[data-xos-gemini-dismiss="${id}"]`;
  };
  const visible = e => e.isConnected && e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden' && !e.closest(`[${attr}]`);
  const toolbar = /^(?:people(?:[ \d()—-]+)?|show everyone(?:[ \d()—-]+)?|participants(?:[ \d()—-]+)?|more options|(?:open|close) the manage participants.*|(?:show|hide|send a|more) reactions?|reactions?|react|raise hand|lower hand|lower all hands|turn (?:on|off) captions.*|(?:show|hide) (?:closed )?captions.*)$/i;
  const reactionOpener = /^(?:reactions?|react|send a reaction|(?:show|open|hide|close)(?: the)? reactions?)(?:,? (?:menu|button|popup).*)?$/i;
  const participantAction = /^(?:admit(?: .+)?|allow(?: all)?|deny(?: .+)?|reject(?: .+)?|remove(?: from (?:the )?(?:meeting|call))?|mute(?: participant| .+[’']s microphone)?|lower .+[’']s hand|more options(?: for .+)?|more actions|more)$/i;
  const reactionInfo = label => {
    const name = label.toLowerCase().replace(/[\uFE0E\uFE0F\u{1F3FB}-\u{1F3FF}]/gu,'').replace(/^send(?: a)? |^react with | reaction$/g,'').replace(/[_:]/g,' ').replace(/\s+/g,' ').trim();
    const definitions = [
      ['thumbs-up','👍','Thumbs up',/^(?:👍|thumbs? ?up|\+1)(?: .+skin tone)?$/],
      ['clap','👏','Clap',/^(?:👏|clap(?:ping)?(?: hands)?|applause)(?: .+skin tone)?$/],
      ['heart','❤️','Heart',/^(?:❤|💖|heart|red heart|sparkling heart)$/],
      ['laugh','😂','Laugh',/^(?:😂|😆|joy|laugh(?:ing)?|face with tears of joy)$/],
      ['celebrate','🎉','Celebrate',/^(?:🎉|🎊|tada|party popper|celebrate|celebration)$/],
      ['surprised','😮','Surprised',/^(?:😮|😲|open mouth|surprised|face with open mouth)$/],
      ['sad','😢','Sad',/^(?:😢|cry|crying face|sad)$/],
      ['thinking','🤔','Thinking',/^(?:🤔|thinking|thinking face)$/],
      ['thumbs-down','👎','Thumbs down',/^(?:👎|thumbs? ?down|-1)(?: .+skin tone)?$/],
      ['yes','✅','Yes',/^(?:yes|✅|✔)$/],['no','❌','No',/^(?:no|❌|✖)$/],
    ];
    const d=definitions.find(d=>d[3].test(name));
    return d ? {key:d[0],emoji:d[1],label:d[2]} : null;
  };
  const namedReactionSurface = '[aria-label*="reaction" i],[class*="reaction" i],[data-testid*="reaction" i]';
  const reactionScope = e => {
    if (e?.closest('[data-message-id],[class*="chat-message" i],[class*="message-reaction" i]')) return null;
    return e?.parentElement?.closest('[role="menu"],[role="dialog"],' + namedReactionSurface);
  };
  const reactionSurface = e => {
    let surface=reactionScope(e);
    for (let p=surface?.parentElement; p && p!==document.body; p=p.parentElement) {
      const r=p.getBoundingClientRect();
      if (p.querySelector('video,audio,canvas') || r.width>innerWidth*.65 || r.height>innerHeight*1.05) break;
      if (p.matches('[role="menu"],[role="dialog"],' + namedReactionSurface)) surface=p;
    }
    return surface;
  };
  const destructive = /^(?:remove|deny|reject)/i;
  const contextText = e => {
    const clone = e.cloneNode(true);
    clone.querySelectorAll('button,[role="button"],[role="menu"],[role="tooltip"],[aria-hidden="true"],.google-symbols,[class*="material-icons"],[class*="material-symbols"]').forEach(n => n.remove());
    const walker = document.createTreeWalker(clone, NodeFilter.SHOW_TEXT), parts=[];
    while (walker.nextNode()) { const t=walker.currentNode.textContent.trim(); if(t) parts.push(t); }
    return parts.join(' ');
  };
  const personOf = e => {
    const named = labelOf(e).match(/^(?:admit|deny|reject) (.+)$/i) || labelOf(e).match(/^more options for (.+)$/i) || labelOf(e).match(/^(?:mute|lower) (.+?)[’']s (?:microphone|hand)$/i);
    if (named) return named[1].slice(0,140);
    const row = e.closest('[role="listitem"],[data-participant-id],.participants-item');
    if (row) {
      const name = row.querySelector('.participants-item__display-name,[data-participant-name]');
      if (name) return (name.innerText || name.getAttribute('data-participant-name') || '').trim().slice(0,140);
      return contextText(row).slice(0,140);
    }
    const dialog = e.closest('[role="alert"],[role="dialog"]');
    if (dialog && /wants to join|request.*join|waiting room/i.test(dialog.textContent)) {
      const clone = dialog.cloneNode(true);
      clone.querySelectorAll('button,[role="button"]').forEach(n => n.remove());
      return clone.textContent.trim().replace(/\s+/g,' ').slice(0,140);
    }
    if (/^more actions$/i.test(labelOf(e))) {
      let context = '';
      for (let p=e.parentElement, depth=0; p && p !== document.body && depth < 8; p=p.parentElement, depth++) {
        const options = [...p.querySelectorAll('button,[role="button"]')].filter(n => /^more actions$/i.test(labelOf(n)));
        if (options.length > 1 || p.querySelector('video,audio,canvas')) break;
        const text = contextText(p);
        if (text && text.length <= 140) context = text;
      }
      return context;
    }
    return '';
  };
  // Private compatibility diagnostics: button metadata and bounded opt-out notice context.
  state.controlLabels = () => active() ? [...document.querySelectorAll('button,[role="button"],[role="menuitem"]')].filter(visible).map(labelOf).filter(s => s && s.length < 160).slice(0,120) : [];
  state.controlDiagnostics = () => {
    const numeric = [...document.querySelectorAll('button,[role="button"]')].filter(e => visible(e) && /^\d+$/.test(labelOf(e))).slice(0,10).map(e => ({label:labelOf(e),attributes:Object.fromEntries(['aria-label','aria-labelledby','aria-describedby','aria-description','data-tooltip','data-tooltip-id','title'].map(k=>[k,e.getAttribute(k)])),icons:[...e.querySelectorAll('[aria-hidden="true"]')].map(n=>n.textContent.trim()).filter(t=>t.length<60).slice(0,3)}));
    const notices = [...document.querySelectorAll('button,[role="button"],[role="checkbox"],input[type="checkbox"]')]
      .filter(e => geminiPreference.test(labelOf(e) || [...(e.labels || [])].map(n=>n.textContent).join(' '))).slice(0,2).map(e => {
        const parents=[];
        for (let p=e.parentElement, depth=0; p && p!==document.body && depth<5; p=p.parentElement, depth++) {
          if (p.querySelector('video,audio,canvas')) break;
          const text=noticeText(p); if(text.length>1000) break;
          parents.push({tag:p.tagName,role:p.getAttribute('role')||'',text:text.slice(0,400)});
        }
        return {kind:'notice-preference',label:labelOf(e),parents};
      });
    const missedReactions = [...document.querySelectorAll('button,[role="button"],[role="menuitem"]')]
      .filter(e => visible(e) && reactionInfo(labelOf(e)) && !reactionScope(e)).slice(0,6).map(e => {
        const parents=[];
        for (let p=e.parentElement, depth=0; p && p!==document.body && depth<4; p=p.parentElement, depth++) {
          parents.push({tag:p.tagName,role:p.getAttribute('role')||'',classes:String(p.className||'').slice(0,160)});
        }
        return {kind:'unscoped-reaction',label:labelOf(e),parents};
      });
    return [...numeric,...notices,...missedReactions];
  };
  state.controls = () => {
    const list = [], next = new Map();
    if (!active()) { offered.clear(); selectedPerson = ''; return list; }
    if (Date.now() - selectedAt > 60000) selectedPerson = '';
    for (const e of document.querySelectorAll('button,[role="button"],[role="menuitem"]')) {
      if (!visible(e) || e.disabled || e.getAttribute('aria-disabled') === 'true') continue;
      const label = labelOf(e);
      if (!label || label.length > 160) continue;
      const isToolbar = toolbar.test(label) || reactionOpener.test(label);
      const personAction = participantAction.test(label);
      const context = personAction ? personOf(e) || (e.closest('[role="menu"],[role="dialog"]') ? selectedPerson : '') : '';
      const isPerson = personAction && !!context;
      const reaction = reactionInfo(label);
      const isReaction = !!reaction && !!reactionScope(e) && (!/^(yes|no)$/.test(reaction.key) || !!e.parentElement?.closest(namedReactionSurface));
      const close = /^(?:close|cancel|done)$/i.test(label) && !!e.closest('[role="menu"],[role="dialog"],[role="complementary"]');
      if (!(isToolbar || isPerson || isReaction || close)) continue;
      const row = e.closest('[data-participant-id],.participants-item,[role="listitem"]');
      const fingerprint = label + '\n' + context + '\n' + (row?.getAttribute('data-participant-id') || row?.id || '');
      let entry = controlNodes.get(e);
      if (!entry || entry.fingerprint !== fingerprint) {
        entry = {id:crypto.randomUUID(), fingerprint, e, context, label};
        controlNodes.set(e, entry);
      }
      next.set(entry.id, entry);
      list.push({id:entry.id,label,context,destructive:destructive.test(label),group:isPerson?'People':isReaction?'Reactions':'Meeting',...(isReaction ? {reaction} : reactionOpener.test(label) ? {kind:'reaction_opener'} : {})});
      if (list.length >= 100) break;
    }
    offered = next;
    return list;
  };
  state.resolveControl = id => {
    state.controls();
    const entry = offered.get(id);
    if (!entry) return '';
    if (entry.context) { selectedPerson = entry.context; selectedAt = Date.now(); }
    else if (toolbar.test(entry.label) || /^(close|cancel|done)$/i.test(entry.label)) selectedPerson = '';
    document.querySelectorAll('[data-xos-control]').forEach(e => e.removeAttribute('data-xos-control'));
    entry.e.setAttribute('data-xos-control', id);
    return `[data-xos-control="${id}"]`;
  };

  let queued = false;
  const refresh = () => {
    queued = false;
    for (const [e, [value, priority]] of remotePaint) {
      if (e.style.getPropertyValue('opacity') === '0' && e.style.getPropertyPriority('opacity') === 'important') {
        if (value) e.style.setProperty('opacity', value, priority);
        else e.style.removeProperty('opacity');
      }
    }
    remotePaint.clear(); state.remoteSurfaces = 0;
    for (const [e, [value, priority]] of savedDisplay) {
      if (e.style.getPropertyValue('display') === 'none' && e.style.getPropertyPriority('display') === 'important') {
        if (value) e.style.setProperty('display', value, priority);
        else e.style.removeProperty('display');
      }
      e.removeAttribute(attr);
    }
    savedDisplay.clear();
    state.hidden = 0;
    // Show the room's meeting, without the account shell's padding or iframe border.
    if (/(^|\.)zoom\.(us|com)$/.test(location.hostname)) {
      const frames = [...document.querySelectorAll('iframe')].filter(frame => {
        try {
          const url = new URL(frame.src, location.href);
          return url.protocol === 'https:' && /(^|\.)zoom\.(us|com)$/.test(url.hostname) && url.pathname.startsWith('/wc/');
        } catch (_) { return false; }
      });
      if (frames.length === 1) {
        for (const [property, value] of Object.entries({position:'fixed',inset:'0',width:'100vw',height:'100vh',maxWidth:'none',maxHeight:'none',border:'0',borderRadius:'0',margin:'0',padding:'0',boxSizing:'border-box',transform:'none',boxShadow:'none',zIndex:'2147483000'})) {
          const cssProperty = property.replace(/[A-Z]/g, c => '-' + c.toLowerCase());
          frames[0].style.setProperty(cssProperty, value, 'important');
        }
      }
    }
    if (!active()) {
      state.attention = '';
      return;
    }
    const gemini = geminiChoice();
    geminiTarget = gemini?.target || null;
    if (gemini) hide(gemini.card);
    let issue = '';
    for (const e of document.querySelectorAll('[role="alert"],[role="status"],[role="dialog"],[role="tooltip"],[aria-live="polite"],[aria-live="assertive"]')) {
      if (captions(e) || e.querySelector('video,audio,canvas') || e.textContent.length > 1000) continue;
      const text = e.textContent.trim();
      if (!text) continue;
      // Never hide admission requests, host decisions, settings, chat or other interactive UI.
      // The invitation card is the one deliberate exception: its link is on the room controller.
      const readyCard = (invitation.test(text) || promotion.test(text)) && !decision.test(text);
      if (e.querySelector(interactive) && !readyCard) continue;
      if (e.getAttribute('role') === 'dialog' && !readyCard) continue;
      const toast = ['alert', 'status'].includes(e.getAttribute('role'));
      if (!(readyCard || tip.test(text) || (toast && floating(e)))) continue;
      issue ||= attention(text);
      hide(e);
    }
    // Some Meet versions render the invitation card without an ARIA dialog role.
    // Match its heading text, then only hide a compact floating ancestor, never the call surface.
    if (document.body) {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        const heading = walker.currentNode.textContent.trim();
        if (!/^your meeting(?:[’']s| is) ready[.!]?$/i.test(heading) && !(heading.length < 180 && promotion.test(heading))) continue;
        let card = null;
        for (let e = walker.currentNode.parentElement; e && e !== document.body; e = e.parentElement) {
          if (e.querySelector('video,audio,canvas') || decision.test(e.textContent)) break;
          if (!floating(e)) break;
          card = e;
        }
        if (card) hide(card);
      }
    }
    for (const labelled of document.querySelectorAll('[aria-label]')) {
      if (!promotion.test(labelled.getAttribute('aria-label') || '')) continue;
      let card = null;
      for (let e = labelled; e && e !== document.body; e = e.parentElement) {
        if (e.querySelector('video,audio,canvas') || decision.test(e.textContent) || !floating(e)) break;
        card = e;
      }
      if (card) hide(card);
    }
    // Control surfaces are operated on the tablet. Keep their DOM and hit targets alive for
    // trusted input, but do not paint participant/reaction/admission overlays on the TV.
    const offeredControls = state.controls();
    const suppressPaint = e => {
      if (!e || remotePaint.has(e) || e.querySelector('video,audio,canvas')) return;
      const r = e.getBoundingClientRect();
      if (!r.width || !r.height || r.width > innerWidth * .65 || r.height > innerHeight * 1.05) return;
      remotePaint.set(e, [e.style.getPropertyValue('opacity'), e.style.getPropertyPriority('opacity')]);
      e.style.setProperty('opacity', '0', 'important'); state.remoteSurfaces++;
    };
    for (const e of document.querySelectorAll('[role="menu"],[role="dialog"],[role="alert"]')) {
      if (/recording|consent|settings/i.test(e.textContent)) continue;
      const inside = offeredControls.filter(c => {
        const entry = offered.get(c.id); return entry && e.contains(entry.e);
      });
      if (inside.some(c => c.group === 'People' || c.group === 'Reactions')) suppressPaint(e);
    }
    for (const c of offeredControls.filter(c => c.group === 'Reactions')) suppressPaint(reactionSurface(offered.get(c.id)?.e));
    for (const c of offeredControls.filter(c => c.group === 'People' && /^more(?: options for | actions$|$)/i.test(c.label))) {
      let panel = null;
      for (let e=offered.get(c.id)?.e.parentElement; e && e !== document.body; e=e.parentElement) {
        const r = e.getBoundingClientRect();
        if (e.querySelector('video,audio,canvas') || r.width > innerWidth * .65) break;
        if (r.height > innerHeight * .25 && r.width > 160) panel = e;
      }
      suppressPaint(panel);
    }
    state.attention = issue;
  };
  const schedule = () => {
    if (queued) return;
    queued = true;
    queueMicrotask(refresh);
  };
  state.refresh = refresh;
  new MutationObserver(schedule).observe(document.documentElement, {
    subtree:true, childList:true, characterData:true, attributes:true,
    attributeFilter:['role','aria-label','aria-live','title','class','hidden'],
  });
  refresh();
})();
