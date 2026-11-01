"""Browser regressions using synthetic HTML exported by PlanningAuditTests.

Usage: python scripts/test_list_ui.py /path/to/fixture.html
Requires the Python playwright package and its Chromium browser.
"""
import re
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

root = Path(__file__).resolve().parents[1]
html = Path(sys.argv[1]).read_text()
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def route(request):
        url = request.request.url
        if "/static/" in url:
            path = root / "staticfiles" / url.split("/static/", 1)[1].split("?", 1)[0]
            if path.is_file():
                request.fulfill(path=str(path))
            else:
                request.fulfill(status=404, body="missing fixture asset")
        elif request.request.method == "GET" and "audit.invalid" in url:
            request.fulfill(content_type="text/html", body=html)
        else:
            request.abort()

    page.route("**/*", route)
    page.goto("https://audit.invalid/")
    games = page.locator(".games-list-item")
    order = lambda: games.evaluate_all("items => items.map(item => item.dataset.name)")
    page.locator(".game-sort-select").select_option("rating:desc")
    assert order() == ["alpha", "gamma", "beta", "ohne wertung"], order()
    page.locator(".game-sort-select").select_option("complexity:asc")
    assert order() == ["beta", "gamma", "alpha", "ohne wertung"], order()
    page.get_by_label("Mitbringer gesucht", exact=True).check()
    expect(page.locator(".games-list-item:visible")).to_have_count(3)
    page.get_by_label("Meine Zusagen", exact=True).check()
    expect(page.locator(".game-filter-empty")).to_be_visible()
    page.locator(".game-filter-reset").click()
    page.locator(".game-filter").fill("nichts-passendes")
    expect(page.locator(".game-filter-empty")).to_be_visible()
    page.locator(".open-wishes-list a").filter(has_text="Beta").click()
    expect(page.locator(".game-filter")).to_have_value("")
    expect(page.locator(".games-list-item:visible")).to_have_count(4)
    expect(page.locator('.games-list-item[data-name="beta"] .game-item-details')).to_have_attribute("open", "")
    # Clicking the same hash again must also reveal a filtered-out item.
    page.locator(".game-filter").fill("nichts-passendes")
    page.locator(".open-wishes-list a").filter(has_text="Beta").click()
    expect(page.locator(".game-filter")).to_have_value("")
    fullscreen = page.locator(".fullscreen-toggle-btn")
    fullscreen.click()
    expect(fullscreen).to_have_text("Vollbild verlassen")
    assert page.evaluate("document.fullscreenElement.classList.contains('game-list-wrapper')")
    expect(fullscreen).to_be_visible()
    fullscreen.click()
    expect(fullscreen).to_have_text("Vollbild")
    orga = page.locator("#orga-section")
    expect(orga.locator(".planning-progress")).to_contain_text("1 von 2 erledigt")
    orga.locator(".planning-status-filter").select_option("unfinished")
    expect(orga.locator("tr[data-planning-status]:visible")).to_have_count(1)
    orga.locator(".planning-person-filter").fill("MILA")
    expect(orga.locator("tr[data-planning-status]:visible")).to_have_count(1)
    orga.locator(".planning-person-filter").fill("Unbekannt")
    expect(orga.locator(".planning-empty-filter")).to_be_visible()
    orga.locator(".planning-reset").click()
    expect(orga.locator("tr[data-planning-status]:visible")).to_have_count(2)
    form = page.locator(".todo-completion-form")
    checkbox = form.locator('input[name="done"]')
    revisions = []

    def save(request):
        revisions.append(request.request.post_data)
        request.fulfill(content_type="application/json", body='{"status":"done","label":"Erledigt","revision":1}')

    page.route("**/erledigt/", save)
    before = page.url
    checkbox.check()
    expect(form.locator(".todo-save-feedback")).to_have_text("Gespeichert")
    expect(checkbox).to_be_checked()
    expect(form.locator('input[name="revision"]')).to_have_value("1")
    expect(page.locator("#members-section .planning-progress")).to_contain_text("1 von 1 erledigt")
    assert page.url == before
    page.unroute("**/erledigt/", save)
    page.route("**/erledigt/", lambda request: request.fulfill(status=409, content_type="application/json", body='{"error":"Inzwischen geändert"}'))
    checkbox.uncheck()
    expect(form.locator(".todo-save-feedback")).to_have_text("Inzwischen geändert")
    expect(checkbox).to_be_checked()
    page.unroute("**/erledigt/")
    page.route("**/erledigt/", lambda request: request.abort())
    checkbox.uncheck()
    expect(checkbox).to_be_checked()
    expect(form.locator(".todo-save-feedback")).not_to_have_text("Wird gespeichert …")
    # Phone view: controls remain usable without horizontal page overflow.
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#games-section").scroll_into_view_if_needed()
    expect(page.locator(".game-quick-filters")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    page.screenshot(path=str(Path(sys.argv[1]).with_suffix(".mobile.png")), full_page=True)
    assert not errors, errors
    page.close()
    # No-JS completion has a visible submit button and a conflict token.
    context = browser.new_context(java_script_enabled=False)
    context.route("**/*", route)
    fallback = context.new_page()
    fallback.goto("https://audit.invalid/")
    expect(fallback.locator('.todo-completion-form button[type="submit"]')).to_be_visible()
    expect(fallback.locator('.todo-completion-form input[name="revision"]')).to_have_count(1)
    context.close()
    unsupported = browser.new_context()
    unsupported.add_init_script("Element.prototype.requestFullscreen = undefined; Element.prototype.webkitRequestFullscreen = undefined;")
    unsupported.route("**/*", route)
    fallback_page = unsupported.new_page()
    fallback_page.goto("https://audit.invalid/")
    expect(fallback_page.locator(".fullscreen-toggle-btn")).to_be_hidden()
    expect(fallback_page.locator(".game-quick-filters")).to_be_visible()
    unsupported.close()
    rejected = browser.new_context()
    rejected.add_init_script("Element.prototype.requestFullscreen = () => Promise.reject(new Error('Unavailable'));")
    rejected.route("**/*", route)
    rejected_page = rejected.new_page()
    rejected_page.goto("https://audit.invalid/")
    rejected_page.locator(".fullscreen-toggle-btn").click()
    expect(rejected_page.locator(".game-fullscreen-feedback")).to_contain_text("nicht verfügbar")
    expect(rejected_page.locator(".fullscreen-toggle-btn")).to_have_text("Vollbild")
    rejected.close()
    browser.close()
print("Browser checks passed: sort, filters, anchors, fullscreen, TODO save/conflict/network failure, mobile and no-JS.")
