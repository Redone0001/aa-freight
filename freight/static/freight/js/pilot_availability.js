"use strict";

// Update only the toggle, preserving the current URL, tabs, filters and form values.
(() => {
    const form = document.getElementById("pilot_availability_form");
    if (!form) return;
    const button = form.querySelector("button");
    const error = form.querySelector("[data-pilot-error]");
    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (button.disabled) return;
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
        error.classList.add("d-none");
        try {
            const response = await fetch(form.action, {
                method: "POST",
                body: new FormData(form),
                headers: { Accept: "application/json" },
                credentials: "same-origin",
            });
            if (!response.ok) throw new Error("Availability update failed");
            const data = await response.json();
            if (typeof data.is_online !== "boolean") throw new Error("Invalid response");
            const online = data.is_online;
            form.elements.online.value = online ? "0" : "1";
            button.setAttribute("aria-pressed", String(online));
            ["btn-success", "bg-success-subtle", "text-success-emphasis"].forEach(
                (name) => button.classList.toggle(name, online)
            );
            ["btn-light", "text-black"].forEach(
                (name) => button.classList.toggle(name, !online)
            );
            document.dispatchEvent(new CustomEvent("freight:availability-changed", {
                detail: { rushAvailable: data.rush_available },
            }));
            const icon = button.querySelector("i");
            icon.classList.toggle("fa-check-circle", online);
            icon.classList.toggle("fa-power-off", !online);
            form.querySelector("[data-pilot-online]").classList.toggle("d-none", !online);
            form.querySelector("[data-pilot-offline]").classList.toggle("d-none", online);
        } catch (exception) {
            error.classList.remove("d-none");
        } finally {
            button.disabled = false;
            button.removeAttribute("aria-busy");
        }
    });
})();
