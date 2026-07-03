import { Link, useLocation } from "wouter";
import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { href: "/", label: "Overview" },
  { href: "/vouches", label: "Vouches" },
  { href: "/strikes", label: "Strikes" },
  { href: "/builder", label: "Builder" },
  { href: "/activity", label: "Activity Log" },
];

export function Layout({ children }: { children: React.ReactNode }) {
  const [location] = useLocation();

  return (
    <div className="min-h-screen flex bg-[#0f1117] text-gray-100">
      {/* Sidebar */}
      <aside className="w-56 flex-shrink-0 bg-[#161b22] border-r border-white/10 flex flex-col">
        <div className="px-5 py-5 border-b border-white/10">
          <div className="text-xs font-semibold text-gray-400 uppercase tracking-widest mb-1">
            Bot Dashboard
          </div>
          <div className="text-[11px] text-gray-600">Staff Management</div>
        </div>
        <nav className="flex-1 py-4 px-2 space-y-0.5">
          {NAV_ITEMS.map(({ href, label }) => {
            const active = href === "/" ? location === "/" : location.startsWith(href);
            return (
              <Link key={href} href={href}>
                <span
                  className={cn(
                    "flex items-center px-3 py-2 rounded-md text-sm transition-colors cursor-pointer",
                    active
                      ? "bg-indigo-600 text-white font-medium"
                      : "text-gray-400 hover:text-gray-100 hover:bg-white/5"
                  )}
                >
                  {label}
                </span>
              </Link>
            );
          })}
        </nav>
        <div className="px-4 py-3 border-t border-white/10">
          <div className="text-[10px] text-gray-600">Read-only • SQLite data</div>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-auto">
        {children}
      </main>
    </div>
  );
}

export function PageHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="mb-6">
      <h1 className="text-xl font-semibold text-gray-100">{title}</h1>
      {subtitle && <p className="mt-1 text-sm text-gray-400">{subtitle}</p>}
    </div>
  );
}

export function StatCard({
  label,
  value,
  sub,
}: {
  label: string;
  value: number | string;
  sub?: string;
}) {
  return (
    <div className="bg-[#161b22] border border-white/10 rounded-lg px-5 py-4">
      <div className="text-xs text-gray-400 uppercase tracking-wider mb-2">{label}</div>
      <div className="text-3xl font-bold text-gray-100 tabular-nums">{value}</div>
      {sub && <div className="mt-1 text-[11px] text-gray-500">{sub}</div>}
    </div>
  );
}

export function TableWrap({ children }: { children: React.ReactNode }) {
  return (
    <div className="bg-[#161b22] border border-white/10 rounded-lg overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">{children}</table>
      </div>
    </div>
  );
}

export function Th({ children }: { children: React.ReactNode }) {
  return (
    <th className="px-4 py-3 text-left text-[11px] font-semibold text-gray-400 uppercase tracking-wider border-b border-white/10">
      {children}
    </th>
  );
}

export function Td({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <td className={cn("px-4 py-3 border-b border-white/5 text-gray-300", className)}>
      {children}
    </td>
  );
}

export function LoadingRows({ cols }: { cols: number }) {
  return (
    <>
      {[...Array(5)].map((_, i) => (
        <tr key={i}>
          {[...Array(cols)].map((__, j) => (
            <td key={j} className="px-4 py-3 border-b border-white/5">
              <div className="h-3 bg-white/5 rounded animate-pulse" />
            </td>
          ))}
        </tr>
      ))}
    </>
  );
}

export function EmptyRow({ cols, message }: { cols: number; message: string }) {
  return (
    <tr>
      <td colSpan={cols} className="px-4 py-10 text-center text-sm text-gray-500">
        {message}
      </td>
    </tr>
  );
}

export function Badge({
  children,
  variant,
}: {
  children: React.ReactNode;
  variant: "green" | "yellow" | "red" | "gray" | "blue";
}) {
  const cls = {
    green: "bg-emerald-500/15 text-emerald-400 ring-emerald-500/30",
    yellow: "bg-yellow-500/15 text-yellow-400 ring-yellow-500/30",
    red: "bg-red-500/15 text-red-400 ring-red-500/30",
    gray: "bg-gray-500/15 text-gray-400 ring-gray-500/30",
    blue: "bg-blue-500/15 text-blue-400 ring-blue-500/30",
  }[variant];
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset",
        cls
      )}
    >
      {children}
    </span>
  );
}
