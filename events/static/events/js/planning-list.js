document.querySelectorAll(".planning-list").forEach(function (section) {
  var rows = Array.from(section.querySelectorAll("tr[data-planning-status]"));
  var controls = section.querySelector(".planning-controls");
  if (!controls || !rows.length) return;
  var status = controls.querySelector(".planning-status-filter");
  var person = controls.querySelector(".planning-person-filter");
  var unassigned = controls.querySelector(".planning-unassigned-filter");
  var progress = controls.querySelector(".planning-progress");
  var apply = function () {
    var visible = 0, done = 0, assigned = 0;
    rows.forEach(function (row) {
      var completed = row.dataset.planningStatus === "done";
      if (completed) done += 1;
      var statusMatch = !status || status.value === "all" || (status.value === "unfinished" ? !completed : row.dataset.planningStatus === status.value);
      var people = (row.dataset.people || "").toLocaleLowerCase("de");
      if (people.trim()) assigned += 1;
      row.hidden = !statusMatch || !people.includes(person.value.trim().toLocaleLowerCase("de")) || (unassigned.checked && people.trim() !== "");
      if (!row.hidden) visible += 1;
    });
    progress.textContent = (status ? done + " von " + rows.length + " erledigt" : assigned + " von " + rows.length + " mit Verantwortung") + " · " + visible + " Einträge angezeigt";
    section.querySelector(".planning-empty-filter").hidden = visible !== 0;
  };
  var reset = function () { if (status) status.value = "all"; person.value = ""; unassigned.checked = false; apply(); };
  controls.hidden = false;
  if (status) status.addEventListener("change", apply);
  person.addEventListener("input", apply);
  unassigned.addEventListener("change", apply);
  controls.querySelector(".planning-reset").addEventListener("click", reset);
  section.addEventListener("planning-updated", apply);
  var revealHash = function () {
    var row = rows.find(function (entry) { return "#" + entry.id === window.location.hash; });
    if (row && row.hidden) { reset(); row.scrollIntoView({block: "nearest"}); }
  };
  window.addEventListener("hashchange", revealHash);
  apply();
  revealHash();
});
