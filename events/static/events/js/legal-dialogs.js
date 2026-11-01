document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("[data-dialog-open]").forEach(function (button) {
    button.addEventListener("click", function () {
      var dialog = document.getElementById(button.getAttribute("data-dialog-open"));
      if (dialog) dialog.showModal();
    });
  });

  document.querySelectorAll("[data-dialog-close]").forEach(function (button) {
    button.addEventListener("click", function () {
      button.closest("dialog").close();
    });
  });

  document.querySelectorAll("dialog.legal-dialog").forEach(function (dialog) {
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) dialog.close();
    });
  });
});
