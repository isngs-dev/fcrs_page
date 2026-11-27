/**
 * Every console route reads cookies, so it is dynamic: without a loading boundary a
 * click waited on the server with nothing on screen (the "laggy" feel, owner
 * 2026-11-27). This skeleton is prefetched with each <Link>, so navigation answers
 * at once and the page streams in behind it. The sidebar stays interactive.
 */
export default function Loading() {
  return (
    <main
      aria-busy="true"
      aria-label="Loading"
      className="flex flex-1 flex-col gap-5 px-4 py-8 sm:px-6 lg:px-8 lg:py-9"
    >
      <div className="flex items-end justify-between gap-4">
        <div className="flex flex-col gap-2">
          <div className="skeleton h-3 w-24" />
          <div className="skeleton h-7 w-56" />
        </div>
        <div className="skeleton h-9 w-32" />
      </div>
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="rounded-[12px] border border-border bg-card p-5 shadow-[var(--shadow-card)]">
            <div className="skeleton h-3 w-20" />
            <div className="skeleton mt-4 h-7 w-16" />
            <div className="skeleton mt-3 h-2.5 w-28" />
          </div>
        ))}
      </div>
      <div className="rounded-[12px] border border-border bg-card shadow-[var(--shadow-card)]">
        <div className="border-b border-border px-5 py-4">
          <div className="skeleton h-4 w-40" />
        </div>
        {[0, 1, 2, 3, 4, 5].map((i) => (
          <div key={i} className="flex items-center gap-4 border-t border-[var(--row-line)] px-5 py-4 first:border-t-0">
            <div className="skeleton size-8 rounded-full" />
            <div className="skeleton h-3 flex-1" />
            <div className="skeleton h-3 w-24" />
          </div>
        ))}
      </div>
    </main>
  );
}
