import type { Metadata } from "next";
import { IBM_Plex_Sans } from "next/font/google";
import "./globals.css";

/**
 * Pine & Clay (owner, 2026-11-27): IBM Plex Sans, self-hosted at build time via
 * next/font/google (no runtime font request, no layout shift). `variable` feeds
 * --font-app-sans, which globals.css maps to --font-sans.
 */
const plexSans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-app-sans",
  fallback: ["system-ui", "sans-serif"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "iSN Admin Console",
  description: "Chatbot platform admin console.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={`h-full antialiased ${plexSans.variable}`}>
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
