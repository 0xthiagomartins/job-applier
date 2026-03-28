import type { Metadata } from "next";

import "@/app/globals.css";

export const metadata: Metadata = {
  title: "Job Applier Panel",
  description: "Control panel for Job Applier MVP configuration.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>): React.JSX.Element {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
