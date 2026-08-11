import type { Metadata } from "next";
import { Archivo } from "next/font/google";
import "./globals.css";

/**
 * SR-15 D2: Archivo, self-hosted at build time via next/font/google -- no
 * runtime request to fonts.googleapis.com/fonts.gstatic.com and no CLS.
 * `variable` feeds --font-app-sans (globals.css's `@theme inline` maps
 * --font-sans to --font-app-sans, and `html { @apply font-sans }` applies
 * it), matching Sidebar.dc.html's declared stack verbatim: Archivo first,
 * system-ui/sans-serif fallback (also declared as globals.css's :root
 * fallback value for --font-app-sans before this variable overrides it).
 */
const archivo = Archivo({
  subsets: ["latin"],
  variable: "--font-app-sans",
  fallback: ["system-ui", "sans-serif"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "Admin Console",
  description: "Chatbot platform admin console.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={`h-full antialiased ${archivo.variable}`}>
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
