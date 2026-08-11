/**
 * Shared `.set-row` primitive (SR-27 slice 0), the label+description /
 * field row recipe used by both the Settings and Bot-settings shells
 * (`Console.dc.html:880-883`):
 *
 *   .set-row{display:grid;grid-template-columns:210px 1fr;gap:20px;
 *            padding:16px 0;border-bottom:1px solid var(--row-line)}
 *   .set-row:last-child{border-bottom:none}
 *   .set-lbl{font-size:13px;font-weight:600}
 *   .set-desc{font-size:12px;color:var(--muted);margin-top:3px;line-height:1.45}
 *   .field{width:100%;border:1px solid var(--line);border-radius:10px;
 *          padding:10px 12px;font-size:13px;background:#fff;color:var(--ink)}
 *
 * The `.field` box styling is intentionally NOT baked into this primitive --
 * callers slot in whatever control they need (text input, textarea, select,
 * a disabled note, a stepper) as `children`, since field TYPE varies per row
 * while the row GEOMETRY does not. `isLast` suppresses the bottom border to
 * match `.set-row:last-child`.
 */
export function SetRow({
  label,
  description,
  htmlFor,
  isLast = false,
  children,
}: {
  label: string;
  description?: string;
  htmlFor?: string;
  isLast?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div
      className={`grid grid-cols-[210px_1fr] gap-5 py-4 ${isLast ? "" : "border-b border-[var(--row-line)]"}`}
    >
      <div>
        <label htmlFor={htmlFor} className="text-[13px] font-semibold text-foreground">
          {label}
        </label>
        {description ? (
          <p className="mt-[3px] text-xs leading-[1.45] text-muted-foreground">{description}</p>
        ) : null}
      </div>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

/** Shared `.field` input styling -- pass via `className` to a real `<input>`/
 * `<textarea>`/`<select>` so every editable row shares one recipe. */
export const SET_ROW_FIELD_CLASS =
  "w-full rounded-[10px] border border-[var(--line)] bg-white px-3 py-2.5 text-[13px] text-foreground";
