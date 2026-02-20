// Render markdown in assistant text blocks
function renderMarkdown() {
    document.querySelectorAll('[data-markdown]').forEach(el => {
        if (el.dataset.rendered) return;
        const raw = el.textContent;
        try {
            el.innerHTML = marked.parse(raw);
        } catch {
            // fallback: leave as-is
        }
        el.dataset.rendered = 'true';
    });
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    renderMarkdown();
    hljs.highlightAll();
});
