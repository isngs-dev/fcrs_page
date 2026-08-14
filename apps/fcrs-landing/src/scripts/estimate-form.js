// Hero estimate-request form submit handler.
//
// Behavior contract (CLAUDE.md "API surface / data capture" +
// "Validation error surfacing" + "Success state"):
//   - novalidate stays on the form; native reportValidity() tooltips never ship.
//   - Client-side checks (validateEstimateForm) run first for responsiveness,
//     but the POST to /api/estimate-request is always made — the server
//     round-trip is authoritative and is never replaced by the client check.
//   - On 400 { ok:false, errors:{ field: message } } -> render each message
//     inline beneath its field, aria-describedby + aria-invalid="true".
//   - On 200 (incl. the duplicate-skip path, which is indistinguishable to
//     the client) -> navigate to /thank-you. The in-page #form-success panel
//     is retained as a fallback only, in case navigation is blocked.
//   - On network/500 failure -> show a generic top-of-form banner; do NOT
//     invent per-field errors for a failure that isn't field-shaped.
//   - Submit button is disabled while the request is in flight.
import { FIELD_DEFS, validateEstimateForm, isValid } from "../lib/estimate-form-schema.js";

// PUBLIC_API_URL is baked in at build time (see astro.config.mjs / .env).
// Falls back to the relative path, which the dev-server proxy forwards to
// the local backend — set PUBLIC_API_URL for any production build where
// frontend and backend are not served from the same origin.
const ENDPOINT = `${import.meta.env.PUBLIC_API_URL ?? ""}/api/estimate-request`;

// Where the browser is sent after a successful submit. astro.config.mjs uses
// build.format:"file", so the built artifact is literally /thank-you.html.
// Using the .html extension explicitly (rather than relying on a host's
// extensionless-URL rewrite, which isn't guaranteed on every static host)
// means this works unconditionally regardless of deploy target.
const SUCCESS_REDIRECT = "/thank-you.html";

export function initEstimateForm() {
  const form = document.getElementById("estimate-form");
  if (!form) return;

  const submitBtn = form.querySelector('button[type="submit"]');
  const formBody = document.getElementById("form-body");
  const formSuccess = document.getElementById("form-success");
  const successHeading = document.getElementById("form-success-heading");
  const genericError = document.getElementById("form-generic-error");

  const fieldEls = {};
  FIELD_DEFS.forEach((def) => {
    fieldEls[def.name] = form.querySelector(`[name="${def.name}"]`);
  });

  function clearFieldError(name) {
    const el = fieldEls[name];
    if (!el) return;
    el.removeAttribute("aria-invalid");
    const errorEl = document.getElementById(`f-${name}-error`);
    if (errorEl) {
      errorEl.textContent = "";
      errorEl.hidden = true;
    }
  }

  function setFieldError(name, message) {
    const el = fieldEls[name];
    if (!el) return;
    el.setAttribute("aria-invalid", "true");
    const errorEl = document.getElementById(`f-${name}-error`);
    if (errorEl) {
      errorEl.textContent = message;
      errorEl.hidden = false;
    }
  }

  function clearAllErrors() {
    FIELD_DEFS.forEach((def) => clearFieldError(def.name));
    if (genericError) {
      genericError.textContent = "";
      genericError.hidden = true;
    }
  }

  function showGenericError(message) {
    if (!genericError) return;
    genericError.textContent = message;
    genericError.hidden = false;
  }

  function readValues() {
    const values = {};
    FIELD_DEFS.forEach((def) => {
      const el = fieldEls[def.name];
      values[def.name] = el ? el.value : "";
    });
    return values;
  }

  // On 200 (including the indistinguishable duplicate-skip path) the browser
  // navigates to the dedicated thank-you page. The POST has already fully
  // resolved server-side at this point — the Sheets/Supabase row, the
  // dedupe check, and both emails are unaffected by the navigation.
  function goToThankYou() {
    // If navigation is blocked, fall back to the in-page success panel so the
    // user is never left staring at an unchanged form.
    window.setTimeout(() => {
      if (formBody) formBody.style.display = "none";
      if (formSuccess) {
        formSuccess.style.display = "block";
        if (successHeading) {
          successHeading.setAttribute("tabindex", "-1");
          successHeading.focus();
        }
      }
    }, 1200);
    window.location.assign(SUCCESS_REDIRECT);
  }

  let inFlight = false;

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (inFlight) return;

    clearAllErrors();
    const values = readValues();
    const clientErrors = validateEstimateForm(values);

    if (!isValid(clientErrors)) {
      Object.entries(clientErrors).forEach(([name, message]) =>
        setFieldError(name, message)
      );
      const firstInvalidName = Object.keys(clientErrors)[0];
      const firstInvalidEl = fieldEls[firstInvalidName];
      if (firstInvalidEl) firstInvalidEl.focus();
      // Still fall through to a real server round-trip is unnecessary here —
      // client-side already caught the problem, so no request is sent. The
      // server remains authoritative for anything the client check misses
      // (see below: server 400 responses are always rendered too).
      return;
    }

    inFlight = true;
    if (submitBtn) submitBtn.disabled = true;

    try {
      const res = await fetch(ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values),
      });

      if (res.status === 200) {
        const data = await res.json().catch(() => ({}));
        if (data && data.ok) {
          goToThankYou();
        } else {
          showGenericError(
            "Something went wrong. Please call us at the number above."
          );
        }
      } else if (res.status === 400) {
        const data = await res.json().catch(() => ({}));
        const errors = (data && data.errors) || {};
        Object.entries(errors).forEach(([name, message]) =>
          setFieldError(name, message)
        );
        const firstName = Object.keys(errors)[0];
        const firstEl = firstName ? fieldEls[firstName] : null;
        if (firstEl) firstEl.focus();
      } else {
        showGenericError(
          "Something went wrong. Please call us at the number above."
        );
      }
    } catch (err) {
      showGenericError(
        "Something went wrong. Please call us at the number above."
      );
    } finally {
      inFlight = false;
      if (submitBtn) submitBtn.disabled = false;
    }
  });
}
