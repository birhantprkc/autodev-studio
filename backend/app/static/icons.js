// Inline SVG icon set (lucide-style, MIT paths) — no icon-font CDN needed.
// Usage: icon("name") or icon("name", 18) → svg string.
(function () {
  const P = {
    spark: '<path d="M12 3l1.9 5.7a2 2 0 0 0 1.3 1.3L21 12l-5.8 1.9a2 2 0 0 0-1.3 1.3L12 21l-1.9-5.8a2 2 0 0 0-1.3-1.3L3 12l5.8-1.9a2 2 0 0 0 1.3-1.3L12 3z"/>',
    chat: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    board: '<rect x="3" y="3" width="7" height="18" rx="1.5"/><rect x="14" y="3" width="7" height="12" rx="1.5"/>',
    bot: '<rect x="4" y="8" width="16" height="12" rx="2"/><path d="M12 8V4M8 4h8"/><circle cx="9" cy="13" r="1" fill="currentColor" stroke="none"/><circle cx="15" cy="13" r="1" fill="currentColor" stroke="none"/><path d="M9 17h6"/>',
    book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V4a2 2 0 0 0-2-2H6.5A2.5 2.5 0 0 0 4 4.5v15z"/><path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"/>',
    coins: '<circle cx="12" cy="12" r="9"/><path d="M14.8 9.2a3.2 3.2 0 0 0-2.8-1.4c-1.8 0-3 1-3 2.2 0 3 6 1.6 6 4.4 0 1.2-1.2 2.2-3 2.2a3.4 3.4 0 0 1-3-1.5M12 6.4v1.4m0 8.4v1.4"/>',
    gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.03 1.56V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1.11-1.56 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.56-1.03H3a2 2 0 1 1 0-4h.09a1.7 1.7 0 0 0 1.56-1.11 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34h.08A1.7 1.7 0 0 0 10 4.09V4a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1.03 1.56 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87v.08c.26.63.87 1.05 1.56 1.05H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.56 1.03z"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    send: '<path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4 20-7z"/>',
    chevronDown: '<path d="m6 9 6 6 6-6"/>',
    chevronUp: '<path d="m18 15-6-6-6 6"/>',
    chevronRight: '<path d="m9 18 6-6-6-6"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    checkCircle: '<circle cx="12" cy="12" r="9"/><path d="m8.5 12 2.5 2.5 4.8-5"/>',
    x: '<path d="M18 6 6 18M6 6l12 12"/>',
    external: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    play: '<polygon points="6 3 20 12 6 21 6 3"/>',
    refresh: '<path d="M21 12a9 9 0 1 1-2.64-6.36L21 8"/><path d="M21 3v5h-5"/>',
    trash: '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M10 11v6M14 11v6"/>',
    branch: '<circle cx="6" cy="6" r="2.6"/><circle cx="6" cy="18" r="2.6"/><circle cx="18" cy="6" r="2.6"/><path d="M6 8.6v6.8"/><path d="M18 8.6a9 9 0 0 1-9 9"/>',
    pr: '<circle cx="6" cy="6" r="2.6"/><circle cx="6" cy="18" r="2.6"/><circle cx="18" cy="18" r="2.6"/><path d="M6 8.6v6.8"/><path d="M13 6h3a2 2 0 0 1 2 2v7.4"/><path d="m13 9-3-3 3-3"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-3.5 3.6-6 8-6s8 2.5 8 6"/>',
    users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c0-3 2.9-5.2 6.5-5.2s6.5 2.2 6.5 5.2"/><path d="M16.5 4.6a3.5 3.5 0 0 1 0 6.8M18.5 15.1c1.8.8 3 2.2 3 4.1"/>',
    logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
    sun: '<circle cx="12" cy="12" r="4.4"/><path d="M12 2.5v2.2m0 14.6v2.2M4.3 4.3l1.6 1.6m12.2 12.2 1.6 1.6M2.5 12h2.2m14.6 0h2.2M4.3 19.7l1.6-1.6M18.1 5.9l1.6-1.6"/>',
    moon: '<path d="M21 12.8A8.5 8.5 0 1 1 11.2 3a6.6 6.6 0 0 0 9.8 9.8z"/>',
    alert: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4"/><circle cx="12" cy="17" r="0.6" fill="currentColor" stroke="none"/>',
    fileCode: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="m9.5 12.5-2 2 2 2m5-4 2 2-2 2"/>',
    layers: '<path d="m12 2 9 5-9 5-9-5 9-5z"/><path d="m3 12 9 5 9-5"/><path d="m3 17 9 5 9-5"/>',
    terminal: '<path d="m4 17 6-6-6-6"/><path d="M12 19h8"/>',
    eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/>',
    lock: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    key: '<circle cx="8" cy="15" r="4.5"/><path d="m11.2 11.8 8.3-8.3M17 5l2.5 2.5M14 8l2.5 2.5"/>',
    database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    activity: '<path d="M22 12h-4l-3 8L9 4l-3 8H2"/>',
    zap: '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
    shield: '<path d="M12 22s8-3.4 8-10V5l-8-3-8 3v7c0 6.6 8 10 8 10z"/>',
    workflow: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/><path d="M10 6.5h4a2 2 0 0 1 2 2V14"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
    arrowRight: '<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>',
    folder: '<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-8L9.6 3.7A2 2 0 0 0 8 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2z"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><circle cx="12" cy="8" r="0.6" fill="currentColor" stroke="none"/>',
    dollar: '<path d="M12 2v20"/><path d="M17 5.5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>',
    // The CodeJury app badge: the balance scale alone, filling the tile.
    // The full logo (static/logo.svg) is braces around the scale with </> on
    // the beam, but scaled into a 28px badge all three collapse into an
    // unreadable smudge — rendered side by side, only the bare scale survives
    // the size. The braces stay on the wide lockup, where there's room.
    codejury: '<path d="M12 3.4v17.2"/><path d="M8 20.6h8"/>'
      + '<path d="M4 7.4h4c2 0 3-.6 4-1 1 .4 2 1 4 1h4"/>'
      + '<path d="m4 7.4-2.4 5.2h4.8z"/><path d="m20 7.4-2.4 5.2h4.8z"/>',
    // Jury / ensemble review, inline at text size.
    scales: '<path d="M12 3v18"/><path d="M8 21h8"/><path d="M4 7h4c2 0 3-.6 4-1 1 .4 2 1 4 1h4"/>'
      + '<path d="m4 7-2.5 5.5h5z"/><path d="m20 7-2.5 5.5h5z"/>',
  };

  window.icon = function (name, size = 16) {
    const path = P[name] || P.info;
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
  };

})();
