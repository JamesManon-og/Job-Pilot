import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "JobPilot",
  description: "Local AI job application automation",
};

const NAV = [
  { href: "/", label: "Overview" },
  { href: "/jobs", label: "Jobs" },
  { href: "/matches", label: "Matches" },
  { href: "/applications", label: "Applications" },
  { href: "/review", label: "Review" },
];

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full bg-zinc-950 font-sans text-zinc-100">
        <div className="flex min-h-screen">
          <aside className="w-52 shrink-0 border-r border-zinc-800 p-4">
            <div className="mb-8 flex items-center gap-2 px-2">
              <span className="text-xl">✈️</span>
              <span className="text-lg font-semibold tracking-tight">JobPilot</span>
            </div>
            <nav className="space-y-1">
              {NAV.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className="block rounded-lg px-3 py-2 text-sm text-zinc-400 transition hover:bg-zinc-900 hover:text-zinc-100"
                >
                  {item.label}
                </Link>
              ))}
            </nav>
            <p className="mt-8 px-3 text-xs text-zinc-600">
              100% local — nothing leaves this machine.
            </p>
          </aside>
          <main className="flex-1 overflow-x-hidden p-8">{children}</main>
        </div>
      </body>
    </html>
  );
}
