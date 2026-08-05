/* Three behaviours, and nothing else on the page moves.
 *
 * The terminal client binds `t` for light/dark and `g` for the repo, so this
 * page binds them too — a key that works in the tool and does nothing here
 * would be the sort of dead interaction this design is trying to avoid.
 */

(function () {
  "use strict";

  /* ── Theme ───────────────────────────────────────────────────────────── */

  var root = document.documentElement;
  var buttons = document.querySelectorAll(".js-theme");
  var stored = null;

  try { stored = localStorage.getItem("codejury-theme"); } catch (e) { /* private mode */ }
  if (stored === "light" || stored === "dark") root.setAttribute("data-theme", stored);

  function current() {
    var set = root.getAttribute("data-theme");
    if (set) return set;
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }

  function sync() {
    var light = current() === "light" ? "true" : "false";
    Array.prototype.forEach.call(buttons, function (b) { b.setAttribute("aria-pressed", light); });
  }

  function toggle() {
    var next = current() === "light" ? "dark" : "light";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("codejury-theme", next); } catch (e) { /* private mode */ }
    sync();
  }

  sync();
  Array.prototype.forEach.call(buttons, function (b) { b.addEventListener("click", toggle); });

  document.addEventListener("keydown", function (event) {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    var tag = (event.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || event.target.isContentEditable) return;
    if (event.key === "t") toggle();
    if (event.key === "g") window.location.href = "https://github.com/krishagarwal314/CodeJury";
  });

  /* ── The two seats ───────────────────────────────────────────────────── */

  var seats = document.querySelector(".seats");
  if (seats) {
    var buttons = seats.querySelectorAll(".switch button");
    Array.prototype.forEach.call(buttons, function (b) {
      b.addEventListener("click", function () {
        var mode = b.getAttribute("data-seat");
        seats.setAttribute("data-mode", mode);
        Array.prototype.forEach.call(buttons, function (other) {
          other.setAttribute("aria-pressed", other === b ? "true" : "false");
        });
      });
    });
  }

  /* ── Stars and forks, live ───────────────────────────────────────────── */

  /* The one request this page makes off its own origin. It is unauthenticated,
   * so GitHub allows 60 an hour per IP — the answer is cached for half an hour
   * rather than asked again on every page view. If it fails, is rate-limited,
   * or the reader is offline, the counts stay hidden and the button still goes
   * to the repo: the same rule the tool follows, degrade instead of break. */

  var REPO = "krishagarwal314/CodeJury";
  var CACHE_KEY = "codejury-repo";
  var CACHE_MS = 30 * 60 * 1000;

  var counts = document.querySelector("[data-counts]");
  var starsEl = document.querySelector("[data-stars]");
  var forksEl = document.querySelector("[data-forks]");

  function show(stars, forks) {
    if (!counts || typeof stars !== "number" || typeof forks !== "number") return;
    starsEl.textContent = stars.toLocaleString("en");
    forksEl.textContent = forks.toLocaleString("en");
    counts.hidden = false;
  }

  function cached() {
    try {
      var raw = localStorage.getItem(CACHE_KEY);
      if (!raw) return null;
      var value = JSON.parse(raw);
      return Date.now() - value.at < CACHE_MS ? value : null;
    } catch (e) { return null; }
  }

  if (counts && window.fetch) {
    var hit = cached();
    if (hit) {
      show(hit.stars, hit.forks);
    } else {
      fetch("https://api.github.com/repos/" + REPO, {
        headers: { Accept: "application/vnd.github+json" }
      }).then(function (response) {
        if (!response.ok) throw new Error(response.status);
        return response.json();
      }).then(function (data) {
        show(data.stargazers_count, data.forks_count);
        try {
          localStorage.setItem(CACHE_KEY, JSON.stringify({
            stars: data.stargazers_count, forks: data.forks_count, at: Date.now()
          }));
        } catch (e) { /* private mode */ }
      }).catch(function () { /* the button still works */ });
    }
  }

  /* ── Which section am I in ───────────────────────────────────────────── */

  var links = {};
  Array.prototype.forEach.call(document.querySelectorAll(".rail nav a"), function (a) {
    links[a.getAttribute("href").slice(1)] = a;
  });

  var sections = document.querySelectorAll("section[id]");
  if (sections.length && "IntersectionObserver" in window) {
    var seen = {};
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) { seen[entry.target.id] = entry.isIntersecting; });
      var active = null;
      Array.prototype.forEach.call(sections, function (section) {
        if (seen[section.id] && !active) active = section.id;
      });
      Object.keys(links).forEach(function (id) {
        if (id === active) links[id].setAttribute("aria-current", "true");
        else links[id].removeAttribute("aria-current");
      });
    }, { rootMargin: "-10% 0px -70% 0px" });
    Array.prototype.forEach.call(sections, function (section) { observer.observe(section); });
  }
})();
