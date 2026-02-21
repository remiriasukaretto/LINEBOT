const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';

function getQueryParams() {
    const params = new URLSearchParams();
    const select = document.getElementById('type-filter');
    if (select && select.value) params.set('type_id', select.value);
    const sortBy = document.getElementById('sort-by');
    if (sortBy && sortBy.value) params.set('sort_by', sortBy.value);
    const sortOrder = document.getElementById('sort-order');
    if (sortOrder && sortOrder.value) params.set('sort_order', sortOrder.value);
    const q = params.toString();
    return q ? `?${q}` : '';
}

function createCsrfInput() {
    const input = document.createElement('input');
    input.type = 'hidden';
    input.name = '_csrf_token';
    input.value = csrfToken;
    return input;
}

function buildStatusCell(row) {
    const td = document.createElement('td');
    const badge = document.createElement('span');
    if (row.status === 'waiting') {
        badge.className = 'badge bg-warning text-dark';
        badge.textContent = '待機中';
    } else if (row.status === 'called') {
        badge.className = 'badge bg-info';
        badge.textContent = '呼出中';
    } else {
        badge.className = 'badge bg-success';
        badge.textContent = '到着済み';
    }
    td.appendChild(badge);
    return td;
}

function buildActionCell(row) {
    const td = document.createElement('td');
    if (row.status === 'waiting') {
        const form = document.createElement('form');
        form.method = 'POST';
        form.action = `/admin/call/${row.id}`;
        form.className = 'd-inline';
        const button = document.createElement('button');
        button.type = 'submit';
        button.className = 'btn btn-sm btn-success';
        button.textContent = '呼出';
        form.appendChild(createCsrfInput());
        form.appendChild(button);
        td.appendChild(form);
    } else if (row.status === 'called') {
        const span = document.createElement('span');
        span.className = 'text-muted small';
        span.textContent = '到着待ち';
        td.appendChild(span);
    } else {
        const form = document.createElement('form');
        form.method = 'POST';
        form.action = `/admin/finish/${row.id}`;
        form.className = 'd-inline';
        const button = document.createElement('button');
        button.type = 'submit';
        button.className = 'btn btn-sm btn-primary';
        button.textContent = '確認完了';
        form.appendChild(createCsrfInput());
        form.appendChild(button);
        td.appendChild(form);
    }
    return td;
}

function buildRow(row) {
    const tr = document.createElement('tr');
    const tdId = document.createElement('td');
    tdId.textContent = row.id ?? '';
    const tdType = document.createElement('td');
    tdType.textContent = row.type || '-';
    const tdMessage = document.createElement('td');
    tdMessage.textContent = row.message || '-';
    tr.appendChild(tdId);
    tr.appendChild(tdType);
    tr.appendChild(tdMessage);
    tr.appendChild(buildStatusCell(row));
    tr.appendChild(buildActionCell(row));
    return tr;
}

async function refreshActiveRows() {
    try {
        const res = await fetch('/admin/data' + getQueryParams(), { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();
        const tbody = document.getElementById('active-rows');
        if (!tbody) return;
        tbody.textContent = '';
        (data.rows || []).forEach((row) => {
            tbody.appendChild(buildRow(row));
        });
    } catch (e) {
        // no-op
    }
}

async function refreshTypeCounts() {
    try {
        const res = await fetch('/admin/type_counts', { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();
        const container = document.getElementById('type-counts');
        if (!container) return;
        container.textContent = '';
        if (!data.counts || data.counts.length === 0) {
            const badge = document.createElement('span');
            badge.className = 'badge bg-secondary';
            badge.textContent = '未設定: 0';
            container.appendChild(badge);
            return;
        }
        data.counts.forEach((c) => {
            const badge = document.createElement('span');
            badge.className = 'badge bg-secondary';
            const name = c.name || '未設定';
            badge.textContent = `${name}: ${c.count}`;
            container.appendChild(badge);
        });
    } catch (e) {
        // no-op
    }
}

function applyAdminFilters() {
    window.location.href = '/admin' + getQueryParams();
}

document.getElementById('type-filter')?.addEventListener('change', applyAdminFilters);
document.getElementById('sort-by')?.addEventListener('change', applyAdminFilters);
document.getElementById('sort-order')?.addEventListener('change', applyAdminFilters);

setInterval(() => {
    refreshActiveRows();
    refreshTypeCounts();
}, 5000);
