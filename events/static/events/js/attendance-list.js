document.addEventListener("DOMContentLoaded", function () {
  var list = document.getElementById("attendance-list");
  if (!list) return;
  var dayFilter = document.getElementById("attendance-day-filter");
  var roleFilter = document.getElementById("attendance-role-filter");
  var sortSelect = document.getElementById("attendance-sort");

  function rows() {
    return Array.prototype.filter.call(list.children, function (li) {
      return li.hasAttribute("data-name");
    });
  }

  function applyFilter() {
    var day = dayFilter ? dayFilter.value : "";
    var role = roleFilter ? roleFilter.value : "";
    rows().forEach(function (li) {
      var days = (li.dataset.days || "").trim().split(/\s+/);
      var matchesDay = !day || days.indexOf(day) !== -1;
      var matchesRole = !role || li.dataset.role === role;
      li.style.display = matchesDay && matchesRole ? "" : "none";
    });
  }

  function applySort() {
    var value = sortSelect ? sortSelect.value : "name";
    var items = rows();
    items.sort(function (a, b) {
      if (value === "role") {
        return (
          (a.dataset.role || "").localeCompare(b.dataset.role || "") ||
          (a.dataset.name || "").localeCompare(b.dataset.name || "")
        );
      }
      return (a.dataset.name || "").localeCompare(b.dataset.name || "");
    });
    items.forEach(function (li) {
      list.appendChild(li);
    });
  }

  if (dayFilter) dayFilter.addEventListener("change", applyFilter);
  if (roleFilter) roleFilter.addEventListener("change", applyFilter);
  if (sortSelect) sortSelect.addEventListener("change", applySort);
});
