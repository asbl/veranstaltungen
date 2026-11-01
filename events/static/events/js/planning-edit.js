document.addEventListener("DOMContentLoaded", function () {
  var dialog = document.getElementById("planning-edit-dialog");
  if (!dialog || !dialog.showModal || !window.fetch) return;
  var content = dialog.querySelector("[data-planning-content]");
  var feedback = dialog.querySelector("[data-planning-feedback]");
  var closeButton = dialog.querySelector("[data-planning-close]");
  var sequence = 0;
  var saving = false;
  var opener;

  function close() { if (!saving) dialog.close(); }
  function install(html) {
    var parsed = new DOMParser().parseFromString(html, "text/html");
    if (!parsed.querySelector("[data-planning-field-form]")) return false;
    content.replaceChildren(...Array.from(parsed.body.childNodes));
    var target = content.querySelector(".form-error-summary, select, input:not([type=hidden])");
    if (target) target.focus();
    return true;
  }
  function login(url) {
    content.replaceChildren();
    var title = document.createElement("h2");
    title.id = "planning-field-title";
    title.textContent = "Bitte anmelden";
    var text = document.createElement("p");
    text.textContent = "Für diese Änderung ist eine persönliche Anmeldung nötig.";
    var link = document.createElement("a");
    link.className = "btn";
    link.href = url;
    link.textContent = "Anmelden und bearbeiten";
    content.append(title, text, link);
    feedback.textContent = "";
    link.focus();
  }
  document.addEventListener("click", async function (event) {
    var link = event.target.closest("[data-planning-edit]");
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault();
    opener = link;
    var current = ++sequence;
    content.innerHTML = '<h2 id="planning-field-title">Bearbeiten</h2>';
    feedback.textContent = "Wird geladen …";
    dialog.showModal();
    try {
      var response = await fetch(link.href, {credentials: "same-origin", headers: {"X-Planning-Popup": "1"}});
      if (current !== sequence || !dialog.open) return;
      if (response.redirected) { login(response.url); return; }
      if (!response.ok || !install(await response.text())) throw new Error("Das Formular konnte nicht geladen werden. Bitte prüfe deinen Zugang oder lade die Seite neu.");
      feedback.textContent = "";
    } catch (error) {
      if (current === sequence && dialog.open) feedback.textContent = error instanceof TypeError ? "Laden fehlgeschlagen. Bitte prüfe deine Verbindung und versuche es erneut." : (error.message || "Laden fehlgeschlagen. Bitte versuche es erneut.");
    }
  });
  dialog.addEventListener("submit", async function (event) {
    var form = event.target.closest("[data-planning-field-form]");
    if (!form) return;
    event.preventDefault();
    if (saving) return;
    var body = new FormData(form);
    var button = form.querySelector('[type="submit"]');
    saving = true;
    button.disabled = true;
    closeButton.disabled = true;
    feedback.textContent = "Wird gespeichert …";
    try {
      var response = await fetch(form.action, {method: "POST", body: body, credentials: "same-origin", headers: {"X-Planning-Popup": "1"}});
      if (response.headers.get("content-type")?.includes("application/json")) {
        var data = await response.json();
        if (response.ok && data.location) {
          var target = new URL(data.location, location.href);
          if (target.origin !== location.origin) throw new Error("Ungültige Rückleitung.");
          if (target.pathname === location.pathname && target.search === location.search) {
            location.hash = target.hash;
            location.reload();
          } else {
            location.assign(target.href);
          }
          return;
        }
      } else if ([400, 409].includes(response.status) && install(await response.text())) {
        feedback.textContent = "Bitte prüfe die markierten Angaben. Bei einem geänderten Eintrag schließe das Popup und öffne es erneut.";
        return;
      }
      throw new Error("Speichern nicht bestätigt. Deine Eingaben bleiben erhalten. Bitte prüfe deinen Zugang und versuche es erneut.");
    } catch (error) {
      feedback.textContent = error instanceof TypeError ? "Speichern fehlgeschlagen. Bitte prüfe deine Verbindung. Deine Eingaben bleiben erhalten." : (error.message || "Speichern fehlgeschlagen. Deine Eingaben bleiben erhalten.");
    } finally {
      saving = false;
      button.disabled = false;
      closeButton.disabled = false;
    }
  });
  closeButton.addEventListener("click", close);
  dialog.addEventListener("click", function (event) {
    if (event.target.closest("[data-planning-cancel]")) { event.preventDefault(); close(); }
    else if (event.target === dialog) close();
  });
  dialog.addEventListener("cancel", function (event) { if (saving) event.preventDefault(); });
  dialog.addEventListener("close", function () { sequence++; if (opener) opener.focus(); });
});
