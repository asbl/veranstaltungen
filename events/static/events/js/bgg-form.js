// BGG-Anbindung im Spiel-Formular: Während der Titel getippt wird, werden
// passende BGG-Treffer als Vorschläge angeboten. Ein Klick übernimmt die ID
// und befüllt die Detailfelder (Spielerzahl, Wertung, Weight, Kategorien)
// direkt aus der BGG-API – vor dem Speichern.
document.addEventListener("DOMContentLoaded", function () {
  var form = document.querySelector("form[data-bgg-search-url]");
  if (!form) return;
  var nameInput = form.querySelector("#id_name");
  var suggestions = document.getElementById("bgg-suggestions");
  var bggIdInput = form.querySelector("#id_bgg_id");
  var status = document.getElementById("bgg-lookup-status");
  var searchUrl = form.getAttribute("data-bgg-search-url");
  var lookupUrl = form.getAttribute("data-bgg-lookup-url");
  var debounceTimer = null;
  var searchSequence = 0;

  if (!nameInput || !suggestions || !bggIdInput) return;

  function setStatus(text) {
    if (status) status.textContent = text || "";
  }

  function setFieldValue(id, value) {
    var input = form.querySelector("#" + id);
    if (input) input.value = value === null || value === undefined ? "" : value;
  }

  function fillFields(data) {
    setFieldValue("id_min_players", data.min_players);
    setFieldValue("id_max_players", data.max_players);
    setFieldValue("id_bgg_rating", data.rating);
    setFieldValue("id_complexity", data.complexity);
    setFieldValue("id_bgg_categories_text", (data.categories || []).join(", "));
    var details = form.querySelector("details.form-more-options");
    if (details && (data.min_players || data.max_players || data.rating || data.complexity)) {
      details.open = true;
    }
  }

  function lookupBggId(id) {
    setStatus("BGG-Daten werden geladen…");
    fetch(lookupUrl + "?id=" + encodeURIComponent(id), { headers: { Accept: "application/json" } })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        if (data.error) {
          setStatus(data.error);
          return;
        }
        fillFields(data);
        setStatus("BGG-Daten von „" + data.name + "“ übernommen.");
      })
      .catch(function () {
        setStatus("BGG-Daten konnten nicht geladen werden; die Felder können manuell gepflegt werden.");
      });
  }

  function hideSuggestions() {
    suggestions.hidden = true;
    suggestions.textContent = "";
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
        clearTimeout(debounceTimer);
        ++searchSequence;
        bggIdInput.value = result.id;
        // Wurde ein BGG-Link eingefügt, den Titel übernehmen.
        if (/^(?:https?:\/\/)?(?:www\.)?boardgamegeek\.com\//i.test(nameInput.value.trim())) {
          nameInput.value = result.name;
        }
        hideSuggestions();
        lookupBggId(result.id);
      });
      suggestions.appendChild(button);
    });
    suggestions.hidden = false;
  }

  nameInput.addEventListener("input", function () {
    clearTimeout(debounceTimer);
    var sequence = ++searchSequence;
    hideSuggestions();
    setStatus("");
    var query = nameInput.value.trim();
    if (query.length < 3) {
      hideSuggestions();
      return;
    }
    setStatus("BGG-Vorschläge werden gesucht…");
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
            setStatus(data.error);
            return;
          }
          showSuggestions(data.results || []);
          setStatus((data.results || []).length ? "Passenden BGG-Eintrag auswählen:" : "Keine passenden BGG-Einträge gefunden.");
        })
        .catch(function () {
          if (sequence !== searchSequence) return;
          hideSuggestions();
          setStatus("BGG-Suche ist gerade nicht erreichbar. Die Felder können manuell gepflegt werden.");
        });
    }, 400);
  });

  bggIdInput.addEventListener("change", function () {
    var value = bggIdInput.value.trim();
    if (value) lookupBggId(value);
    else setStatus("");
  });

  document.addEventListener("click", function (event) {
    if (!suggestions.hidden && !suggestions.contains(event.target) && event.target !== nameInput) {
      hideSuggestions();
    }
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !suggestions.hidden) hideSuggestions();
  });
});
