document.addEventListener("DOMContentLoaded", function () {
  var openItemFromHash = function () {
    if (!window.location.hash) return;
    var target = document.getElementById(window.location.hash.slice(1));
    if (!target || !target.matches('tr[id^="potluck-"], [id^="game-"]')) return;
    var wrapper = target.closest(".game-list-wrapper");
    if (target.hidden && wrapper && wrapper.resetGameFilters) {
      wrapper.resetGameFilters();
      var count = wrapper.querySelector(".game-filter-count");
      if (count) count.textContent += " Filter für den Spielelink zurückgesetzt.";
    }
    var details = target.querySelector(".potluck-item-details, .game-item-details");
    if (details) details.open = true;
    target.scrollIntoView({ block: "nearest" });
  };

  window.addEventListener("hashchange", openItemFromHash);
  document.addEventListener("click", function (event) {
    var link = event.target.closest("a[href^='#game-']");
    if (link && link.hash === window.location.hash) openItemFromHash();
  });

  var compareItems = function (a, b, key, ascending) {
    var av = (a.dataset[key] || "").trim();
    var bv = (b.dataset[key] || "").trim();
    var numeric = ["interest", "rating", "complexity", "players"].indexOf(key) !== -1;
    var an = Number(av), bn = Number(bv);
    var aMissing = av === "" || (numeric && !Number.isFinite(an));
    var bMissing = bv === "" || (numeric && !Number.isFinite(bn));
    if (aMissing !== bMissing) return aMissing ? 1 : -1;
    var cmp = aMissing ? 0 : numeric ? an - bn : av.localeCompare(bv, "de", {numeric: true, sensitivity: "base"});
    return (ascending ? cmp : -cmp) || (a.dataset.name || "").localeCompare(b.dataset.name || "", "de", {numeric: true});
  };

  // Diskrete BGG-Popups: Icon in der Spieleliste öffnet die Details, ohne
  // den Listeneintrag selbst auf- oder zuzuklappen.
  var closeAllGamePopups = function (except) {
    document.querySelectorAll(".game-bgg-popup, .game-explain-popup").forEach(function (popup) {
      if (popup === except) return;
      popup.hidden = true;
      var toggle = popup.parentElement.querySelector(".game-bgg-toggle, .game-explain-toggle");
      if (toggle) toggle.setAttribute("aria-expanded", "false");
    });
  };

  var positionPopup = function (toggle, popup) {
    popup.style.transform = "";
    var bounds = popup.getBoundingClientRect();
    var viewportWidth = document.documentElement.clientWidth;
    var shift = Math.max(8 - bounds.left, Math.min(0, viewportWidth - 8 - bounds.right));
    var summary = toggle.closest("summary");
    var shiftDown = summary ? Math.max(0, summary.getBoundingClientRect().bottom + 6 - bounds.top) : 0;
    popup.style.transform = "translate(" + shift + "px, " + shiftDown + "px)";
  };

  var openPopup = function (toggle, popup) {
    closeAllGamePopups(popup);
    popup.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    positionPopup(toggle, popup);
  };

  var csrfToken = function () {
    var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
    if (input) return input.value;
    var match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  };

  var explainAnnouncer = null;
  var announce = function (text) {
    if (!explainAnnouncer) {
      explainAnnouncer = document.createElement("p");
      explainAnnouncer.className = "game-explain-announcer";
      explainAnnouncer.setAttribute("role", "status");
      document.body.appendChild(explainAnnouncer);
    }
    explainAnnouncer.textContent = text;
  };

  // Klick auf 🐻 trägt die angemeldete Person direkt als Erklärbär ein bzw.
  // aus. Gespeichert wird im Hintergrund; schnelle Mehrfachklicks werden zum
  // zuletzt gewünschten Zustand zusammengefasst. Bewusst ohne Popup, damit
  // nichts das 🐻 des nächsten Spiels verdeckt - die Namen stehen im Tooltip.
  var setupExplainToggle = function (toggle, popup) {
    var item = toggle.closest(".games-list-item");
    var gameName = (item.querySelector(".game-summary-name-row strong") || {}).textContent || "";
    var saved = toggle.getAttribute("aria-pressed") === "true";
    var desired = saved;
    var inFlight = false;

    var setStatus = function (text, isError) {
      var status = popup.querySelector(".game-explain-status");
      if (!status) {
        status = document.createElement("span");
        status.className = "game-explain-status";
        status.setAttribute("role", "status");
        popup.appendChild(status);
      }
      status.textContent = text;
      status.classList.toggle("is-error", !!isError);
    };

    var showState = function (explaining) {
      toggle.classList.toggle("is-mine", explaining);
      toggle.setAttribute("aria-pressed", String(explaining));
      var names = Array.prototype.map.call(
        popup.querySelectorAll(".game-explain-person .user-link, .game-explain-person .user-label"),
        function (link) { return link.textContent.trim(); }
      );
      toggle.title = (explaining ? "Klicken, um dich als Erklärbär auszutragen" : "Klicken, um dich als Erklärbär einzutragen")
        + (names.length ? "\nErklärbären: " + names.join(", ") : "");
    };

    var send = async function () {
      inFlight = true;
      toggle.classList.add("is-saving");
      var target = desired;
      var body = new FormData();
      body.append("explain", target ? "1" : "0");
      try {
        var response = await fetch(toggle.dataset.explainUrl, {
          method: "POST", body: body, credentials: "same-origin",
          headers: {"Accept": "application/json", "X-CSRFToken": csrfToken()}
        });
        var data = (response.headers.get("content-type") || "").indexOf("application/json") !== -1 ? await response.json() : {};
        if (!response.ok || typeof data.explaining !== "boolean") {
          throw new Error(data.error || "Speichern fehlgeschlagen. Bitte lade die Seite neu und versuche es erneut.");
        }
        saved = data.explaining;
        toggle.querySelector(".game-explain-count").textContent = data.count;
        toggle.classList.toggle("is-missing", data.count === 0);
        toggle.setAttribute("aria-label", data.count + " Erklärbären für " + gameName);
        popup.innerHTML = data.popup_html;
        var section = item.querySelector(".game-explain-section");
        if (section) section.innerHTML = data.section_html;
        item.dataset.missingExplainer = String(data.count === 0);
        item.dataset.mine = String(data.mine);
        if (desired === saved) {
          showState(saved);
          announce(saved ? "Gespeichert: Du erklärst " + gameName + "." : "Gespeichert: Du erklärst " + gameName + " nicht mehr.");
        }
      } catch (error) {
        desired = saved;
        showState(saved);
        if (popup.hidden) openPopup(toggle, popup);
        setStatus(error.message || "Speichern fehlgeschlagen.", true);
        announce(error.message || "Speichern fehlgeschlagen.");
      } finally {
        inFlight = false;
        toggle.classList.remove("is-saving");
      }
      if (desired !== saved) send();
    };

    showState(saved);
    toggle.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      desired = !desired;
      closeAllGamePopups(null);
      showState(desired);
      if (!inFlight) send();
    });
  };

  document.querySelectorAll(".game-bgg-toggle, .game-explain-toggle").forEach(function (toggle) {
    var popup = toggle.parentElement.querySelector(".game-bgg-popup, .game-explain-popup");
    if (!popup) return;
    // Klicks im Popup dürfen den Listeneintrag (summary) nicht auf-/zuklappen.
    popup.addEventListener("click", function (event) {
      event.stopPropagation();
    });
    if (toggle.dataset.explainUrl && window.fetch) {
      setupExplainToggle(toggle, popup);
      return;
    }
    toggle.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      if (popup.hidden) {
        openPopup(toggle, popup);
      } else {
        closeAllGamePopups(null);
      }
    });
  });

  document.addEventListener("click", function (event) {
    if (event.target.closest && event.target.closest(".game-bgg-badge, .game-explain-badge")) return;
    closeAllGamePopups(null);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeAllGamePopups(null);
  });

  document.querySelectorAll("table.game-table").forEach(function (table) {
    var tbody = table.tBodies[0];
    var headerCells = table.tHead.rows[0].cells;
    var wrapper = table.closest(".game-list-wrapper");
    var section = table.closest("section.feature");
    var primaryRows = function () {
      return Array.prototype.slice.call(tbody.querySelectorAll("tr[data-name]"));
    };
    var commentsRowFor = function (row) {
      var toggle = row.querySelector(".game-comments-toggle");
      return toggle ? document.getElementById(toggle.getAttribute("aria-controls")) : null;
    };

    table.querySelectorAll(".game-comments-toggle").forEach(function (toggle) {
      var commentsRow = document.getElementById(toggle.getAttribute("aria-controls"));
      if (!commentsRow) return;
      toggle.addEventListener("click", function () {
        var willOpen = commentsRow.hidden;
        commentsRow.hidden = !willOpen;
        toggle.setAttribute("aria-expanded", String(willOpen));
      });
    });

    var filterInput = wrapper ? wrapper.querySelector(".game-filter") : null;
    if (filterInput) {
      filterInput.addEventListener("input", function () {
        var query = filterInput.value.trim().toLowerCase();
        primaryRows().forEach(function (row) {
          var haystack = Object.keys(row.dataset)
            .map(function (key) { return row.dataset[key]; })
            .join(" ");
          var filteredOut = haystack.indexOf(query) === -1;
          row.style.display = filteredOut ? "none" : "";
          var commentsRow = commentsRowFor(row);
          if (commentsRow) commentsRow.style.display = filteredOut ? "none" : "";
        });
      });
    }

    var activeSort = null;
    var sortRows = function (key, ascending) {
      activeSort = { key: key, ascending: ascending };
      var rows = primaryRows();
      rows.sort(function (a, b) {
        return compareItems(a, b, key, ascending);
      });
      rows.forEach(function (row) {
        tbody.appendChild(row);
        var commentsRow = commentsRowFor(row);
        if (commentsRow) tbody.appendChild(commentsRow);
      });
      Array.prototype.forEach.call(headerCells, function (cell) {
        cell.classList.remove("sort-asc", "sort-desc");
        if (cell.dataset.sort) cell.setAttribute("aria-sort", "none");
      });
      var activeHeader = table.querySelector('th[data-sort="' + key + '"]');
      if (activeHeader) {
        activeHeader.classList.add(ascending ? "sort-asc" : "sort-desc");
        activeHeader.setAttribute("aria-sort", ascending ? "ascending" : "descending");
      }
    };

    Array.prototype.forEach.call(headerCells, function (th) {
      var key = th.dataset.sort;
      if (!key) return;
      th.classList.add("sortable");
      th.tabIndex = 0;
      th.setAttribute("role", "button");
      th.setAttribute("aria-sort", "none");
      var activateSort = function () {
        var ascending = !(activeSort && activeSort.key === key && activeSort.ascending);
        sortRows(key, ascending);
      };
      th.addEventListener("click", activateSort);
      th.addEventListener("keydown", function (event) {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        activateSort();
      });
    });

    var sortSelect = wrapper ? wrapper.querySelector(".game-sort-select") : null;
    if (sortSelect) {
      sortSelect.addEventListener("change", function () {
        var parts = sortSelect.value.split(":");
        sortRows(parts[0], parts[1] !== "desc");
      });
    }

  });

  document.querySelectorAll(".games-list").forEach(function (list) {
    var wrapper = list.closest(".game-list-wrapper");
    var section = list.closest("section.feature");
    var items = function () {
      return Array.prototype.slice.call(list.querySelectorAll(":scope > .games-list-item"));
    };
    var sortItems = function (key, ascending) {
      var sorted = items().sort(function (a, b) {
        return compareItems(a, b, key, ascending);
      });
      sorted.forEach(function (item) { list.appendChild(item); });
    };
    var filterInput = wrapper.querySelector(".game-filter");
    var quickFilters = Array.from(wrapper.querySelectorAll("[data-game-filter]"));
    var count = wrapper.querySelector(".game-filter-count");
    var empty = wrapper.querySelector(".game-filter-empty");
    var applyFilters = function () {
      var query = filterInput.value.trim().toLocaleLowerCase("de");
      var visible = 0;
      items().forEach(function (item) {
        var haystack = ["name", "owner", "description", "categories", "expansions"]
          .map(function (key) { return item.dataset[key] || ""; }).join(" ").toLocaleLowerCase("de");
        item.hidden = haystack.indexOf(query) === -1 || quickFilters.some(function (filter) {
          return filter.checked && item.dataset[filter.dataset.gameFilter] !== "true";
        });
        if (!item.hidden) visible += 1;
      });
      if (count) { count.hidden = false; count.textContent = visible + " von " + items().length + " Spielen angezeigt"; }
      if (empty) empty.hidden = visible !== 0;
    };
    wrapper.resetGameFilters = function () {
      filterInput.value = "";
      quickFilters.forEach(function (filter) { filter.checked = false; });
      applyFilters();
    };
    filterInput.addEventListener("input", applyFilters);
    quickFilters.forEach(function (filter) { filter.addEventListener("change", applyFilters); });
    var quickContainer = wrapper.querySelector(".game-quick-filters");
    if (quickContainer) quickContainer.hidden = false;
    var reset = wrapper.querySelector(".game-filter-reset");
    if (reset) reset.addEventListener("click", wrapper.resetGameFilters);
    var sortSelect = wrapper.querySelector(".game-sort-select");
    sortSelect.addEventListener("change", function () {
      var parts = sortSelect.value.split(":");
      sortItems(parts[0], parts[1] !== "desc");
    });
    var initialSort = sortSelect.value.split(":");
    sortItems(initialSort[0], initialSort[1] !== "desc");
    applyFilters();
  });

  document.querySelectorAll(".game-list-wrapper").forEach(function (wrapper) {
    var button = wrapper.querySelector(".fullscreen-toggle-btn");
    var enter = wrapper.requestFullscreen || wrapper.webkitRequestFullscreen;
    var leave = document.exitFullscreen || document.webkitExitFullscreen;
    if (!button || !enter || !leave || document.fullscreenEnabled === false) return;
    button.hidden = false;
    var isFullscreen = function () {
      return document.fullscreenElement === wrapper || document.webkitFullscreenElement === wrapper;
    };
    var updateLabel = function () {
      button.querySelector("span").textContent = isFullscreen() ? "Vollbild verlassen" : "Vollbild";
      button.querySelector("i").className = isFullscreen() ? "fa-solid fa-compress" : "fa-solid fa-expand";
      button.setAttribute("aria-pressed", String(isFullscreen()));
    };
    button.addEventListener("click", async function () {
      var feedback = wrapper.querySelector(".game-fullscreen-feedback");
      if (feedback) feedback.textContent = "";
      try {
        await (isFullscreen() ? leave.call(document) : enter.call(wrapper));
      } catch (error) {
        if (feedback) feedback.textContent = "Vollbild ist derzeit nicht verfügbar. Die Liste bleibt normal bedienbar.";
      }
      updateLabel();
    });
    document.addEventListener("fullscreenchange", updateLabel);
    document.addEventListener("webkitfullscreenchange", updateLabel);
    updateLabel();
  });
  openItemFromHash();
});
