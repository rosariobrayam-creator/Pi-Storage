/* Lightbox + broken-thumbnail flagging. No framework, no network fetches. */
(function () {
  "use strict";

  var grid = document.getElementById("grid");
  var dialog = document.getElementById("lightbox");
  if (!grid || !dialog || typeof dialog.showModal !== "function") return;

  var tiles = Array.prototype.slice.call(grid.querySelectorAll(".tile-btn"));
  var img = document.getElementById("lb-img");
  var download = document.getElementById("lb-download");
  var nameEl = dialog.querySelector(".lb-name");
  var subEl = dialog.querySelector(".lb-sub");
  var index = -1;

  function fmtDate(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return iso || "";
    return d.toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric",
      hour: "numeric", minute: "2-digit"
    });
  }

  function show(i) {
    if (i < 0 || i >= tiles.length) return;
    index = i;
    var t = tiles[i];
    var id = t.dataset.id;
    img.src = "/photo/" + id;
    img.alt = t.dataset.name || "";
    download.href = "/original/" + id;
    nameEl.textContent = t.dataset.name;
    subEl.textContent = [fmtDate(t.dataset.date), t.dataset.device, t.dataset.format]
      .filter(Boolean).join("  ·  ");
    preload(i + 1);
    preload(i - 1);
  }

  // The Pi transcodes HEIC on demand, so warming the neighbours makes arrow-key
  // paging feel instant instead of showing a blank frame each time.
  function preload(i) {
    if (i < 0 || i >= tiles.length) return;
    new Image().src = "/photo/" + tiles[i].dataset.id;
  }

  tiles.forEach(function (t, i) {
    t.addEventListener("click", function () {
      show(i);
      dialog.showModal();
    });
  });

  dialog.addEventListener("click", function (e) {
    if (e.target.hasAttribute("data-close") || e.target === dialog) dialog.close();
    else if (e.target.hasAttribute("data-prev")) show(index - 1);
    else if (e.target.hasAttribute("data-next")) show(index + 1);
  });

  dialog.addEventListener("keydown", function (e) {
    if (e.key === "ArrowRight") { e.preventDefault(); show(index + 1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); show(index - 1); }
  });

  // Free the decoded image when the lightbox closes.
  dialog.addEventListener("close", function () { img.removeAttribute("src"); });

  // If the server fell back to its placeholder, or the request failed outright,
  // mark the tile so it reads as "this one didn't render" instead of a mystery.
  grid.querySelectorAll(".tile img").forEach(function (el) {
    el.addEventListener("error", function () {
      var tile = el.closest(".tile");
      if (tile) tile.classList.add("is-broken");
    });
  });
})();
