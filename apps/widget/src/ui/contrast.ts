/**
 * Readable colors for a tenant's accent (WCAG 2 relative luminance).
 *
 * A light accent (e.g. cream) made the white text on accent surfaces --
 * launcher, send button, visitor bubble -- and the accent-colored text on
 * light surfaces ("Schedule a Call", suggestion icons) unreadable.
 * Inputs are `#rrggbb` (validated upstream by the session schema).
 */

const INK = "#0f172a"; // = --cw-ink
const WHITE = "#ffffff";

function channels(hex: string): [number, number, number] {
  return [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)) as [number, number, number];
}

function luminance(hex: string): number {
  const [r, g, b] = channels(hex).map((c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  }) as [number, number, number];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function contrastRatio(a: string, b: string): number {
  const la = luminance(a);
  const lb = luminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/** Text color for content sitting ON the accent: whichever of ink/white reads better. */
export function onAccentColor(accent: string): string {
  return contrastRatio(accent, WHITE) >= contrastRatio(accent, INK) ? WHITE : INK;
}

/** The accent as text/icon on a white surface: the accent itself if it already
 * reaches 4.5:1, else the same hue darkened in 5% steps until it does. */
export function accentTextColor(accent: string): string {
  const base = channels(accent);
  for (let keep = 1; keep > 0; keep -= 0.05) {
    const hex = `#${base.map((c) => Math.round(c * keep).toString(16).padStart(2, "0")).join("")}`;
    if (contrastRatio(hex, WHITE) >= 4.5) return hex === accent.toLowerCase() ? accent : hex;
  }
  return INK;
}
