// Mehrere Spiele auf einmal eintragen: Zeilen kommen automatisch hinzu,
// eingefügte Listen werden auf Zeilen verteilt und jede Zeile bekommt eigene
// BGG-Vorschläge. Ohne JavaScript bleiben die festen Zeilen mit sichtbarem
// BGG-ID-Feld nutzbar.
document.addEventListener("DOMContentLoaded", function () {
  var form = document.querySelector("form[data-bulk-search-url]");
  if (!form) return;
  var searchUrl = form.getAttribute("data-bulk-search-url");
  var rowsContainer = form.querySelector(".game-bulk-rows");
  var addRowButton = form.querySelector(".game-bulk-add-row");
  var submitLabel = form.querySelector(".game-bulk-submit-label");
  var typeSelect = form.querySelector("select[name='type']");
  var existingElement = document.getElementById("game-bulk-existing");
  var existing = {};
  JSON.parse(existingElement ? existingElement.textContent : "[]").forEach(function (name) {
    existing[name.toLowerCase()] = name;
  });
  var template = rowsContainer.querySelector(".game-bulk-row").cloneNode(true);
  var explainAllLabel = form.querySelector(".game-bulk-explain-all");
  var explainAll = explainAllLabel.querySelector("input");
  var rowCounter = 0;
  var BGG_LINK_PATTERN = /^(?:https?:\/\/)?(?:www\.)?boardgamegeek\.com\//i;

  function rows() {
    return Array.prototype.slice.call(rowsContainer.querySelectorAll(".game-bulk-row"));
  }

  function filledRows() {
    return rows().filter(function (row) {
      return row.querySelector("input[name='name']").value.trim();
    });
  }

  function updateSubmitLabel() {
    var count = filledRows().length;
    submitLabel.textContent = count === 1 ? "1 Spiel hinzufügen" : count + " Spiele hinzufügen";
  }

  function renumber() {
    rows().forEach(function (row, index) {
      row.querySelector(".game-bulk-label").textContent = "Spiel " + (index + 1);
      row.querySelector(".game-bulk-remove").hidden = rows().length <= 1;
      // Die Kästchen übertragen nur ihre Zeilennummer, daher nach jeder
      // Änderung der Reihenfolge neu durchzählen.
      row.querySelector("input[name='explain']").value = index;
    });
  }

  function addRow(name) {
    var row = template.cloneNode(true);
    rowCounter += 1;
    var input = row.querySelector("input[name='name']");
    input.id = "bulk-name-new-" + rowCounter;
    row.querySelector(".game-bulk-label").setAttribute("for", input.id);
    input.value = name || "";
    row.querySelector("input[name='bgg_id']").value = "";
    row.querySelector("input[name='explain']").checked = explainAll.checked;
    row.querySelector(".bgg-lookup-status").textContent = "";
    rowsContainer.appendChild(row);
    setupRow(row);
    renumber();
    return row;
  }

  function ensureTrailingEmptyRow() {
    var all = rows();
    var last = all[all.length - 1];
    if (last.querySelector("input[name='name']").value.trim()) addRow("");
  }

  function setupRow(row) {
    var nameInput = row.querySelector("input[name='name']");
    var bggIdInput = row.querySelector("input[name='bgg_id']");
    var suggestions = row.querySelector(".bgg-suggestions");
    var status = row.querySelector(".bgg-lookup-status");
    var debounceTimer = null;
    var searchSequence = 0;
    var linkedName = "";

    // Die BGG-ID wird über die Vorschläge gesetzt; das Zahlenfeld ist nur
    // der Rückfall ohne JavaScript.
    bggIdInput.type = "hidden";
    row.querySelector(".game-bulk-bgg-id").hidden = true;

    function hideSuggestions() {
      suggestions.hidden = true;
      suggestions.textContent = "";
    }

    function duplicateHint() {
      var match = existing[nameInput.value.trim().toLowerCase()];
      if (!match) return "";
      return typeSelect && typeSelect.value === "wish"
        ? "„" + match + "“ steht schon auf der Liste und wird nicht doppelt angelegt."
        : "„" + match + "“ steht schon auf der Liste – du wirst dort als Mitbringer eingetragen.";
    }

    function renderStatus(text) {
      status.textContent = "";
      var hint = duplicateHint();
      if (hint) {
        status.textContent = hint;
        return;
      }
      if (bggIdInput.value) {
        status.appendChild(document.createTextNode("✓ Mit BGG verknüpft: " + linkedName + " (#" + bggIdInput.value + ") "));
        var unlink = document.createElement("button");
        unlink.type = "button";
        unlink.className = "btn link game-bulk-unlink";
        unlink.textContent = "Verknüpfung lösen";
        unlink.addEventListener("click", function () {
          bggIdInput.value = "";
          linkedName = "";
          renderStatus("");
          nameInput.focus();
        });
        status.appendChild(unlink);
        return;
      }
      status.textContent = text || "";
    }
    row.renderStatus = renderStatus;

    if (bggIdInput.value) {
      linkedName = "BGG-Eintrag";
      renderStatus("");
    }

    function choose(result) {
      clearTimeout(debounceTimer);
      ++searchSequence;
      bggIdInput.value = result.id;
      // Wurde ein BGG-Link eingefügt, den Titel übernehmen.
      if (BGG_LINK_PATTERN.test(nameInput.value.trim())) nameInput.value = result.name;
      linkedName = result.name + (result.year ? ", " + result.year : "");
      hideSuggestions();
      renderStatus("");
    }

    function showSuggestions(results) {
      suggestions.textContent = "";
      if (!results.length) {
        hideSuggestions();
        return;
      }
      results.forEach(function (result) {
        var button = document.createElement("button");
        button.type = "button";
        button.className = "bgg-suggestion";
        var label = document.createElement("span");
        label.textContent = result.name;
        button.appendChild(label);
        if (result.year) {
          var year = document.createElement("span");
          year.className = "bgg-suggestion-year";
          year.textContent = result.year;
          button.appendChild(year);
        }
        var id = document.createElement("span");
        id.className = "bgg-suggestion-id";
        id.textContent = "BGG #" + result.id;
        button.appendChild(id);
        button.addEventListener("click", function () {
          choose(result);
          var next = row.nextElementSibling;
          if (next) next.querySelector("input[name='name']").focus();
        });
        suggestions.appendChild(button);
      });
      suggestions.hidden = false;
    }

    var searchedQuery = "";

    function search(delay) {
      clearTimeout(debounceTimer);
      var sequence = ++searchSequence;
      var query = nameInput.value.trim();
      searchedQuery = query;
      if (query.length < 3 || duplicateHint()) return;
      renderStatus("BGG-Vorschläge werden gesucht…");
      debounceTimer = setTimeout(function () {
        fetch(searchUrl + "?q=" + encodeURIComponent(query), { headers: { Accept: "application/json" } })
          .then(function (response) {
            if (!response.ok) throw new Error("HTTP " + response.status);
            return response.json();
          })
          .then(function (data) {
            if (sequence !== searchSequence) return;
            if (data.error) {
              hideSuggestions();
              renderStatus(data.error);
              return;
            }
            var results = data.results || [];
            if (document.activeElement === nameInput || suggestions.contains(document.activeElement)) {
              showSuggestions(results);
            }
            renderStatus(results.length ? "Passenden BGG-Eintrag auswählen (oder ohne BGG weiter)." : "Kein BGG-Eintrag gefunden – das Spiel wird ohne BGG-Daten eingetragen.");
          })
          .catch(function () {
            if (sequence !== searchSequence) return;
            hideSuggestions();
            renderStatus("BGG-Suche ist gerade nicht erreichbar. Das Spiel wird ohne BGG-Daten eingetragen.");
          });
      }, delay);
    }

    nameInput.addEventListener("input", function () {
      bggIdInput.value = "";
      linkedName = "";
      hideSuggestions();
      renderStatus("");
      ensureTrailingEmptyRow();
      updateSubmitLabel();
      search(400);
    });

    // Eingefügte Titel wurden noch nicht gesucht: beim Betreten der Zeile
    // gleich Vorschläge holen, ohne dass neu getippt werden muss.
    nameInput.addEventListener("focus", function () {
      if (!bggIdInput.value && nameInput.value.trim() !== searchedQuery) search(0);
    });

    // Eine eingefügte mehrzeilige Liste wird auf diese und die folgenden
    // Zeilen verteilt. Aufzählungszeichen und Nummerierungen fallen weg.
    nameInput.addEventListener("paste", function (event) {
      var text = (event.clipboardData || window.clipboardData).getData("text");
      if (!text || text.indexOf("\n") === -1) return;
      var names = text.split(/\r?\n/).map(function (line) {
        return line.replace(/^\s*(?:[-*•–]|\d+[.)])\s*/, "").trim();
      }).filter(Boolean);
      if (!names.length) return;
      event.preventDefault();
      var current = row;
      names.forEach(function (name, index) {
        var target = current;
        if (index > 0) {
          var next = current.nextElementSibling;
          target = next && !next.querySelector("input[name='name']").value.trim() ? next : addRow("");
          if (target !== current.nextElementSibling) rowsContainer.insertBefore(target, current.nextElementSibling);
        }
        var input = target.querySelector("input[name='name']");
        input.value = name;
        target.querySelector("input[name='bgg_id']").value = "";
        target.renderStatus("");
        current = target;
      });
      renumber();
      ensureTrailingEmptyRow();
      updateSubmitLabel();
      var pending = rows().filter(function (r) {
        return r.querySelector("input[name='name']").value.trim() && !r.querySelector("input[name='bgg_id']").value;
      }).length;
      if (pending) {
        row.renderStatus(names.length + " Spiele übernommen. Für BGG-Vorschläge in eine Zeile klicken – oder direkt speichern.");
      }
    });

    nameInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        // Enter springt in die nächste Zeile statt das Formular abzuschicken.
        event.preventDefault();
        var first = suggestions.hidden ? null : suggestions.querySelector(".bgg-suggestion");
        if (first) {
          first.click();
          return;
        }
        ensureTrailingEmptyRow();
        var next = row.nextElementSibling;
        if (next) next.querySelector("input[name='name']").focus();
      } else if (event.key === "ArrowDown" && !suggestions.hidden) {
        event.preventDefault();
        var firstSuggestion = suggestions.querySelector(".bgg-suggestion");
        if (firstSuggestion) firstSuggestion.focus();
      } else if (event.key === "Escape") {
        hideSuggestions();
      }
    });

    suggestions.addEventListener("keydown", function (event) {
      var current = document.activeElement;
      if (event.key === "ArrowDown" && current.nextElementSibling) {
        event.preventDefault();
        current.nextElementSibling.focus();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        (current.previousElementSibling || nameInput).focus();
      } else if (event.key === "Escape") {
        hideSuggestions();
        nameInput.focus();
      }
    });

    row.querySelector(".game-bulk-remove").addEventListener("click", function () {
      var next = row.nextElementSibling || row.previousElementSibling;
      row.remove();
      renumber();
      ensureTrailingEmptyRow();
      updateSubmitLabel();
      if (next) next.querySelector("input[name='name']").focus();
    });

    document.addEventListener("click", function (event) {
      if (!suggestions.hidden && !suggestions.contains(event.target) && event.target !== nameInput) {
        hideSuggestions();
      }
    });
  }

  rows().forEach(setupRow);
  renumber();
  explainAllLabel.hidden = false;
  explainAll.checked = rows().every(function (row) {
    return row.querySelector("input[name='explain']").checked;
  });
  explainAll.addEventListener("change", function () {
    rows().forEach(function (row) {
      row.querySelector("input[name='explain']").checked = explainAll.checked;
    });
  });
  rowsContainer.addEventListener("change", function (event) {
    if (event.target.name === "explain" && !event.target.checked) explainAll.checked = false;
  });
  addRowButton.hidden = false;
  addRowButton.addEventListener("click", function () {
    addRow("").querySelector("input[name='name']").focus();
  });
  if (typeSelect) {
    typeSelect.addEventListener("change", function () {
      rows().forEach(function (row) {
        row.renderStatus("");
      });
    });
  }
  updateSubmitLabel();
  var firstInput = rows()[0].querySelector("input[name='name']");
  if (!filledRows().length) firstInput.focus();
});
