"""Exercise popup interactions with a synthetic Django fixture and mocked writes."""
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect

root = Path(__file__).resolve().parents[1]
fixture = json.loads(Path(sys.argv[1]).read_text())
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    for width in (1280, 390):
        page = browser.new_page(viewport={"width": width, "height": 844})
        errors = []
        writes = []
        fail = [False]
        page.on('pageerror', lambda error: errors.append(str(error)))

        def serve(route):
            request = route.request
            path = urlsplit(request.url).path
            if '/static/events/js/planning-edit' in path:
                route.fulfill(path=str(root / 'events/static/events/js/planning-edit.js'))
            elif '/static/' in path:
                asset = root / 'staticfiles' / path.split('/static/', 1)[1]
                route.fulfill(path=str(asset)) if asset.is_file() else route.fulfill(status=404)
            elif request.method == 'POST':
                writes.append(request.post_data)
                if fail[0]:
                    route.abort()
                else:
                    route.fulfill(content_type='application/json', body=json.dumps({'location': '/#planning-1'}))
            elif path in (fixture['status_url'], fixture['people_url']):
                field = 'status' if path == fixture['status_url'] else 'people'
                route.fulfill(content_type='text/html', body=fixture[field])
            else:
                route.fulfill(content_type='text/html', body=fixture['page'])

        page.route('**/*', serve)
        page.goto('https://audit.invalid/')
        dialog = page.locator('#planning-edit-dialog')
        status = page.get_by_role('link', name='Status bearbeiten: Flyer', exact=True)
        status.click()
        expect(dialog).to_be_visible()
        expect(dialog.get_by_label('Status', exact=False)).to_be_focused()
        dialog.get_by_label('Status', exact=False).select_option('progress')
        page.keyboard.press('Escape')
        expect(dialog).not_to_be_visible()
        expect(status).to_be_focused()
        assert not writes

        responsible = page.get_by_role('link', name='Verantwortliche bearbeiten: Flyer', exact=True)
        expect(responsible).to_have_text('Noch offen')
        responsible.click()
        names = dialog.get_by_label('Verantwortlich', exact=False)
        expect(names).to_be_focused()
        names.fill('Anna, S')
        fail[0] = True
        dialog.get_by_role('button', name='Verantwortliche speichern').click()
        expect(dialog.locator('[data-planning-feedback]')).to_contain_text('Speichern')
        expect(dialog.get_by_role('button', name='Verantwortliche speichern')).to_be_enabled()
        expect(names).to_have_value('Anna, S')
        expect(dialog).to_be_visible()
        assert dialog.bounding_box()['width'] <= width
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        fail[0] = False
        dialog.get_by_role('button', name='Verantwortliche speichern').click()
        expect(dialog).not_to_be_visible()
        assert 'Anna, S' in writes[-1]
        assert not errors, errors
        page.close()
    browser.close()
print('Popup UI: desktop/mobile, keyboard, cancel, save, network failure and retained input OK')
