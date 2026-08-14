// Smooth anchor scroll with the 88px sticky-header offset, ported from the
// prototype's data-scroll click handler.
const HEADER_OFFSET = 88;

export function initSmoothScroll() {
  document.querySelectorAll("a[data-scroll]").forEach((a) => {
    a.addEventListener("click", (ev) => {
      const href = a.getAttribute("href");
      if (!href || href.charAt(0) !== "#") return;
      const target = document.querySelector(href);
      if (!target) return;
      ev.preventDefault();
      const top =
        target.getBoundingClientRect().top + window.scrollY - HEADER_OFFSET;
      const reduceMotion = window.matchMedia(
        "(prefers-reduced-motion: reduce)"
      ).matches;
      window.scrollTo({ top, behavior: reduceMotion ? "auto" : "smooth" });
    });
  });
}
