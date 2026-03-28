import Link from "next/link";
import { PropsWithChildren } from "react";

import { StatusSummary } from "@/components/status-summary";
import { cn } from "@/lib/utils";

const links = [
  { href: "/", label: "Overview" },
  { href: "/profile", label: "Profile" },
  { href: "/preferences", label: "Preferences" },
  { href: "/schedule", label: "Schedule" },
  { href: "/history", label: "History" },
  { href: "/ai", label: "AI Settings" },
];

type PanelShellProps = PropsWithChildren<{
  active: string;
  title: string;
  description: string;
}>;

export function PanelShell({
  active,
  title,
  description,
  children,
}: PanelShellProps): React.JSX.Element {
  return (
    <div className="mx-auto flex min-h-screen max-w-7xl flex-col gap-8 px-5 py-8 lg:px-8">
      <section className="grid gap-5 lg:grid-cols-[1.2fr_0.8fr]">
        <div className="rounded-[28px] border border-border bg-card/90 p-8 shadow-panel">
          <span className="inline-flex rounded-full bg-secondary px-3 py-1 text-xs font-semibold uppercase tracking-[0.2em] text-[#7a3b1e]">
            MVP Control Room
          </span>
          <h1 className="mt-4 font-display text-5xl leading-none text-foreground md:text-7xl">
            Lightweight panel, backend-first workflow.
          </h1>
          <p className="mt-5 max-w-2xl text-base leading-7 text-muted-foreground md:text-lg">
            Configure the profile, search filters, schedule, AI options and recruiter toggle from
            a simple Next.js panel backed by the FastAPI API.
          </p>
        </div>
        <StatusSummary />
      </section>

      <section className="grid gap-6 lg:grid-cols-[260px_minmax(0,1fr)]">
        <aside className="h-fit rounded-[28px] border border-border bg-card/90 p-5 shadow-panel">
          <h2 className="font-display text-2xl text-foreground">Panel</h2>
          <p className="mt-2 text-sm text-muted-foreground">Choose the section you want to update.</p>
          <nav className="mt-6 flex flex-col gap-2">
            {links.map((link) => (
              <Link
                key={link.href}
                href={link.href}
                className={cn(
                  "rounded-2xl border border-transparent bg-white/70 px-4 py-3 text-sm font-medium transition hover:-translate-y-0.5 hover:border-[#d4b298] hover:bg-white",
                  active === link.href && "border-[#d4b298] bg-white text-[#7a3b1e]",
                )}
              >
                {link.label}
              </Link>
            ))}
          </nav>
        </aside>

        <main className="rounded-[28px] border border-border bg-card/90 p-7 shadow-panel md:p-8">
          <header className="mb-8 space-y-3">
            <h2 className="font-display text-4xl text-foreground">{title}</h2>
            <p className="max-w-3xl text-base leading-7 text-muted-foreground">{description}</p>
          </header>
          {children}
        </main>
      </section>
    </div>
  );
}
