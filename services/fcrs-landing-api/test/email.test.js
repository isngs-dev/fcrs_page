import { describe, it, expect, vi } from "vitest";
import { createEmailService } from "../src/services/email/index.js";
import { buildInternalNotificationEmail, buildConfirmationEmail } from "../src/services/email/templates.js";

const submission = {
  name: "Jane Smith",
  phone: "(989) 843-4628",
  email: "jane@example.com",
  service: "Residential Roofing",
  state: "Alabama",
  zip: "35203",
  date: "2026-08-01T10:00",
  notes: "Leak near the chimney.",
};

const baseEnv = {
  INTERNAL_NOTIFICATION_EMAIL: "leads@example.com",
  EMAILJS_SERVICE_ID: "service-id",
  EMAILJS_PUBLIC_KEY: "public-key",
  EMAILJS_PRIVATE_KEY: "private-key",
  EMAILJS_TEMPLATE_ID_CONFIRMATION: "confirmation-template",
  EMAILJS_TEMPLATE_ID_INTERNAL: "internal-template",
};

describe("email templates", () => {
  it("confirmation email is addressed to the submitter and uses the confirmation template's variable names", () => {
    const email = buildConfirmationEmail(submission, { templateId: "confirmation-template" });
    expect(email.templateId).toBe("confirmation-template");
    expect(email.params.to_email).toBe("jane@example.com");
    expect(email.params["First Name"]).toBe("Jane");
    expect(email.params["Company Name"]).toBe("First Class Roofing & Solar");
    expect(email.params["Phone Number"]).toBe("(989) 843-4628");
  });

  it("internal notification includes every captured field, labelled to match the internal template", () => {
    const email = buildInternalNotificationEmail(submission, {
      to: "leads@example.com",
      timestamp: "2026-07-25T12:00:00.000Z",
      templateId: "internal-template",
    });

    expect(email.templateId).toBe("internal-template");
    expect(email.params.to_email).toBe("leads@example.com");
    expect(email.params["First Name"]).toBe("Jane");
    expect(email.params["Last Name"]).toBe("Smith");
    expect(email.params["Phone Number"]).toBe(submission.phone);
    expect(email.params["Email Address"]).toBe(submission.email);
    expect(email.params["ZIP Code"]).toBe(submission.zip);
    expect(email.params["Service Requested"]).toBe(submission.service);
    expect(email.params.Message).toContain(submission.state);
    expect(email.params.Message).toContain(submission.notes);
    expect(email.params["Submission Date & Time"]).toBe("2026-07-25T12:00:00.000Z");
  });

  it("internal notification shows 'none' for notes when absent", () => {
    const { notes, ...rest } = submission;
    const email = buildInternalNotificationEmail(rest, {
      to: "leads@example.com",
      timestamp: "2026-07-25T12:00:00.000Z",
      templateId: "internal-template",
    });
    expect(email.params.Message).toContain("none");
  });
});

describe("createEmailService", () => {
  it("sends both messages with correct template ids and recipients on a valid submission", async () => {
    const send = vi.fn().mockResolvedValue(undefined);
    const emailService = createEmailService({ env: baseEnv, emailClient: { send } });

    await emailService.sendLeadEmails(submission, { timestamp: "2026-07-25T12:00:00.000Z" });

    expect(send).toHaveBeenCalledTimes(2);
    const messages = send.mock.calls.map((call) => call[0]);
    const confirmation = messages.find((m) => m.templateId === "confirmation-template");
    const internal = messages.find((m) => m.templateId === "internal-template");
    expect(confirmation.params.to_email).toBe("jane@example.com");
    expect(internal.params.to_email).toBe("leads@example.com");
  });

  it("logs a warning and does not throw when a send fails, and the other send still attempts", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const send = vi.fn().mockRejectedValue(new Error("emailjs down"));
    const emailService = createEmailService({ env: baseEnv, emailClient: { send } });

    await expect(
      emailService.sendLeadEmails(submission, { timestamp: "2026-07-25T12:00:00.000Z" })
    ).resolves.toBeUndefined();

    expect(send).toHaveBeenCalledTimes(2);
    expect(warnSpy).toHaveBeenCalled();
    // Never log secrets/recipients/payload.
    const loggedText = warnSpy.mock.calls.map((c) => c.join(" ")).join(" ");
    expect(loggedText).not.toContain("jane@example.com");
    expect(loggedText).not.toContain("leads@example.com");

    warnSpy.mockRestore();
  });

  it("warns once per missing config and skips sending without throwing", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const emailService = createEmailService({ env: {}, emailClient: null });

    await expect(
      emailService.sendLeadEmails(submission, { timestamp: "2026-07-25T12:00:00.000Z" })
    ).resolves.toBeUndefined();

    expect(warnSpy).toHaveBeenCalled();

    warnSpy.mockRestore();
  });

  it("warns and skips (never throws) when a template id is missing but other config is present", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const send = vi.fn().mockResolvedValue(undefined);
    const envMissingTemplateIds = {
      INTERNAL_NOTIFICATION_EMAIL: "leads@example.com",
      EMAILJS_SERVICE_ID: "service-id",
      EMAILJS_PUBLIC_KEY: "public-key",
      EMAILJS_PRIVATE_KEY: "private-key",
      // EMAILJS_TEMPLATE_ID_CONFIRMATION and EMAILJS_TEMPLATE_ID_INTERNAL omitted
    };
    const emailService = createEmailService({ env: envMissingTemplateIds, emailClient: { send } });

    await expect(
      emailService.sendLeadEmails(submission, { timestamp: "2026-07-25T12:00:00.000Z" })
    ).resolves.toBeUndefined();

    expect(send).not.toHaveBeenCalled();
    expect(warnSpy).toHaveBeenCalled();
    const loggedText = warnSpy.mock.calls.map((c) => c.join(" ")).join(" ");
    expect(loggedText).toMatch(/EMAILJS_TEMPLATE_ID_CONFIRMATION/);
    expect(loggedText).toMatch(/EMAILJS_TEMPLATE_ID_INTERNAL/);

    warnSpy.mockRestore();
  });
});
