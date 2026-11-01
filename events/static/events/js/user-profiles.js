document.addEventListener("DOMContentLoaded", function () {
  var dialog = document.getElementById("user-profile-dialog");
  var dataElement = document.getElementById("event-user-profiles");
  if (!dialog || !dataElement) return;

  var profiles = JSON.parse(dataElement.textContent);
  var title = dialog.querySelector("[data-user-profile-title]");

  function fillList(name, items) {
    var list = dialog.querySelector('[data-user-profile-list="' + name + '"]');
    list.replaceChildren();
    if (!items || items.length === 0) {
      var empty = document.createElement("li");
      empty.className = "muted";
      empty.textContent = "Nichts eingetragen.";
      list.appendChild(empty);
      return;
    }
    items.forEach(function (text) {
      var item = document.createElement("li");
      item.textContent = text;
      list.appendChild(item);
    });
  }

  // Delegiert (Capture-Phase, da Popups stopPropagation nutzen), damit auch
  // nachgeladene Links wie im Erklärbären-Popup funktionieren.
  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest("[data-user-profile-id]");
    if (!link) return;
    event.preventDefault();
    var profile = profiles[link.dataset.userProfileId] || {};
    title.textContent = link.dataset.userProfileName;
    fillList("potluck", profile.potluck);
    fillList("games", profile.games);
    fillList("duties", profile.duties);
    dialog.showModal();
  }, true);

  dialog.querySelector("[data-user-profile-close]").addEventListener("click", function () {
    dialog.close();
  });

  dialog.addEventListener("click", function (event) {
    if (event.target === dialog) dialog.close();
  });
});
