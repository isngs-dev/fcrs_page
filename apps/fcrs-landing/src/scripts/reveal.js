// Scroll-reveal, ported from the prototype's componentDidMount() IntersectionObserver.
// threshold/rootMargin/unobserve-after-fire match CLAUDE.md's motion inventory verbatim.
export function initReveal() {
  const targets = document.querySelectorAll("[data-reveal]");
  if (!targets.length) return;

  const reduceMotion = window.matchMedia(
    "(prefers-reduced-motion: reduce)"
  ).matches;

  if (reduceMotion || !("IntersectionObserver" in window)) {
    targets.forEach((el) => el.classList.add("is-revealed"));
    return;
  }

  const io = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-revealed");
          io.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.12, rootMargin: "0px 0px -8% 0px" }
  );

  targets.forEach((el) => io.observe(el));
}
