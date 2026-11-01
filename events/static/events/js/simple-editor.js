document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll(".simple-editor").forEach(function (editor) {
    var textarea = editor.querySelector("textarea");
    if (!textarea) return;

    editor.querySelectorAll("[data-wrap]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var token = btn.dataset.wrap;
        var start = textarea.selectionStart;
        var end = textarea.selectionEnd;
        var selected = textarea.value.slice(start, end) || "Text";
        var before = textarea.value.slice(0, start);
        var after = textarea.value.slice(end);
        textarea.value = before + token + selected + token + after;
        textarea.focus();
        textarea.selectionStart = start + token.length;
        textarea.selectionEnd = start + token.length + selected.length;
      });
    });

    editor.querySelectorAll("[data-prefix]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var prefix = btn.dataset.prefix;
        var start = textarea.selectionStart;
        var lineStart = textarea.value.lastIndexOf("\n", start - 1) + 1;
        textarea.value = textarea.value.slice(0, lineStart) + prefix + textarea.value.slice(lineStart);
        textarea.focus();
        textarea.selectionStart = textarea.selectionEnd = start + prefix.length;
      });
    });
  });
});
