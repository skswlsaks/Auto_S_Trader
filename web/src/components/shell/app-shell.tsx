"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { CompareProvider, useCompare } from "@/lib/compare";
import { useIndex, useNewerBuild } from "@/lib/data";
import { Button, cx, Swatch, TipProvider } from "@/components/ui";

const NAV = [
  { href: "/", label: "개요" },
  { href: "/runs/", label: "모델 순위" },
  { href: "/compare/", label: "비교" },
  { href: "/studies/", label: "조건 실험" },
  { href: "/lab/", label: "조합 실험실" },
];

function ThemeToggle() {
  const [theme, setTheme] = useState<string | null>(null);
  useEffect(() => setTheme(document.documentElement.dataset.theme ?? "light"), []);
  return (
    <Button variant="ghost" size="sm" aria-label="화면 테마 바꾸기" title="화면 테마 바꾸기"
      onClick={() => { const t = theme === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = t; localStorage.setItem("arena-theme", t); setTheme(t); }}>
      {theme === "dark" ? "밝게" : "어둡게"}
    </Button>
  );
}

function Tray() {
  const cmp = useCompare(), path = usePathname();
  if (!cmp.list.length || path.startsWith("/compare")) return null;
  return (
    <div role="region" aria-label="비교 목록" className="fixed bottom-4 left-1/2 z-30 flex max-w-[calc(100vw-32px)] -translate-x-1/2 items-center gap-2.5 rounded-xl bg-ink py-2 pl-3.5 pr-2 text-panel shadow-[0_8px_28px_rgba(24,35,58,.28)]">
      <span className="whitespace-nowrap text-[13px] text-ink-3">비교 목록</span>
      <div className="flex gap-1.5 overflow-x-auto">
        {cmp.list.map(e => (
          <span key={e.id} className="inline-flex h-7 items-center gap-1.5 whitespace-nowrap rounded-full bg-white/10 pl-1.5 pr-1 text-xs dark:bg-black/25">
            <Swatch color={`var(--series-${e.slot + 1})`} n={e.slot + 1} />{e.id}
            <button className="h-5 w-5 rounded-full opacity-70 hover:bg-white/15 hover:opacity-100" aria-label={`${e.id} 비교에서 빼기`} onClick={() => cmp.toggle(e.id)}>✕</button>
          </span>
        ))}
      </div>
      <button className="h-7 flex-none rounded px-2.5 text-xs text-ink-3 hover:bg-white/10 hover:text-panel" onClick={cmp.clear}>비우기</button>
      <Link href={`/compare/?ids=${encodeURIComponent(cmp.list.map(e => e.id).join(","))}`}
        className="inline-flex h-7 flex-none items-center rounded bg-panel px-3 text-xs font-medium text-ink">비교 보기</Link>
    </div>
  );
}

function NewerBanner() {
  const { data } = useIndex();
  const newer = useNewerBuild(data?.generated_at);
  if (!newer) return null;
  return (
    <div role="status" className="border-b border-rule bg-accent-soft px-6 py-2 text-[13px] text-ink">
      새 결과가 올라왔습니다. <button className="font-semibold text-accent underline underline-offset-2" onClick={() => location.reload()}>새로고침</button>
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const path = usePathname();
  const [https, setHttps] = useState(false);
  useEffect(() => setHttps(location.protocol === "https:"), []);
  return (
    <TipProvider>
      <CompareProvider>
        <header className="sticky top-0 z-20 border-b border-rule bg-panel">
          <div className="mx-auto flex max-w-[1640px] flex-wrap items-center gap-x-6 gap-y-2 px-6 py-2.5">
            <Link href="/" className="text-base font-bold tracking-tight text-ink no-underline">Model Arena</Link>
            <nav aria-label="주 메뉴" className="flex flex-wrap gap-0.5">
              {NAV.map(n => {
                const on = n.href === "/" ? path === "/" : path.startsWith(n.href.replace(/\/$/, ""));
                return <Link key={n.href} href={n.href} aria-current={on ? "page" : undefined}
                  className={cx("rounded px-3 py-1.5 text-[13px] font-medium no-underline", on ? "bg-sunken text-ink" : "text-ink-2 hover:text-ink")}>{n.label}</Link>;
              })}
            </nav>
            <div className="flex-1" />
            <ThemeToggle />
            {https && <a href="/_auth/logout" className="rounded px-2.5 py-1 text-xs font-medium text-ink-2 no-underline hover:bg-sunken hover:text-ink">로그아웃</a>}
          </div>
          <NewerBanner />
        </header>
        <main className="mx-auto max-w-[1640px] px-6 pb-28 pt-5">{children}</main>
        <Tray />
      </CompareProvider>
    </TipProvider>
  );
}
