(function () {
    "use strict";

    var shell = document.querySelector(".event-section-nav-shell");
    if (!shell) return;

    var nav = shell.querySelector(".event-section-nav");
    var links = Array.from(nav.querySelectorAll('a[href^="#"]'));
    var sections = links.map(function (link) {
        return document.querySelector(link.getAttribute("href"));
    }).filter(Boolean);
    var scheduled = false;
    var activeSectionId = "";

    function revealLink(link, behavior) {
        var targetLeft = link.offsetLeft - (nav.clientWidth - link.offsetWidth) / 2;
        nav.scrollTo({left: targetLeft, behavior: behavior || "auto"});
    }

    function updateOverflowHints() {
        shell.classList.toggle("can-scroll-left", nav.scrollLeft > 2);
        shell.classList.toggle(
            "can-scroll-right",
            nav.scrollLeft + nav.clientWidth < nav.scrollWidth - 2
        );
    }

    function setActiveSection() {
        var threshold = shell.getBoundingClientRect().bottom + 16;
        var active = sections[0];

        sections.forEach(function (section) {
            if (section.getBoundingClientRect().top <= threshold) active = section;
        });

        links.forEach(function (link) {
            var isActive = active && link.getAttribute("href") === "#" + active.id;
            if (isActive) {
                link.setAttribute("aria-current", "location");
            } else {
                link.removeAttribute("aria-current");
            }
        });

        if (active && active.id !== activeSectionId) {
            activeSectionId = active.id;
            var activeLink = links.find(function (link) {
                return link.getAttribute("href") === "#" + active.id;
            });
            if (activeLink) revealLink(activeLink, "smooth");
        }
    }

    function update() {
        scheduled = false;
        updateOverflowHints();
        setActiveSection();
    }

    function scheduleUpdate() {
        if (scheduled) return;
        scheduled = true;
        window.requestAnimationFrame(update);
    }

    links.forEach(function (link) {
        link.addEventListener("click", function () {
            revealLink(link, "smooth");
        });
    });
    nav.addEventListener("scroll", scheduleUpdate, {passive: true});
    window.addEventListener("scroll", scheduleUpdate, {passive: true});
    window.addEventListener("resize", scheduleUpdate);
    window.addEventListener("hashchange", scheduleUpdate);
    update();
})();
