/**
 * Pure helpers for the qualification settings card (plan 1.2): answer choices are edited
 * as one "label = points" line each; a new question gets a stable key from its wording.
 */
export type Choice = { label: string; points: number };
export type Parsed = { ok: true; choices: Choice[] } | { ok: false; error: string };

export function parseChoices(text: string): Parsed {
  const choices: Choice[] = [];
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const at = line.lastIndexOf("=");
    if (at < 0) return { ok: false, error: `Line "${line}" needs "= points", e.g. "${line} = 100".` };
    const label = line.slice(0, at).trim();
    const pointsText = line.slice(at + 1).trim();
    const points = Number(pointsText);
    if (!label) return { ok: false, error: `Line "${line}" needs an answer before the "=".` };
    if (!/^\d+$/.test(pointsText) || points > 100)
      return { ok: false, error: `Points for "${label}" must be a whole number from 0 to 100.` };
    if (choices.some((c) => c.label.toLowerCase() === label.toLowerCase()))
      return { ok: false, error: `The choice "${label}" is listed twice.` };
    choices.push({ label, points });
  }
  if (choices.length < 2) return { ok: false, error: "Give at least 2 answer choices." };
  return { ok: true, choices };
}

export function choicesToText(choices: Choice[]): string {
  return choices.map((c) => `${c.label} = ${c.points}`).join("\n");
}

/** Matches the backend's key rule: ^[a-z][a-z0-9_]{0,39}$, unique among ``taken``. */
export function newKey(ask: string, taken: string[]): string {
  let base = ask
    .toLowerCase()
    .replace(/['’]/g, "")
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  if (!base) base = "question";
  if (/^\d/.test(base)) base = `q_${base}`;
  base = base.slice(0, 36).replace(/_+$/, "");
  let key = base;
  for (let n = 2; taken.includes(key); n += 1) key = `${base}_${n}`;
  return key;
}
