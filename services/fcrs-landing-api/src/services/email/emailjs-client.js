import { createRequire } from "node:module";

// EmailJS replaces the Gmail API. Called SERVER-SIDE from the Supabase
// Database Webhook route — never from the browser — so EmailJS's
// "Allow EmailJS API for non-browser applications" setting must be ENABLED
// (Account -> Security) and the private key is supplied as accessToken.
//
// @emailjs/nodejs's published ESM ("mjs") build uses extensionless relative
// imports, which fail Node's strict ESM resolution (works fine bundled, but
// not run directly). Loading the package's CJS build via createRequire is
// the documented, spec-shaped Node workaround — a runtime interop detail,
// not a change to the EmailJS/Supabase architecture.
const require = createRequire(import.meta.url);
const emailjs = require("@emailjs/nodejs");

export function createEmailJsClient(env = process.env) {
  const serviceId = env.EMAILJS_SERVICE_ID;
  const publicKey = env.EMAILJS_PUBLIC_KEY;
  const privateKey = env.EMAILJS_PRIVATE_KEY;

  if (!serviceId || !publicKey || !privateKey) {
    return null;
  }

  return {
    async send({ templateId, params }) {
      if (!templateId) throw new Error("Missing EmailJS template id.");
      await emailjs.send(serviceId, templateId, params, { publicKey, privateKey });
    },
  };
}
