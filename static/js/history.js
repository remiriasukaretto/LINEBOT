function getHistoryQueryParams() {
    const params = new URLSearchParams();
    const select = document.getElementById('history-type-filter');
    if (select && select.value) params.set('type_id', select.value);
    const sortBy = document.getElementById('history-sort-by');
    if (sortBy && sortBy.value) params.set('sort_by', sortBy.value);
    const sortOrder = document.getElementById('history-sort-order');
    if (sortOrder && sortOrder.value) params.set('sort_order', sortOrder.value);
    const q = params.toString();
    return q ? `?${q}` : '';
}

function applyHistoryFilters() {
    window.location.href = '/admin/history' + getHistoryQueryParams();
}

document.getElementById('history-type-filter')?.addEventListener('change', applyHistoryFilters);
document.getElementById('history-sort-by')?.addEventListener('change', applyHistoryFilters);
document.getElementById('history-sort-order')?.addEventListener('change', applyHistoryFilters);
