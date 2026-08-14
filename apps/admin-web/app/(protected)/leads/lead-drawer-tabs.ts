/**
 * `LeadDrawer`'s tab set, shared between the server component
 * (`lead-drawer-container.tsx`) and the client component (`lead-drawer.tsx`)
 * that render around it.
 *
 * This must NOT live inside `lead-drawer.tsx`: that file is `"use client"`,
 * and every export of a `"use client"` module becomes an opaque client
 * reference when imported from server code -- fine for the component itself
 * (used as JSX, e.g. `<LeadDrawer />`), but a plain value import like `TABS`
 * resolves to a reference placeholder, not the real array, so
 * `TABS.includes(...)` throws `TABS.includes is not a function` at runtime
 * in the server component. Keeping these in a plain module with no
 * directive makes them safely importable from both sides of the boundary.
 */
export const TABS = ["timeline", "details", "notes"] as const;
export type Tab = (typeof TABS)[number];

export function isTab(value: string | undefined): value is Tab {
  return !!value && (TABS as readonly string[]).includes(value);
}
