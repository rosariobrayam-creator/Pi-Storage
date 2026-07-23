/* Lightbox, selection mode, favorites/albums, broken-thumbnail flagging.
   No framework; fetch is used only for favorite/album toggles. */
(function () {
  "use strict";

  var grid = document.getElementById("grid");
  if (!grid) return;

  var tiles = Array.prototype.slice.call(grid.querySelectorAll(".tile-btn"));

  // POST form-encoded, expect JSON. A 401 means the session died; send them
  // to sign in rather than failing silently.
  function post(url, data) {
    return fetch(url, { method: "POST", body: new URLSearchParams(data) })
      .then(function (r) {
        if (r.status === 401) { window.location = "/login"; throw new Error("401"); }
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      });
  }

  function setTileFav(t, on) {
    t.dataset.fav = on ? "1" : "0";
    t.closest(".tile").classList.toggle("is-fav", on);
  }

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
    var favBtn = document.getElementById("lb-fav");
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
      favBtn.classList.toggle("is-on", t.dataset.fav === "1");
      preload(i + 1);
      preload(i - 1);
    };

    favBtn.addEventListener("click", function () {
      var t = tiles[index];
      if (!t) return;
      post("/photo/" + t.dataset.id + "/favorite", {}).then(function (res) {
        setTileFav(t, res.favorite);
        favBtn.classList.toggle("is-on", res.favorite);
      }).catch(function () {});
    });

    // A Live Photo replays its paired clip in place of the still, then
    // falls back to the still when it ends.
    var backToStill = function () {
      stopVideo();
      video.hidden = true;
      img.hidden = false;
    };

    liveBtn.addEventListener("click", function () {
      var t = tiles[index];
      if (!t || !t.dataset.live) return;
      video.src = "/media/" + t.dataset.live;
      video.hidden = false;
      img.hidden = true;
      var p = video.play();
      if (p && p.catch) p.catch(function () {});  // the error handler reports it
    });

    video.addEventListener("ended", function () {
      var t = tiles[index];
      if (t && t.dataset.type !== "video") backToStill();  // Live replay over
    });

    // A clip that can't decode here (iPhone HEVC in a browser without HEVC
    // support, typically) would otherwise just sit black and silent -- put the
    // still back and say why instead.
    video.addEventListener("error", function () {
      if (!video.getAttribute("src")) return;  // fired by stopVideo() detaching
      var t = tiles[index];
      if (t && t.dataset.type !== "video") {
        backToStill();
        subEl.textContent =
          "Live clip can't play in this browser (HEVC) — try Safari, or the phone.";
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

    /* ---- favorites & albums on the selection bar ---- */

    var favBulk = document.getElementById("sel-fav");
    var albumSel = document.getElementById("sel-album");
    var albumName = document.getElementById("sel-album-name");
    var albumGo = document.getElementById("sel-album-go");
    var unalbum = document.getElementById("sel-unalbum");

    favBulk.addEventListener("click", function () {
      if (!selected.length) return;
      // If everything picked is already a favorite, unfavorite; else favorite.
      var allFav = selected.every(function (id) {
        var t = tiles.find(function (x) { return x.dataset.id === id; });
        return t && t.dataset.fav === "1";
      });
      post("/photos/favorite", { ids: selected.join(","), on: allFav ? "0" : "1" })
        .then(function (res) {
          selected.forEach(function (id) {
            var t = tiles.find(function (x) { return x.dataset.id === id; });
            if (t) setTileFav(t, res.favorite);
          });
        }).catch(function () {});
    });

    albumSel.addEventListener("change", function () {
      var isNew = albumSel.value === "__new__";
      albumName.hidden = !isNew;
      albumGo.hidden = !albumSel.value;
      if (isNew) albumName.focus();
    });

    albumGo.addEventListener("click", function () {
      if (!selected.length || !albumSel.value) return;
      var data = { ids: selected.join(",") };
      if (albumSel.value === "__new__") {
        if (!albumName.value.trim()) { albumName.focus(); return; }
        data.new_name = albumName.value.trim();
      } else {
        data.album_id = albumSel.value;
      }
      albumGo.disabled = true;
      post("/albums/add", data).then(function () {
        window.location.reload();  // pills + counts come from the server
      }).catch(function () { albumGo.disabled = false; });
    });

    if (unalbum) {
      unalbum.addEventListener("click", function () {
        if (!selected.length) return;
        post("/albums/" + selbar.dataset.album + "/remove",
             { ids: selected.join(",") })
          .then(function () { window.location.reload(); })
          .catch(function () {});
      });
    }
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

  /* -------------------------------------------------- a little easter egg */
  // Wrench would approve of the self-hosting. Admin only: everyone else's
  // gallery stays perfectly serious. Type the word, or tap the corner tag.

  (function () {
    if (document.body.dataset.admin !== "1") return;

    console.log("%c" + [
      "      _.-\"\"-._",
      "     /  _  _  \\",
      "    |  (o)(o)  |     WE ARE DEDSEC.",
      "    |   /__\\   |     nice server. no cloud, no data brokers.",
      "     \\  \\/\\/  /      we do not judge the 6,000 screenshots.",
      "      '-.__.-'       (you know the word. type it in the gallery.)",
    ].join("\n"), "color:#27E67A; font-family:monospace; font-size:12px;");

    function breach() {
      if (document.querySelector(".dedsec")) return;
      var el = document.createElement("div");
      el.className = "dedsec";
      el.innerHTML =
        '<div class="dedsec-inner">' +
        '<p class="dedsec-logo" data-text="ded_sec">ded_sec</p>' +
        '<p class="dedsec-line">&gt; INTRUSION DETECTED&hellip; just kidding. it\'s us.</p>' +
        '<p class="dedsec-line">&gt; We are DedSec.</p>' +
        '<p class="dedsec-line">&gt; Self-hosted photos. No cloud. No data brokers. Respect.</p>' +
        '<p class="dedsec-line">&gt; Your data belongs to you. Keep it that way, citizen.</p>' +
        '<p class="dedsec-hint">[ click anywhere / esc ]</p>' +
        "</div>" +
        '<img class="dedsec-badge" src="/static/dedsec.webp" alt="">';
      var close = function () {
        el.remove();
        document.removeEventListener("keydown", esc);
      };
      var esc = function (e) { if (e.key === "Escape") close(); };
      el.addEventListener("click", close);
      document.addEventListener("keydown", esc);
      document.body.appendChild(el);
      setTimeout(close, 12000);
    }

    var buf = "";
    document.addEventListener("keydown", function (e) {
      var t = e.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA"
                || t.tagName === "SELECT")) return;
      if (!e.key || e.key.length !== 1) return;
      buf = (buf + e.key.toLowerCase()).slice(-6);
      if (buf === "dedsec") { buf = ""; breach(); }
    });

    // The spray tag in the corner -- and the way in from a phone, where
    // there's no keyboard to type the word on.
    var tag = document.createElement("button");
    tag.type = "button";
    tag.className = "dedsec-corner";
    tag.setAttribute("aria-label", "ded_sec");
    tag.innerHTML = '<img src="/static/dedsec.webp" alt="">';
    tag.addEventListener("click", breach);
    document.body.appendChild(tag);
  })();

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
