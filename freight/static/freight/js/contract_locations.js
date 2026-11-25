/* Keep the displayed structure, clipboard text and next calculation in sync. */
document.addEventListener('click', function (event) {
    const option = event.target.closest('[data-contract-location] [data-location-id]');
    if (!option) {
        return;
    }

    const selector = option.closest('[data-contract-location]');
    const name = option.dataset.locationName;
    selector.querySelector('[data-selected-location]').textContent = name;
    selector.querySelector('.copy_to_clipboard').setAttribute('data-clipboard-text', name);

    const field = document.getElementById(selector.dataset.fieldId);
    if (field) {
        field.value = option.dataset.locationId;
    }
    selector.querySelectorAll('[data-location-id]').forEach(function (item) {
        const selected = item === option;
        item.classList.toggle('active', selected);
        item.setAttribute('aria-pressed', String(selected));
    });
});
