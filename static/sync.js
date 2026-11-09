// Xos shared clock: screens set their clock against the server's so that every screen showing
// the same page (the welcome page, a photo slideshow) is on the same scene at the same moment.
// XosClock.now() is the server's time in ms; XosClock.ready resolves once it's measured (or
// after 2 s without a server answer, when the screen's own clock is used; Fire TVs keep theirs
// set by network time, so that is off by well under a second).
(function () {
  "use strict";
  var offset = 0;

  // A few round trips; the quickest one gives the best estimate of the offset.
  function measure() {
    var best = null;
    function one() {
      var t0 = Date.now();
      return fetch("/api/time", { cache: "no-store" })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(function (d) {
          var t1 = Date.now(), rtt = t1 - t0;
          if (typeof d.now === "number" && (!best || rtt < best.rtt)) {
            best = { rtt: rtt, offset: d.now + rtt / 2 - t1 };
          }
        });
    }
    return one().then(one).then(one).catch(function () {}).then(function () {
      if (best) offset = best.offset;
    });
  }

  var ready = Promise.race([measure(), new Promise(function (r) { setTimeout(r, 2000); })]);
  setInterval(measure, 10 * 60 * 1000);

  window.XosClock = {
    now: function () { return Date.now() + offset; },
    ready: ready,
  };
})();
