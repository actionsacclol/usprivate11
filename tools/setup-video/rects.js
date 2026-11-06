window.__r = (pairs) => Object.fromEntries(pairs.map(([k, f]) => { const el = f(); if (!el) return [k, null]; const r = el.getBoundingClientRect(); return [k, [r.left, r.top, r.right, r.bottom].map(v => Math.round(v * 1.2))]; }));
window.__btn = (t, i = 0) => [...document.querySelectorAll('button')].filter(b => b.innerText.trim().startsWith(t))[i];
window.__hook = (el, pred) => { const k = Object.keys(el).find(k => k.startsWith('__reactFiber$')); let f = el[k]; while (f) { let h = f.memoizedState; if (f.tag === 0 || f.tag === 11 || f.tag === 15) { while (h && typeof h === 'object' && 'next' in h) { if (h.queue && pred(h.memoizedState)) return h; h = h.next; } } f = f.return; } return null; };
// Load with {"evalFile":"rects.js"}. __r measures boxes in screenshot px; __hook finds a React state hook by its value.
'rects';
