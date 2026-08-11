import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { OffRampDialog } from "@/components/admin/off-ramp-dialog";

describe("OffRampDialog -- D4 (off-ramp is an explicit confirmed action, never a bare drag)", () => {
  it("renders nothing when open=false", () => {
    const html = renderToStaticMarkup(
      <OffRampDialog
        open={false}
        offRampLabel="Closed Lost"
        requireReason
        onCancel={() => {}}
        onConfirm={async () => ({ status: "ok", stage: "closed_lost" })}
        onSettled={() => {}}
      />
    );
    expect(html).toBe("");
  });

  it("requireReason=true (deals) renders a required reason textarea", () => {
    const html = renderToStaticMarkup(
      <OffRampDialog
        open
        offRampLabel="Closed Lost"
        requireReason
        onCancel={() => {}}
        onConfirm={async () => ({ status: "ok", stage: "closed_lost" })}
        onSettled={() => {}}
      />
    );
    expect(html).toMatch(/<textarea/);
    expect(html).toMatch(/Reason \(required\)/);
  });

  it("the submit button starts disabled when requireReason=true and no reason has been typed yet", () => {
    const html = renderToStaticMarkup(
      <OffRampDialog
        open
        offRampLabel="Closed Lost"
        requireReason
        onCancel={() => {}}
        onConfirm={async () => ({ status: "ok", stage: "closed_lost" })}
        onSettled={() => {}}
      />
    );
    const idx = html.indexOf("Confirm move to");
    const tagStart = html.lastIndexOf("<button", idx);
    const tagEnd = html.indexOf(">", tagStart);
    const tag = html.slice(tagStart, tagEnd + 1);
    expect(tag).toMatch(/disabled=""/);
  });

  it("requireReason=false (leads) renders NO reason field at all", () => {
    const html = renderToStaticMarkup(
      <OffRampDialog
        open
        offRampLabel="Disqualified"
        requireReason={false}
        onCancel={() => {}}
        onConfirm={async () => ({ status: "ok", stage: "disqualified" })}
        onSettled={() => {}}
      />
    );
    expect(html).not.toMatch(/<textarea/);
    expect(html).not.toMatch(/Reason \(required\)/);
  });

  it("warns the move is one-way/irreversible regardless of reason requirement", () => {
    const html = renderToStaticMarkup(
      <OffRampDialog
        open
        offRampLabel="Disqualified"
        requireReason={false}
        onCancel={() => {}}
        onConfirm={async () => ({ status: "ok", stage: "disqualified" })}
        onSettled={() => {}}
      />
    );
    expect(html).toMatch(/one-way|cannot be reopened/i);
  });

  it("renders as an alertdialog with aria-modal for screen readers", () => {
    const html = renderToStaticMarkup(
      <OffRampDialog
        open
        offRampLabel="Closed Lost"
        requireReason
        onCancel={() => {}}
        onConfirm={async () => ({ status: "ok", stage: "closed_lost" })}
        onSettled={() => {}}
      />
    );
    expect(html).toMatch(/role="alertdialog"/);
    expect(html).toMatch(/aria-modal="true"/);
  });

  it("the offRampLabel appears in the confirm button's own text (deal vs lead off-ramp are visibly distinct)", () => {
    const html = renderToStaticMarkup(
      <OffRampDialog
        open
        offRampLabel="Closed Lost"
        requireReason
        onCancel={() => {}}
        onConfirm={async () => ({ status: "ok", stage: "closed_lost" })}
        onSettled={() => {}}
      />
    );
    expect(html).toMatch(/Confirm move to Closed Lost/);
  });

  it("onCancel/onConfirm/onSettled are accepted as props without being invoked on a static render", () => {
    const onCancel = vi.fn();
    const onConfirm = vi.fn(async () => ({ status: "ok" as const, stage: "closed_lost" }));
    const onSettled = vi.fn();

    renderToStaticMarkup(
      <OffRampDialog
        open
        offRampLabel="Closed Lost"
        requireReason
        onCancel={onCancel}
        onConfirm={onConfirm}
        onSettled={onSettled}
      />
    );

    expect(onCancel).not.toHaveBeenCalled();
    expect(onConfirm).not.toHaveBeenCalled();
    expect(onSettled).not.toHaveBeenCalled();
  });
});
