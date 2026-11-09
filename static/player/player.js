// Xos slideshow player, shown full-screen in an iframe by the page view (#/page/<id>) and the media
// view (#/media/<id>).
//   ?page=<id>   a slideshow page: photos for their own seconds, videos until they end, looping
//   ?media=<id>  one photo or video from the media library (a video loops)
//   &sound=1     play video sound (signage is muted by default)
// Arrow Left / Right (keys, or {xos:"key"} messages from Xos Admin) step back and forth.
(function () {
  "use strict";
  var q = new URLSearchParams(location.search);
  var stage = document.getElementById("stage");
  var msgEl = document.getElementById("msg");
  var sound = q.get("sound") === "1";
  var slides = [];
  var at = -1;
  var timer = 0;
  var current = null;
  var single = q.has("media");

  function msg(text) {
    msgEl.textContent = text || "";
    msgEl.hidden = !text;
  }

  function getJSON(url) {
    return fetch(url, { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error(r.status === 404 ? "missing" : "HTTP " + r.status);
      return r.json();
    });
  }

  // The slideshow as a list of {kind, url, seconds}; media deleted since it was made is skipped.
  function load() {
    if (single) {
      return getJSON("/api/media").then(function (d) {
        var m = d.items.filter(function (x) { return x.id === q.get("media"); })[0];
        if (!m) throw new Error("missing");
        return { fit: "contain", items: [{ kind: m.kind, url: m.url, seconds: 0 }] };
      });
    }
    return getJSON("/api/pages/" + encodeURIComponent(q.get("page") || "")).then(function (p) {
      var media = p.media || {};
      return {
        fit: p.fit || "cover",
        items: (p.items || []).filter(function (i) { return media[i.media]; }).map(function (i) {
          var m = media[i.media];
          return { kind: m.kind, url: m.url, seconds: i.seconds || 10 };
        }),
      };
    });
  }

  function make(slide) {
    var layer = document.createElement("div");
    layer.className = "layer";
    var el;
    if (slide.kind === "video") {
      el = document.createElement("video");
      el.muted = !sound;
      el.playsInline = true;
      el.setAttribute("playsinline", "");
      el.preload = "auto";
      el.loop = single;
      el.src = slide.url;
    } else {
      el = document.createElement("img");
      el.className = "kb";
      el.decoding = "async";
      el.src = slide.url;
      if (slide.seconds) layer.style.setProperty("--kb", slide.seconds + 2 + "s");
    }
    el.alt = "";
    layer.appendChild(el);
    return layer;
  }

  var preloaded = null; // {index, layer}
  function layerFor(i) {
    if (preloaded && preloaded.index === i) { var l = preloaded.layer; preloaded = null; return l; }
    return make(slides[i]);
  }

  function show(i) {
    clearTimeout(timer);
    if (!slides.length) { timer = setTimeout(function () { refresh(function () { show(0); }); }, 30000); return; }
    at = ((i % slides.length) + slides.length) % slides.length;
    var slide = slides[at];
    var layer = layerFor(at);
    stage.appendChild(layer);
    var el = layer.firstChild;
    var old = current;
    current = layer;
    // Start the crossfade on the next frame so the transition runs.
    requestAnimationFrame(function () { requestAnimationFrame(function () { layer.classList.add("on"); }); });
    if (old) {
      old.classList.remove("on");
      setTimeout(function () {
        var v = old.querySelector("video");
        if (v) { v.pause(); v.removeAttribute("src"); v.load(); }
        old.remove();
      }, 1100);
    }
    if (slide.kind === "video") {
      el.currentTime = 0;
      var p = el.play();
      if (p && p.catch) p.catch(function () { el.muted = true; el.play().catch(function () {}); });
      if (!single) {
        el.onended = next;
        el.onerror = function () { timer = setTimeout(next, 1500); };
        // Backstop if "ended" never fires: the clip's length (once known) plus a little.
        var guard = function () {
          var d = isFinite(el.duration) && el.duration > 0 ? el.duration : 600;
          clearTimeout(timer);
          timer = setTimeout(next, (d + 3) * 1000);
        };
        if (el.readyState >= 1) guard(); else el.onloadedmetadata = guard;
      }
    } else if (!single) {
      timer = setTimeout(next, slide.seconds * 1000);
    }
    // Get the next slide's file loading now.
    if (slides.length > 1) {
      var n = (at + 1) % slides.length;
      preloaded = { index: n, layer: make(slides[n]) };
    }
  }

  function next() {
    // Each time round, pick up edits made in Admin since the last loop.
    if (at === slides.length - 1 && !single) {
      return refresh(function () { show(0); });
    }
    show(at + 1);
  }

  function prev() { show(at - 1); }

  function apply(d) {
    document.body.className = "fit-" + (d.fit === "contain" ? "contain" : "cover");
    var changed = JSON.stringify(d.items) !== JSON.stringify(slides);
    slides = d.items;
    if (changed) preloaded = null;
    if (!slides.length) {
      msg("This slideshow has no photos or videos yet. Add some in Xos Admin → Content.");
    } else {
      msg("");
    }
    return changed;
  }

  function refresh(then) {
    load().then(function (d) { apply(d); then(); }).catch(function () { then(); });
  }

  function start() {
    load().then(function (d) {
      apply(d);
      if (slides.length) show(0);
      else setTimeout(start, 30000);
    }).catch(function (e) {
      msg(e.message === "missing" ? "This photo, video or slideshow was deleted." : "Can't reach Xos. Retrying…");
      setTimeout(start, 30000);
    });
  }

  function key(k) {
    if (k === "ArrowRight") next();
    else if (k === "ArrowLeft") prev();
  }
  window.addEventListener("keydown", function (e) { key(e.key); });
  window.addEventListener("message", function (e) {
    if (e.data && e.data.xos === "key") key(e.data.key);
  });

  start();
})();
