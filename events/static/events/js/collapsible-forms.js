document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll(".comment-toggle").forEach(function (btn) {
    var form = btn.dataset.target ? document.getElementById(btn.dataset.target) : btn.nextElementSibling;
    if (!form) return;
    btn.addEventListener("click", function () {
      form.hidden = false;
      btn.hidden = true;
      var field = form.querySelector("input[type=text], textarea");
      if (field) field.focus();
    });
  });

  document.querySelectorAll(".reaction-toggle").forEach(function (btn) {
    var picker = btn.nextElementSibling;
    if (!picker) return;
    btn.addEventListener("click", function () {
      picker.hidden = !picker.hidden;
    });
  });
});
