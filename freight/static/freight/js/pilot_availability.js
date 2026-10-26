"use strict";

// Keep navigation, filters and unsaved inputs intact when changing a shift.
(() => {
    const form = document.getElementById("pilot_availability_form");
    if (!form) return;
    const main = form.querySelector("[data-pilot-main]");
    const dropdown = form.querySelector("[data-pilot-dropdown]");
    const error = form.querySelector("[data-pilot-error]");
    const modal = document.getElementById("pilot_availability_modal");
    const custom = document.getElementById("pilot_custom_availability_form");
    const customError = custom.querySelector("[data-pilot-custom-error]");
    const hours = document.getElementById("pilot_hours");
    const minutes = document.getElementById("pilot_minutes");
    const eveInput = document.getElementById("pilot_until_eve");
    const localInput = document.getElementById("pilot_until_local");
    const durationMode = document.getElementById("pilot_mode_duration");
    const durationFields = document.getElementById("pilot_duration_fields");
    const untilFields = document.getElementById("pilot_until_fields");
    const submit = custom.querySelector("[data-pilot-custom-submit]");
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    let busy = false;
    let expiryTimer;
    let clockTimer;
    let absoluteEnd = null;

    function formatTime(date, utc) {
        return new Intl.DateTimeFormat(undefined, {
            year: "numeric", month: "short", day: "2-digit",
            hour: "2-digit", minute: "2-digit", hourCycle: "h23",
            timeZone: utc ? "UTC" : zone,
            timeZoneName: utc ? "short" : "shortOffset",
        }).format(date);
    }

    function showError(target, message) {
        target.textContent = message;
        target.classList.remove("d-none");
    }

    function setBusy(value) {
        busy = value;
        [main, dropdown, submit].forEach(button => { button.disabled = value; });
        if (value) main.setAttribute("aria-busy", "true");
        else main.removeAttribute("aria-busy");
    }

    function renderStatus(data) {
        const online = data.is_online;
        form.elements.online.value = online ? "0" : "1";
        main.setAttribute("aria-pressed", String(online));
        [main, dropdown].forEach(button => {
            ["btn-success", "bg-success-subtle", "text-success-emphasis"].forEach(
                name => button.classList.toggle(name, online)
            );
            ["btn-light", "text-black"].forEach(
                name => button.classList.toggle(name, !online)
            );
        });
        const icon = main.querySelector("i");
        icon.classList.toggle("fa-check-circle", online);
        icon.classList.toggle("fa-power-off", !online);
        form.querySelector("[data-pilot-online]").classList.toggle("d-none", !online);
        form.querySelector("[data-pilot-offline]").classList.toggle("d-none", online);
        form.dataset.onlineUntil = online && data.online_until ? data.online_until : "";
        const until = form.dataset.onlineUntil ? new Date(form.dataset.onlineUntil) : null;
        const deadline = form.querySelector("[data-pilot-deadline]");
        deadline.classList.toggle("d-none", !until);
        form.querySelector("[data-pilot-timed]").classList.toggle("d-none", !until);
        const title = until ? "Online until " + formatTime(until, true) + " / " + formatTime(until, false) : "";
        deadline.textContent = title;
        main.title = title;
        clearTimeout(expiryTimer);
        if (until) {
            expiryTimer = setTimeout(refreshStatus, Math.max(1000, Math.min(until.getTime() - Date.now() + 500, 2147480000)));
        }
        if (typeof data.rush_available === "boolean") {
            document.dispatchEvent(new CustomEvent("freight:availability-changed", {
                detail: { rushAvailable: data.rush_available },
            }));
        }
    }

    async function refreshStatus() {
        if (busy) { expiryTimer = setTimeout(refreshStatus, 1000); return; }
        try {
            const response = await fetch(form.dataset.statusUrl, {
                headers: { Accept: "application/json" }, credentials: "same-origin", cache: "no-store",
            });
            if (!response.ok) throw new Error("Status unavailable");
            const data = await response.json();
            if (typeof data.is_online !== "boolean") throw new Error("Invalid status");
            renderStatus(data);
        } catch (exception) {
            expiryTimer = setTimeout(refreshStatus, 15000);
        }
    }

    async function updateAvailability(options = {}) {
        if (busy) return false;
        setBusy(true);
        error.classList.add("d-none");
        customError.classList.add("d-none");
        try {
            const body = new FormData(form);
            Object.entries(options).forEach(([name, value]) => body.set(name, value));
            const response = await fetch(form.action, {
                method: "POST", body,
                headers: { Accept: "application/json" }, credentials: "same-origin",
            });
            const data = await response.json();
            if (!response.ok) {
                const messages = Object.values(data.errors || {}).flat().map(item => item.message);
                throw new Error(messages.join(" ") || "Could not update availability. Please try again.");
            }
            if (typeof data.is_online !== "boolean") throw new Error("Could not update availability. Please try again.");
            renderStatus(data);
            return true;
        } catch (exception) {
            const target = modal.classList.contains("show") ? customError : error;
            showError(target, exception.message || "Could not update availability. Please try again.");
            return false;
        } finally {
            setBusy(false);
        }
    }

    form.addEventListener("submit", event => {
        event.preventDefault();
        updateAvailability();
    });
    form.querySelectorAll("[data-pilot-duration]").forEach(button => {
        button.addEventListener("click", () => {
            bootstrap.Dropdown.getInstance(dropdown)?.hide();
            updateAvailability({ online: "1", duration_minutes: button.dataset.pilotDuration });
        });
    });

    function durationMinutes() {
        const h = Number(hours.value);
        const m = Number(minutes.value);
        return Number.isInteger(h) && h >= 0 && Number.isInteger(m) && m >= 0 && m <= 59 ? h * 60 + m : 0;
    }

    function selectedEnd() {
        return durationMode.checked ? new Date(Date.now() + durationMinutes() * 60000) : absoluteEnd;
    }

    function dateInputValue(date, utc) {
        if (!date || Number.isNaN(date.getTime())) return "";
        const adjusted = utc ? date : new Date(date.getTime() - date.getTimezoneOffset() * 60000);
        return adjusted.toISOString().slice(0, 16);
    }

    function updatePreview() {
        durationFields.disabled = !durationMode.checked;
        untilFields.disabled = durationMode.checked;
        durationFields.classList.toggle("d-none", !durationMode.checked);
        untilFields.classList.toggle("d-none", durationMode.checked);
        const end = selectedEnd();
        const valid = end && !Number.isNaN(end.getTime()) && end > new Date();
        modal.querySelector("[data-pilot-preview-eve]").textContent = valid ? formatTime(end, true) : "Choose a future time";
        modal.querySelector("[data-pilot-preview-local]").textContent = valid ? formatTime(end, false) : "";
    }

    function updateClock() {
        const now = new Date();
        modal.querySelector("[data-pilot-now-eve]").textContent = formatTime(now, true);
        modal.querySelector("[data-pilot-now-local]").textContent = formatTime(now, false);
        updatePreview();
    }
    modal.querySelectorAll("[data-pilot-zone]").forEach(el => { el.textContent = "(" + zone + ")"; });
    [hours, minutes].forEach(input => input.addEventListener("input", updatePreview));
    custom.querySelectorAll('[name="end_mode"]').forEach(input => input.addEventListener("change", updatePreview));
    eveInput.addEventListener("input", () => {
        absoluteEnd = eveInput.value ? new Date(eveInput.value + "Z") : null;
        localInput.value = dateInputValue(absoluteEnd, false);
        updatePreview();
    });
    localInput.addEventListener("input", () => {
        absoluteEnd = localInput.value ? new Date(localInput.value) : null;
        eveInput.value = dateInputValue(absoluteEnd, true);
        updatePreview();
    });
    modal.addEventListener("show.bs.modal", () => {
        customError.classList.add("d-none");
        absoluteEnd = form.dataset.onlineUntil ? new Date(form.dataset.onlineUntil) : new Date(Date.now() + 3600000);
        absoluteEnd.setSeconds(0, 0);
        eveInput.value = dateInputValue(absoluteEnd, true);
        localInput.value = dateInputValue(absoluteEnd, false);
        updateClock();
        clearInterval(clockTimer);
        clockTimer = setInterval(updateClock, 1000);
    });
    modal.addEventListener("hidden.bs.modal", () => clearInterval(clockTimer));
    custom.addEventListener("submit", async event => {
        event.preventDefault();
        const end = selectedEnd();
        if (!end || Number.isNaN(end.getTime()) || end <= new Date()) {
            showError(customError, "Choose a positive duration or a future end time.");
            return;
        }
        const options = durationMode.checked
            ? { online: "1", duration_minutes: String(durationMinutes()) }
            : { online: "1", until: end.toISOString() };
        if (await updateAvailability(options)) bootstrap.Modal.getInstance(modal).hide();
    });

    renderStatus({ is_online: main.getAttribute("aria-pressed") === "true", online_until: form.dataset.onlineUntil });
})();
