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

// --- Click-to-expand modal for text cards ---

// Make text cards expandable by adding the clickable class and click handler.
// Called on page load and after dynamically loading more turns.
function initExpandableCards() {
    // Assistant text blocks (rendered markdown)
    document.querySelectorAll('.assistant-text[data-markdown]').forEach(el => {
        if (el.dataset.expandable) return;
        el.classList.add('text-card-expandable');
        el.dataset.expandable = 'true';
        el.addEventListener('click', (e) => {
            // Don't open modal if user is selecting text or clicking a link/button/code
            if (window.getSelection().toString()) return;
            // Allow clicks inside the parent turn-block <details>, but block clicks
            // on nested interactive elements like inner <details>, links, etc.
            const innerInteractive = e.target.closest('a, button, input, pre, code, .leaf-block, .tool-block, .thinking-block, .routine-tools-group, .notable-tool-card');
            if (innerInteractive && el.contains(innerInteractive)) return;
            openTextModal(el.innerHTML, 'Claude');
        });
    });

    // User text blocks (plain whitespace-pre-wrap text)
    document.querySelectorAll('.user-bubble .px-4.pb-3 > .text-sm.whitespace-pre-wrap').forEach(el => {
        if (el.dataset.expandable) return;
        el.classList.add('text-card-expandable');
        el.dataset.expandable = 'true';
        el.addEventListener('click', (e) => {
            if (window.getSelection().toString()) return;
            const innerInteractive = e.target.closest('a, button, input');
            if (innerInteractive && el.contains(innerInteractive)) return;
            openTextModal(`<div class="whitespace-pre-wrap">${el.innerHTML}</div>`, 'You');
        });
    });
}

function openTextModal(contentHtml, label) {
    const modal = document.getElementById('text-modal');
    const body = document.getElementById('text-modal-body');
    const labelEl = document.getElementById('text-modal-label');

    // Style the label color to match the sender
    if (label === 'You') {
        labelEl.style.color = '#93c5fd'; // blue-300
    } else {
        labelEl.style.color = '#d4a574'; // claude-accent
    }
    labelEl.textContent = label;
    body.innerHTML = contentHtml;

    modal.classList.remove('hidden');
    document.body.style.overflow = 'hidden'; // prevent background scroll

    // Re-highlight any code blocks copied into the modal
    modal.querySelectorAll('pre code').forEach(block => {
        hljs.highlightElement(block);
    });
}

function closeTextModal(event) {
    // If called from the overlay click, only close if clicking the overlay itself
    if (event && event.target !== event.currentTarget) return;
    const modal = document.getElementById('text-modal');
    modal.classList.add('hidden');
    document.body.style.overflow = '';
}

// Close modal on Escape key
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        const modal = document.getElementById('text-modal');
        if (!modal.classList.contains('hidden')) {
            closeTextModal();
        }
    }
});

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    renderMarkdown();
    hljs.highlightAll();
    initExpandableCards();
});
