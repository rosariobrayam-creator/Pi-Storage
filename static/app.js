/* Lightbox, selection mode, and broken-thumbnail flagging.
   No framework, no network fetches. */
(function () {
  "use strict";

  var grid = document.getElementById("grid");
  if (!grid) return;

  var tiles = Array.prototype.slice.call(grid.querySelectorAll(".tile-btn"));

  /* ------------------------------------------------------------ lightbox */

  var dialog = document.getElementById("lightbox");
  var hasDialog = dialog && typeof dialog.showModal === "function";
  var index = -1;

  if (hasDialog) {
    var img = document.getElementById("lb-img");
    var download = document.getElementById("lb-download");
    var save = document.getElementById("lb-save");
    var nameEl = dialog.querySelector(".lb-name");
    var subEl = dialog.querySelector(".lb-sub");

    var fmtDate = function (iso) {
      var d = new Date(iso);
      if (isNaN(d)) return iso || "";
      return d.toLocaleString(undefined, {
        year: "numeric", month: "short", day: "numeric",
        hour: "numeric", minute: "2-digit"
      });
    };

    // The Pi transcodes HEIC on demand, so warming the neighbours makes
    // arrow-key paging feel instant instead of showing a blank frame.
    var preload = function (i) {
      if (i < 0 || i >= tiles.length) return;
      new Image().src = "/photo/" + tiles[i].dataset.id;
    };

    var show = function (i) {
      if (i < 0 || i >= tiles.length) return;
      index = i;
      var t = tiles[i];
      var id = t.dataset.id;
      img.src = "/photo/" + id;
      img.alt = t.dataset.name || "";
      download.href = "/original/" + id;
      // inline=1 so iOS long-press offers "Add to Photos" with the real file.
      save.href = "/original/" + id + "?inline=1";
      nameEl.textContent = t.dataset.name;
      subEl.textContent = [fmtDate(t.dataset.date), t.dataset.device, t.dataset.format]
        .filter(Boolean).join("  ·  ");
      preload(i + 1);
      preload(i - 1);
    };

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

    var openLightbox = function (i) { show(i); dialog.showModal(); };
  }

  /* ------------------------------------------------------ selection mode */

  var toggleBtn = document.getElementById("select-toggle");
  var selbar = document.getElementById("selbar");
  var countEl = document.getElementById("selbar-count");
  var dlLink = document.getElementById("sel-download");
  var allBtn = document.getElementById("sel-all");
  var cancelBtn = document.getElementById("sel-cancel");

  var selectMode = false;
  var selected = [];

  function isSelected(id) { return selected.indexOf(id) !== -1; }

  function refresh() {
    var n = selected.length;
    countEl.textContent = n + (n === 1 ? " selected" : " selected");
    dlLink.href = n ? "/export.zip?ids=" + selected.join(",") : "#";
    // A zip of nothing is a 404 from the server; don't let them ask for it.
    dlLink.classList.toggle("is-disabled", n === 0);
    allBtn.textContent = n === tiles.length ? "Select none" : "Select all";
  }

  function setMode(on) {
    selectMode = on;
    selected = [];
    grid.classList.toggle("selecting", on);
    tiles.forEach(function (t) {
      t.closest(".tile").classList.remove("is-selected");
      t.setAttribute("aria-pressed", "false");
    });
    selbar.hidden = !on;
    document.body.classList.toggle("has-selbar", on);
    toggleBtn.textContent = on ? "Done" : "Select";
    refresh();
  }

  if (toggleBtn && selbar) {
    toggleBtn.addEventListener("click", function () { setMode(!selectMode); });
    cancelBtn.addEventListener("click", function () { setMode(false); });

    allBtn.addEventListener("click", function () {
      var selectAll = selected.length !== tiles.length;
      selected = [];
      tiles.forEach(function (t) {
        var on = selectAll;
        t.closest(".tile").classList.toggle("is-selected", on);
        t.setAttribute("aria-pressed", on ? "true" : "false");
        if (on) selected.push(t.dataset.id);
      });
      refresh();
    });

    dlLink.addEventListener("click", function (e) {
      if (!selected.length) e.preventDefault();
    });
  }

  /* ---------------------------------------------------------- tile clicks */

  tiles.forEach(function (t, i) {
    t.addEventListener("click", function () {
      if (selectMode) {
        var id = t.dataset.id;
        var tile = t.closest(".tile");
        if (isSelected(id)) {
          selected.splice(selected.indexOf(id), 1);
          tile.classList.remove("is-selected");
          t.setAttribute("aria-pressed", "false");
        } else {
          selected.push(id);
          tile.classList.add("is-selected");
          t.setAttribute("aria-pressed", "true");
        }
        refresh();
      } else if (hasDialog) {
        openLightbox(i);
      }
    });
  });

  /* --------------------------------------------------- broken thumbnails */

  // If the server fell back to its placeholder, or the request failed outright,
  // mark the tile so it reads as "this one didn't render" instead of a mystery.
  grid.querySelectorAll(".tile img").forEach(function (el) {
    el.addEventListener("error", function () {
      var tile = el.closest(".tile");
      if (tile) tile.classList.add("is-broken");
    });
  });
})();
