(function () {
  var L = window.LEGAL || {};
  var missing = false;
  document.querySelectorAll("[data-legal]").forEach(function (el) {
    var v = L[el.getAttribute("data-legal")] || "";
    if (!v || /^REPLACE/.test(v)) { missing = true; el.classList.add("missing"); }
    el.textContent = v || "(not provided)";
  });
  if (missing) {
    var b = document.createElement("div");
    b.className = "legal-warning";
    b.textContent = "Setup needed: the site owner has not finished filling in legal-config.js yet.";
    document.body.insertBefore(b, document.body.firstChild);
  }
})();
