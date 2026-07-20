function toggleSidebar() {
    document.getElementById("sidebar").classList.toggle("hidden");
}

/* Shared fetch wrapper used across admin/HOD/teacher pages (notifications,
   Edit Teacher modal, timetable filters, preference-window status, etc).
   Was referenced in 8 templates but never defined anywhere — every call
   threw "authFetch is not defined" and silently broke whatever UI depended
   on it (e.g. the Edit Teacher modal never populated). */
function authFetch(url, options) {
    options = options || {};
    if (!('credentials' in options)) options.credentials = 'same-origin';
    return fetch(url, options);
}

/* BASIC DETAILS */
function editBasic() {
    document.getElementById("basicView").classList.add("hidden");
    document.getElementById("basicEdit").classList.remove("hidden");
}

function cancelBasic() {
    document.getElementById("basicEdit").classList.add("hidden");
    document.getElementById("basicView").classList.remove("hidden");
}

/* ADDITIONAL DETAILS */
function editAdditional() {
    document.getElementById("additionalView").classList.add("hidden");
    document.getElementById("additionalEdit").classList.remove("hidden");
}

function cancelAdditional() {
    document.getElementById("additionalEdit").classList.add("hidden");
    document.getElementById("additionalView").classList.remove("hidden");
}
