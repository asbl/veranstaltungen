(() => {
    const container = document.querySelector('[data-list-settings]');
    if (!container) return;
    const order = document.getElementById('id_list_order');
    let dragged = null;
    function update() {
        const rows = [...container.children];
        order.value = rows.map(row => row.dataset.listKey).join(',');
        rows.forEach((row, index) => {
            row.querySelector('[data-list-move="up"]').disabled = index === 0;
            row.querySelector('[data-list-move="down"]').disabled = index === rows.length - 1;
        });
    }
    container.addEventListener('click', event => {
        const button = event.target.closest('[data-list-move]');
        if (!button) return;
        const row = button.closest('[data-list-key]');
        const before = button.dataset.listMove === 'up';
        const sibling = before ? row.previousElementSibling : row.nextElementSibling;
        if (!sibling) return;
        container.insertBefore(row, before ? sibling : sibling.nextElementSibling);
        update();
        document.querySelector('[data-list-order-status]').textContent = 'Listenreihenfolge geändert.';
        (button.disabled ? row.querySelector(`[data-list-move="${before ? 'down' : 'up'}"]`) : button).focus();
    });
    container.addEventListener('dragstart', event => {
        if (!event.target.closest('.list-drag-handle')) return;
        dragged = event.target.closest('[data-list-key]');
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', dragged.dataset.listKey);
        dragged.classList.add('is-dragging');
    });
    container.addEventListener('dragover', event => {
        if (!dragged) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = 'move';
    });
    container.addEventListener('drop', event => {
        if (!dragged) return;
        event.preventDefault();
        const target = event.target.closest('[data-list-key]');
        if (target && target !== dragged) {
            const rect = target.getBoundingClientRect();
            container.insertBefore(dragged, event.clientY < rect.top + rect.height / 2 ? target : target.nextElementSibling);
            update();
            document.querySelector('[data-list-order-status]').textContent = 'Listenreihenfolge geändert.';
        }
    });
    container.addEventListener('dragend', () => {
        if (dragged) dragged.classList.remove('is-dragging');
        dragged = null;
    });
    update();
})();
