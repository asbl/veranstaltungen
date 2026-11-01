// Keep normal POST forms as the fallback when fetch is unavailable.
document.querySelectorAll(".todo-completion-form").forEach(function (form) {
  var checkbox = form.querySelector('input[name="done"]');
  var button = form.querySelector('button[type="submit"]');
  if (!checkbox || !button || !window.fetch) return;
  var row = form.closest("tr");
  var saved = checkbox.checked;
  var busy = false;
  var feedback = form.querySelector(".todo-save-feedback");
  var save = async function (event) {
    if (event) event.preventDefault();
    if (busy) return;
    var body = new FormData(form);
    busy = true;
    checkbox.disabled = true;
    button.disabled = true;
    feedback.textContent = "Wird gespeichert …";
    try {
      var response = await fetch(form.action, {
        method: "POST", body: body, credentials: "same-origin",
        headers: {"Accept": "application/json"}
      });
      var data = response.headers.get("content-type")?.includes("application/json") ? await response.json() : {};
      if (!response.ok || !["open", "done"].includes(data.status) || !Number.isInteger(data.revision)) {
        throw new Error(data.error || "Speichern nicht bestätigt. Bitte lade die Seite neu und prüfe den aktuellen Stand.");
      }
      saved = data.status === "done";
      checkbox.checked = saved;
      row.classList.toggle("todo-completed", saved);
      row.dataset.planningStatus = data.status;
      row.querySelector(".todo-status-label").textContent = data.label;
      row.querySelectorAll('input[name="revision"]').forEach(function (input) { input.value = data.revision; });
      feedback.textContent = "Gespeichert";
      row.dispatchEvent(new CustomEvent("planning-updated", {bubbles: true}));
    } catch (error) {
      checkbox.checked = saved;
      feedback.textContent = error.message || "Speichern fehlgeschlagen. Bitte versuche es erneut.";
    } finally {
      busy = false;
      checkbox.disabled = false;
      button.disabled = false;
    }
  };
  checkbox.addEventListener("change", save);
  form.addEventListener("submit", save);
  button.hidden = true;
});
