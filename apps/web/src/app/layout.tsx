import type { Metadata } from "next";
import Link from "next/link";

import "./globals.css";

export const metadata: Metadata = {
  title: "Lab AI Assistant",
  description: "Internal research-evidence platform",
  robots: { index: false, follow: false },
};

const NAV = [
  { href: "/ask", label: "Ask" },
  { href: "/collections", label: "Collections" },
  { href: "/review", label: "Review" },
  { href: "/releases", label: "Releases" },
  { href: "/analytics", label: "Analytics" },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <header className="border-b border-neutral-200 bg-white">
          <div className="mx-auto flex max-w-6xl items-center gap-6 px-4 py-3">
            <Link href="/ask" className="font-semibold">
              Lab AI Assistant
            </Link>
            <nav className="flex gap-4 text-sm">
              {NAV.map((item) => (
                <Link key={item.href} href={item.href} className="hover:underline">
                  {item.label}
                </Link>
              ))}
            </nav>
            <span className="ml-auto text-xs text-neutral-500">internal use only</span>
          </div>
        </header>
        <main className="mx-auto max-w-6xl px-4 py-6">{children}</main>
      </body>
    </html>
  );
}
