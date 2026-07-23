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
    var video = document.getElementById("lb-video");
    var download = document.getElementById("lb-download");
    var save = document.getElementById("lb-save");
    var liveBtn = document.getElementById("lb-live");
    var locLink = document.getElementById("lb-loc");
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
    // Videos are skipped: speculatively pulling megabytes over Tailscale
    // to maybe watch a clip is a bad trade.
    var preload = function (i) {
      if (i < 0 || i >= tiles.length) return;
      if (tiles[i].dataset.type === "video") return;
      new Image().src = "/photo/" + tiles[i].dataset.id;
    };

    var stopVideo = function () {
      video.pause();
      // Detach the source for real -- iOS keeps the decoder alive otherwise.
      video.removeAttribute("src");
      video.load();
    };

    var show = function (i) {
      if (i < 0 || i >= tiles.length) return;
      index = i;
      var t = tiles[i];
      var id = t.dataset.id;
      var isVideo = t.dataset.type === "video";
      stopVideo();
      if (isVideo) {
        video.src = "/media/" + id;
        img.removeAttribute("src");
      } else {
        img.src = "/photo/" + id;
      }
      img.hidden = isVideo;
      video.hidden = !isVideo;
      img.alt = isVideo ? "" : (t.dataset.name || "");
      download.href = "/original/" + id;
      // inline=1 so iOS long-press offers "Add to Photos" with the real file.
      save.href = "/original/" + id + "?inline=1";
      nameEl.textContent = t.dataset.name;
      // Capture details when we have them; upload date and device otherwise.
      subEl.textContent = [
        fmtDate(t.dataset.taken || t.dataset.date),
        t.dataset.camera || t.dataset.device,
        t.dataset.format
      ].filter(Boolean).join("  ·  ");
      if (t.dataset.lat) {
        locLink.href = "https://maps.apple.com/?ll=" + t.dataset.lat + "," + t.dataset.lon
          + "&q=" + encodeURIComponent(t.dataset.name || "Photo");
        locLink.hidden = false;
      } else {
        locLink.hidden = true;
      }
      liveBtn.hidden = !t.dataset.live;
      preload(i + 1);
      preload(i - 1);
    };

    // A Live Photo replays its paired clip in place of the still, then
    // falls back to the still when it ends.
    liveBtn.addEventListener("click", function () {
      var t = tiles[index];
      if (!t || !t.dataset.live) return;
      video.src = "/media/" + t.dataset.live;
      video.hidden = false;
      img.hidden = true;
      video.play();
    });

    video.addEventListener("ended", function () {
      var t = tiles[index];
      if (t && t.dataset.type !== "video") {  // it was a Live Photo replay
        stopVideo();
        video.hidden = true;
        img.hidden = false;
      }
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

    // Free the decoded image / stop playback when the lightbox closes.
    dialog.addEventListener("close", function () {
      img.removeAttribute("src");
      stopVideo();
    });

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
