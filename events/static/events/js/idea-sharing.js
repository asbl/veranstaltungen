(function () {
    "use strict";

    document.querySelectorAll(".idea-share-link").forEach(function (link) {
        link.addEventListener("click", async function (event) {
            event.preventDefault();
            var url = link.href;
            var title = link.dataset.shareTitle || document.title;

            if (navigator.share) {
                try {
                    await navigator.share({title: title, url: url});
                    return;
                } catch (error) {
                    if (error.name === "AbortError") return;
                }
            }

            try {
                await navigator.clipboard.writeText(url);
                var oldText = link.lastChild.textContent;
                link.lastChild.textContent = " Link kopiert";
                window.setTimeout(function () {
                    link.lastChild.textContent = oldText;
                }, 2000);
            } catch (error) {
                window.prompt("Diesen Link kopieren:", url);
            }
        });
    });
}());
