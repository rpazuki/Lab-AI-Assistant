import type { Metadata } from "next";

import { Sidebar } from "@/components/Sidebar";

import "./globals.css";

export const metadata: Metadata = {
  title: "Lab AI Assistant",
  description: "Internal research-evidence platform",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="flex min-h-screen">
        <Sidebar />
        <main className="min-w-0 flex-1 px-4 py-8 sm:px-8">
          <div className="mx-auto max-w-7xl">{children}</div>
        </main>
      </body>
    </html>
  );
}
