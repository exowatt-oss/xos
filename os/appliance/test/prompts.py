"""Real headed Chromium prompt check, inside the Debian appliance test container.

Uses an isolated profile and a separate headless Wayland output, never the room's profile.
No fake UI: camera/microphone are fake devices but permission handling is real Chromium.
"""
import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location("prompt_browser", "/xos/os/appliance/xos_room_linux.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

with tempfile.TemporaryDirectory(prefix="xos-prompts-") as directory:
    os.environ.update(XDG_RUNTIME_DIR=directory, WLR_BACKENDS="headless", WLR_RENDERER="pixman",
                      WLR_LIBINPUT_NO_DEVICES="1")
    browser = module.Browser(Path(directory) / "profile", "about:blank", ui_scale=1.5, extra=[
        "--no-sandbox", "--ignore-certificate-errors", "--use-fake-device-for-media-stream",
        "--host-resolver-rules=MAP meet.google.com:443 127.0.0.1:8443, MAP app.zoom.us:443 127.0.0.1:8443, MAP zoom.us:443 127.0.0.1:8443",
        "--isolate-origins=https://zoom.us",
    ])
    try:
        browser.start()
        assert browser.evaluate("typeof window.__xosMeetingDisplay") == 'undefined'
        browser.navigate("https://meet.google.com/abc-defg-hij")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if browser.evaluate("location.hostname + '|' + document.readyState") == "meet.google.com|complete":
                break
            time.sleep(.2)
        ratio = float(browser.evaluate("devicePixelRatio"))
        assert abs(ratio - 1.8) < .01, ratio
        size = json.loads(browser.evaluate("JSON.stringify({width:innerWidth,screen:screen.width})"))
        assert size['width'] < size['screen'] * .75, size
        print("PASS real Chromium: 180% Meet page zoom enlarges controls and reduces the logical viewport", flush=True)
        browser.evaluate("""(() => {
          window.promptCheck = {};
          const button = document.createElement('button'); button.textContent = 'Probe prompts';
          button.style.cssText = 'position:fixed;top:10px;left:10px;z-index:99999';
          button.onclick = (event) => {
            promptCheck.trusted = event.isTrusted;
            navigator.mediaDevices.getDisplayMedia({video:true}).then(stream => {
              stream.getTracks().forEach(t => t.stop()); promptCheck.capture = 'UNEXPECTED_GRANTED';
            }, e => { promptCheck.capture = e.name; });
            navigator.mediaDevices.getUserMedia({audio:true,video:true}).then(stream => {
              stream.getTracks().forEach(t => t.stop()); promptCheck.av = 'granted';
            }, e => { promptCheck.av = e.name; });
            if (typeof Notification === 'undefined') promptCheck.notifications = 'disabled';
            else Notification.requestPermission().then(value => { promptCheck.notifications = value; });
          };
          document.body.appendChild(button); return 'ready';
        })()""")
        browser.click(50, 20)
        deadline = time.monotonic() + 10
        result = {}
        while time.monotonic() < deadline:
            result = json.loads(browser.evaluate("JSON.stringify(window.promptCheck)"))
            if all(key in result for key in ("av", "capture", "notifications")):
                break
            time.sleep(.2)
        assert result.get("trusted") is True, result
        assert result.get("av") == "granted", result
        assert result.get("capture") == "NotAllowedError", result
        assert result.get("notifications") in ("denied", "disabled"), result
        # A real user gesture must settle without choosing or dismissing any browser UI.
        print("PASS real Chromium: AV granted, capture rejected, notifications blocked without prompts", flush=True)
        # Real Chromium DOM/observer/CSS test, using explicit fixtures rather than a provider login.
        browser.evaluate("""(() => {
          const fixture = document.createElement('section'); fixture.id = 'display-fixture';
          fixture.innerHTML = `<div id="notice" role="status" style="position:fixed;left:10px;bottom:10px">You joined the meeting</div>`;
          document.body.append(fixture); return 'ready';
        })()""")
        assert browser.evaluate("getComputedStyle(document.querySelector('#notice')).display") != 'none'
        browser.evaluate("""(() => {
          const fixture = document.querySelector('#display-fixture');
          fixture.insertAdjacentHTML('beforeend', `
            <button aria-label="Leave call">Leave</button>
            <div id="ready" role="dialog"><h2>Your meeting’s ready</h2><a href="#">Copy meeting link</a></div>
            <div id="caption" aria-live="polite" style="position:fixed;left:10px;bottom:50px">A spoken sentence</div>
            <div id="admit" role="alert" style="position:fixed;left:10px;bottom:100px">Someone wants to join<button>Admit</button></div>
            <button id="gemini-aria" aria-label="Gemini uses meeting conversations to answer questions" style="position:fixed;left:10px;bottom:250px;width:40px;height:40px">i</button><div id="gemini-tip" role="tooltip" style="position:fixed;left:10px;bottom:250px;width:300px">Gemini uses meeting conversations to answer questions<button>Close</button></div><div id="gemini" role="dialog"><h2>Gemini is available in Meet</h2><button>Try it</button><button>Got it</button></div>
            <div id="plain-gemini" style="position:fixed;left:10px;bottom:250px;width:300px"><h2>Gemini is available</h2><button>Got it</button></div>
            <div id="gemini-consent" role="dialog">Gemini is available. Recording consent required<button>Accept</button></div>
            <div id="settings" role="dialog"><h2>Audio settings</h2><button>Done</button></div>
            <div id="plain-ready" style="position:fixed;left:10px;bottom:200px;width:300px"><h2>Your meeting's ready</h2><a href="#">Copy meeting link</a></div>
            <div id="connection" role="alert" style="position:fixed;left:10px;bottom:160px">Network unavailable</div>`);
          return 'added';
        })()""")
        # Read directly through CDP (not Browser.evaluate's fallback install) to exercise the observer.
        observed = browser.send("Runtime.evaluate", {"expression": """new Promise(resolve => setTimeout(() => resolve(JSON.stringify({
          hidden: ['notice','ready','plain-ready','connection','gemini','plain-gemini','gemini-tip','gemini-aria'].every(id => getComputedStyle(document.getElementById(id)).display === 'none'),
          preserved: ['caption','admit','settings','gemini-consent'].every(id => getComputedStyle(document.getElementById(id)).display !== 'none'),
          attention: window.__xosMeetingDisplay.attention,
          active: [...document.querySelectorAll('button')].map(e=>e.getAttribute('aria-label')||e.textContent),
        })), 30))""", "awaitPromise": True, "returnByValue": True})
        observed = json.loads(observed['result']['value'])
        assert observed['hidden'] and observed['preserved'] and observed['attention'] == 'connection', observed
        browser.evaluate("""(() => {
          const notice = document.querySelector('#notice');
          notice.innerHTML = 'Someone wants to join<button>Admit</button>';
          document.querySelector('#ready').remove(); document.querySelector('#connection').remove();
          return 'changed';
        })()""")
        assert browser.evaluate("getComputedStyle(document.querySelector('#notice')).display") != 'none'
        browser.evaluate("document.querySelector('#display-fixture button[aria-label]').remove(); 'left'")
        assert browser.evaluate("getComputedStyle(document.querySelector('#notice')).display") != 'none'
        browser.evaluate("document.querySelector('#display-fixture').remove(); 'removed'")
        print("PASS real Chromium: passive meeting notices and invitation cards hidden; captions, admission and settings retained; failures routed to controller", flush=True)
        # The room saves the user's explicit Gemini opt-out, rather than only hiding its card.
        browser.evaluate("""(() => {
          const f=document.createElement('section'); f.id='gemini-preference-fixture';
          f.innerHTML=`<button aria-label="Leave call">Leave</button>
            <div id="gemini-optout" role="dialog" style="position:fixed;left:20px;top:80px;width:340px"><h2>Gemini is available to answer questions</h2><button><span class="google-symbols">visibility_off</span>Don't show again</button><button>Try it</button></div>
            <div id="unrelated-optout" role="dialog">A browser tip<button>Don't show again</button></div>
            <div id="consent-optout" role="dialog">Gemini is available. Recording consent required<button>Don't show again</button></div>`;
          window.geminiClicks=[];
          f.querySelector('#gemini-optout button').onclick=e=>{geminiClicks.push(e.isTrusted);e.target.parentElement.remove();};
          document.body.append(f); return 'ready';
        })()""")
        assert browser.evaluate("getComputedStyle(document.querySelector('#gemini-optout')).opacity") == '0'
        controller = module.ChromiumMeet(browser, 'about:blank')
        controller._dismiss_gemini()
        assert json.loads(browser.evaluate('JSON.stringify(window.geminiClicks)')) == [True]
        assert browser.evaluate("window.__xosMeetingDisplay.geminiDismissal()") == ''
        assert browser.evaluate("!!document.querySelector('#unrelated-optout button') && !!document.querySelector('#consent-optout button')") == 'true'
        browser.evaluate("""(() => {
          const card=document.createElement('div');card.id='gemini-checkbox';card.setAttribute('role','dialog');
          card.style.cssText='position:fixed;left:20px;top:80px;width:340px';
          card.innerHTML=`<h2><span>Gemini</span><span>is available</span></h2><p>to answer questions using recording transcripts</p><input id="gemini-check" type="checkbox"><label for="gemini-check">Don’t show again</label><button>Got it</button>`;
          card.querySelector('input').onchange=e=>{window.geminiChecked={checked:e.target.checked,trusted:e.isTrusted};};
          card.querySelector('button').onclick=e=>{if(card.querySelector('input').checked){geminiClicks.push(e.isTrusted);card.remove();}};
          document.querySelector('#gemini-preference-fixture').append(card);return 'again';
        })()""")
        controller._dismiss_gemini()
        assert json.loads(browser.evaluate('JSON.stringify(window.geminiChecked)')) == {'checked':True,'trusted':True}
        assert json.loads(browser.evaluate('JSON.stringify(window.geminiClicks)')) == [True,True]
        assert browser.evaluate("document.querySelector('#gemini-checkbox') === null") == 'true'
        browser.evaluate("""(() => {
          const card=document.createElement('div');card.id='gemini-labelled';card.setAttribute('role','dialog');
          card.setAttribute('aria-describedby','gemini-description');card.style.cssText='position:fixed;left:20px;top:80px;width:340px';
          card.innerHTML=`<input id="gemini-enabled" type="checkbox" checked><label for="gemini-enabled">Don't show again</label><button>Got it</button>`;
          const description=document.createElement('span');description.id='gemini-description';description.hidden=true;description.textContent='Gemini is available to answer questions';
          card.querySelector('input').onchange=()=>{window.geminiUnticked=true;};
          card.querySelector('button').onclick=e=>{geminiClicks.push(e.isTrusted);card.remove();};
          document.querySelector('#gemini-preference-fixture').append(description,card);return 'aria-labelled';
        })()""")
        controller._dismiss_gemini()
        assert json.loads(browser.evaluate('JSON.stringify(window.geminiClicks)')) == [True,True,True]
        assert browser.evaluate('window.geminiUnticked === undefined') == 'true'
        assert controller.gemini_dismissals == 4
        browser.evaluate("document.querySelector('#gemini-preference-fixture').remove(); 'removed'")
        print("PASS real Chromium: Gemini Don't show again and checkbox confirmation use trusted input; unrelated tips and consent decisions untouched", flush=True)
        # A provider roster, including duplicate actions and disabled host permissions.
        browser.evaluate("""(() => {
          const f = document.createElement('section'); f.id = 'controls-fixture';
          f.innerHTML = `<button aria-label="Leave call">Leave</button>
            <button id="icon-people" aria-label="2"><span aria-hidden="true">group</span>2</button><span id="people-name">People</span><button aria-describedby="people-name">2</button><button aria-label="More options for Alice">Options</button><button aria-label="Show everyone (2)">People</button><button aria-label="More options">More</button><button aria-label="Raise hand (Ctrl + Alt + h)">Hand</button>
            <div role="listitem" data-participant-id="alice-original"><span data-participant-name="Alice">Alice</span><button id="alice-remove"><span aria-hidden="true">person_remove</span>Remove from the call</button><button>Lower Alice’s hand</button></div>
            <div role="listitem"><span>Bob</span><span class="google-symbols">mic_off</span><div role="tooltip">You cannot unmute Bob</div><button>More actions</button><button id="bob-remove">Remove</button><button disabled>Mute</button></div>
            <div role="alert" style="position:fixed;left:10px;top:120px;width:260px">Casey wants to join<button id="casey-admit">Admit Casey</button></div>
            <div role="menu" style="position:fixed;left:310px;top:120px;width:120px"><button id="reaction" aria-label="Thumbs up">👍</button></div>
            <section id="unlabelled-roster"><div><span>Dana</span><button>More actions</button></div><div><span>Erin</span><button>More actions</button></div></section><button id="unscoped-remove">Remove</button><button id="unscoped-reaction" aria-label="Thumbs up">👍</button>`;
          f.querySelectorAll('button').forEach(e => e.onclick = event => { window.controlClicked = {id:e.id,trusted:event.isTrusted}; });
          document.body.append(f); return 'ready';
        })()""")
        controls = json.loads(browser.evaluate("JSON.stringify(window.__xosMeetingDisplay.controls())"))
        assert len([c for c in controls if c['label'] == 'People']) == 2, controls
        assert any(c['label'] == 'More options for Alice' and c['context'] == 'Alice' for c in controls), controls
        assert any(c['label'] == 'Show everyone (2)' for c in controls), controls
        assert any(c['label'] == 'More options' and not c['context'] for c in controls), controls
        assert any(c['label'] == 'Raise hand' for c in controls), controls
        alice = next(c for c in controls if c['label'] == 'Remove from the call' and c['context'] == 'Alice')
        bob = next(c for c in controls if c['label'] == 'Remove' and c['context'] == 'Bob')
        assert alice['id'] != bob['id'] and alice['destructive']
        assert {c['context'] for c in controls if c['label'] == 'More actions'} == {'Bob','Dana','Erin'}, controls
        assert any(c['label'] == 'Lower Alice’s hand' and c['context'] == 'Alice' for c in controls), controls
        assert not any(c['label'] == 'Mute' for c in controls), controls
        assert len([c for c in controls if c['label'].startswith('Remove')]) == 2, controls
        assert any(c['label'] == 'Admit Casey' and 'Casey' in c['context'] for c in controls), controls
        assert browser.evaluate("getComputedStyle(document.querySelector('#casey-admit').parentElement).opacity") == '0'
        assert browser.evaluate("getComputedStyle(document.querySelector('#reaction').parentElement).opacity") == '0'

        options = next(c for c in controls if c['label'] == 'More options for Alice')
        browser.evaluate('window.__xosMeetingDisplay.resolveControl(' + json.dumps(options['id']) + ')')
        controls = json.loads(browser.evaluate("JSON.stringify(window.__xosMeetingDisplay.controls())"))
        assert all(not c['context'] for c in controls if c['group'] == 'Reactions'), controls
        diagnostics = json.loads(browser.evaluate('JSON.stringify(window.__xosMeetingDisplay.controlDiagnostics())'))
        assert any(d.get('kind')=='unscoped-reaction' and d['label']=='Thumbs up' for d in diagnostics), diagnostics
        reaction = next(c for c in controls if c['label'] == 'Thumbs up')
        selector = browser.evaluate('window.__xosMeetingDisplay.resolveControl(' + json.dumps(reaction['id']) + ')')
        controller = module.ChromiumMeet(browser, 'about:blank')
        controller._tool('browser_click', {'selector':selector})
        assert json.loads(browser.evaluate('JSON.stringify(window.controlClicked)')) == {'id':'reaction','trusted':True}
        browser.evaluate("document.addEventListener('keydown', e => { if (e.key === 'Escape') window.backTrusted = e.isTrusted; }); 'ready'")
        controller._tool('browser_press_key', {'key':'Escape'})
        assert browser.evaluate('window.backTrusted') == 'true'
        browser.evaluate("document.querySelector('[data-participant-id=alice-original]').setAttribute('data-participant-id','different-id'); 'reused'")
        assert browser.evaluate('window.__xosMeetingDisplay.resolveControl(' + json.dumps(alice['id']) + ')') == ''
        controls = json.loads(browser.evaluate("JSON.stringify(window.__xosMeetingDisplay.controls())"))
        alice = next(c for c in controls if c['label'] == 'Remove from the call' and c['context'] == 'Alice')
        browser.evaluate("document.querySelector('[data-participant-name=Alice]').textContent = 'Different person'; document.querySelector('[data-participant-name=Alice]').removeAttribute('data-participant-name'); 'changed'")
        assert browser.evaluate('window.__xosMeetingDisplay.resolveControl(' + json.dumps(alice['id']) + ')') == ''
        browser.evaluate("document.getElementById('bob-remove').disabled = true; 'disabled'")
        assert browser.evaluate('window.__xosMeetingDisplay.resolveControl(' + json.dumps(bob['id']) + ')') == ''
        browser.evaluate("document.getElementById('controls-fixture').remove(); 'removed'")
        assert browser.evaluate('window.__xosMeetingDisplay.resolveControl(' + json.dumps(reaction['id']) + ')') == ''
        print('PASS real Chromium: permission-aware named controls, admission, trusted reactions and stale participant tokens', flush=True)
        browser.navigate("https://app.zoom.us/wc/frame-shell")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if browser.evaluate("document.querySelector('#end') ? 'ready' : 'loading'") == "ready":
                    break
            except module.CdpError:
                pass
            time.sleep(.2)
        else:
            raise AssertionError("Zoom meeting frame controls not readable")
        browser.send("Runtime.evaluate", {"expression": """(() => {
          const header = document.createElement('div'); header.textContent = 'Account shell header';
          header.style.cssText = 'position:fixed;left:0;top:0;width:100%;height:60px;z-index:2000;background:white';
          document.body.append(header);
        })()"""})
        browser.evaluate("'refresh fitting'")
        assert browser.send("Runtime.evaluate", {"expression": "document.elementFromPoint(10,20)?.tagName", "returnByValue": True})['result']['value'] == 'IFRAME'
        fitted = browser.send("Runtime.evaluate", {"expression": """(() => {
          const frame = document.querySelector('iframe'); const r = frame.getBoundingClientRect();
          return {x:r.x,y:r.y,width:r.width,height:r.height,viewportWidth:innerWidth,viewportHeight:innerHeight,border:getComputedStyle(frame).borderTopWidth};
        })()""", "returnByValue": True})['result']['value']
        assert fitted['x'] == 0 and fitted['y'] == 0 and fitted['border'] == '0px', fitted
        assert abs(fitted['width'] - fitted['viewportWidth']) < 2 and abs(fitted['height'] - fitted['viewportHeight']) < 2, fitted
        controller = module.ChromiumMeet(browser, 'about:blank')
        controls = json.loads(browser.evaluate('JSON.stringify(window.__xosMeetingDisplay.controls())'))
        opener = next(c for c in controls if c.get('kind') == 'reaction_opener')
        controller.control(opener['id'])
        choices = json.loads(browser.evaluate('JSON.stringify(window.__xosMeetingDisplay.controls())'))
        assert len([c for c in choices if c['group']=='Reactions']) == 6, choices
        clap = next(c for c in choices if c.get('reaction',{}).get('key')=='clap')
        assert clap['reaction']['emoji']=='👏'
        assert browser.evaluate("getComputedStyle(document.querySelector('.reactions-menu')).opacity") == '0'
        controller.control(clap['id'])
        assert json.loads(browser.evaluate("document.body.getAttribute('data-last-reaction')")) == {'name':'clap','trusted':True}
        assert browser.evaluate('window.__xosMeetingDisplay.resolveControl(' + json.dumps(clap['id']) + ')') == ''
        print('PASS real Chromium: Zoom class-based reaction popover, canonical emoji choices and trusted reaction input inside its fitted iframe',flush=True)
        box = json.loads(browser.evaluate("JSON.stringify((()=>{const r=document.querySelector('#end').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})())"))
        browser.click(box['x'], box['y'])
        assert browser.evaluate("document.querySelector('#all') ? 'clicked' : 'missing'") == 'clicked'
        print("PASS real Chromium: borderless fitted Zoom iframe receives trusted input at scaled coordinates", flush=True)
        browser.navigate("https://app.zoom.us/wc/shadow-frame-shell")
        deadline = time.monotonic()+15
        while time.monotonic()<deadline:
            try:
                if browser.evaluate("document.querySelector('#end') ? 'ready' : 'loading'") == 'ready':
                    break
            except module.CdpError:
                pass
            time.sleep(.2)
        else:
            raise AssertionError("Redirected shadow meeting frame is not readable")
        inner = json.loads(browser.evaluate("JSON.stringify({width:innerWidth,height:innerHeight})"))
        outer = browser.root_viewport(browser.home)
        assert abs(inner['width']-outer['width']) < 2 and abs(inner['height']-outer['height']) < 2, (inner,outer)
        offset = browser.meeting_frame()[2:]
        assert abs(offset[0]) < 1 and abs(offset[1]) < 1, offset
        box = json.loads(browser.evaluate("JSON.stringify((()=>{const r=document.querySelector('#end').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})())"))
        browser.click(box['x'],box['y'])
        assert browser.evaluate("document.querySelector('#all') ? 'clicked' : 'missing'") == 'clicked'
        print("PASS real Chromium: redirected closed-shadow Zoom frame fills viewport and receives input",flush=True)
        popup = browser.cdp.call("Target.createTarget", {"url":"https://app.zoom.us/wc/9876543210/start", "newWindow":True})['targetId']
        try:
            deadline = time.monotonic()+5
            while time.monotonic()<deadline:
                if browser.fullscreen(popup):
                    break
                time.sleep(.1)
            else:
                raise AssertionError("Zoom popup window did not become fullscreen")
            print("PASS real Chromium/Cage: Zoom popup window promoted to fullscreen", flush=True)
        finally:
            browser.close_page(popup)
    finally:
        browser.stop()
