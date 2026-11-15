"use client";
/* Model Arena UI primitives: thin wrappers over Radix with the Arena design tokens.
   Keep them small and composable; pages assemble these, never style raw Radix directly. */
import { clsx } from "clsx";
import { Dialog, Popover, Select, Tabs as RTabs, Tooltip } from "radix-ui";
import { useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import type { MetricDef } from "@/lib/metric-defs";

export const cx = clsx;

// ---------- buttons ----------
type Variant = "default" | "primary" | "ghost";
export function Button({ variant = "default", size = "md", className, ...p }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: "sm" | "md" }) {
  return (
    <button
      {...p}
      className={cx(
        "inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded font-medium transition-colors disabled:opacity-50",
        size === "sm" ? "h-7 px-2.5 text-xs" : "h-8 px-3 text-[13px]",
        variant === "primary" && "bg-accent text-white hover:opacity-90 dark:text-paper",
        variant === "default" && "border border-rule bg-panel text-ink hover:border-ink-3",
        variant === "ghost" && "text-ink-2 hover:bg-sunken hover:text-ink",
        className,
      )}
    />
  );
}

/** Mutually exclusive options (e.g. evaluation split). */
export function Segmented<T extends string>({ value, options, onChange, label }: { value: T; options: { value: T; label: ReactNode }[]; onChange: (v: T) => void; label: string }) {
  return (
    <div role="group" aria-label={label} className="inline-flex rounded-md border border-rule bg-sunken p-0.5">
      {options.map(o => (
        <button key={o.value} aria-pressed={o.value === value} onClick={() => onChange(o.value)}
          className={cx("h-7 rounded px-3 text-[13px] font-medium", o.value === value ? "bg-panel text-ink shadow-[0_0_0_1px_var(--rule)]" : "text-ink-2 hover:text-ink")}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** Independent on/off filter chip. */
export function Chip({ on, onClick, children, title }: { on: boolean; onClick: () => void; children: ReactNode; title?: string }) {
  return (
    <button aria-pressed={on} onClick={onClick} title={title}
      className={cx("inline-flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-[13px]",
        on ? "border-ink-3 bg-sunken text-ink" : "border-rule bg-panel text-ink-3 line-through decoration-ink-3/60 hover:text-ink-2")}>
      <span className={cx("h-2 w-2 rounded-full border", on ? "border-ink bg-ink" : "border-ink-3")} />
      {children}
    </button>
  );
}

// ---------- labels ----------
export function Tag({ children, tone = "neutral" }: { children: ReactNode; tone?: "neutral" | "accent" }) {
  return <span className={cx("inline-block whitespace-nowrap rounded px-1.5 text-xs leading-[18px]", tone === "accent" ? "bg-accent-soft text-accent" : "bg-sunken text-ink-2")}>{children}</span>;
}
export type Tone = "pass" | "warn" | "fail" | "none";
export function Badge({ tone, children }: { tone: Tone; children: ReactNode }) {
  const cls = { pass: "bg-pass-soft text-pass", warn: "bg-warn-soft text-warn", fail: "bg-fail-soft text-fail", none: "bg-sunken text-ink-3" }[tone];
  return <span className={cx("inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 text-xs font-medium leading-5", cls)}>{children}</span>;
}
export function Swatch({ color, n }: { color: string; n: number }) {
  return <span className="inline-grid h-[18px] w-[18px] flex-none place-items-center rounded-full text-[11px] font-bold text-white" style={{ background: color }}>{n}</span>;
}

// ---------- tooltip ----------
export function Tip({ content, children, side = "top" }: { content: ReactNode; children: ReactNode; side?: "top" | "bottom" | "left" | "right" }) {
  return (
    <Tooltip.Root delayDuration={150}>
      <Tooltip.Trigger asChild>{children}</Tooltip.Trigger>
      <Tooltip.Portal><Tooltip.Content side={side} sideOffset={6} className="tooltip-content">{content}</Tooltip.Content></Tooltip.Portal>
    </Tooltip.Root>
  );
}
export const TipProvider = Tooltip.Provider;

/** "?" icon that opens an explanation on click/tap (works on touch, unlike a hover tooltip). */
export function HelpTip({ title, children, className }: { title: string; children: ReactNode; className?: string }) {
  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <button type="button" aria-label={`${title} 설명`}
          className={cx("inline-grid h-4 w-4 flex-none cursor-help place-items-center rounded-full border border-ink-3 text-[10px] font-semibold leading-none text-ink-3",
            "hover:border-ink-2 hover:text-ink-2 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent data-[state=open]:border-accent data-[state=open]:text-accent", className)}>?</button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content side="bottom" align="start" sideOffset={6} collisionPadding={12} className="tooltip-content help-content">
          <b className="mb-1 block">{title}</b>
          <div className="whitespace-pre-line">{children}</div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

/** Metric name with its definition on hover/focus. */
export function MetricLabel({ def, className }: { def: MetricDef; className?: string }) {
  return (
    <Tip content={<><b className="mb-1 block">{def.label}</b>{def.help}{def.better !== 0 && <span className="mt-1 block text-ink-3">{def.better > 0 ? (def.ref === 0 && def.key === "max_drawdown_pp" ? "0에 가까울수록 좋음" : "높을수록 좋음") : "낮을수록 좋음"}</span>}</>}>
      <span tabIndex={0} className={cx("cursor-help underline decoration-dotted decoration-ink-3 underline-offset-[3px]", className)}>{def.label}</span>
    </Tip>
  );
}

// ---------- select ----------
export function SelectBox({ value, onChange, options, label, className }: { value: string; onChange: (v: string) => void; options: { value: string; label: string }[]; label: string; className?: string }) {
  return (
    <Select.Root value={value} onValueChange={onChange}>
      <Select.Trigger aria-label={label} className={cx("inline-flex h-8 items-center justify-between gap-2 rounded border border-rule bg-panel px-2.5 text-[13px] text-ink hover:border-ink-3", className)}>
        <Select.Value />
        <Select.Icon className="text-ink-3">▾</Select.Icon>
      </Select.Trigger>
      <Select.Portal>
        <Select.Content position="popper" sideOffset={4} className="select-content min-w-[var(--radix-select-trigger-width)]">
          <Select.Viewport className="p-1">
            {options.map(o => (
              <Select.Item key={o.value} value={o.value} className="select-item">
                <Select.ItemText>{o.label}</Select.ItemText>
                <Select.ItemIndicator className="absolute right-2">✓</Select.ItemIndicator>
              </Select.Item>
            ))}
          </Select.Viewport>
        </Select.Content>
      </Select.Portal>
    </Select.Root>
  );
}

// ---------- tabs ----------
export function Tabs({ value, onChange, tabs, children }: { value: string; onChange: (v: string) => void; tabs: { value: string; label: ReactNode }[]; children: ReactNode }) {
  return (
    <RTabs.Root value={value} onValueChange={onChange}>
      <RTabs.List className="flex gap-0.5 border-b border-rule px-3">
        {tabs.map(t => (
          <RTabs.Trigger key={t.value} value={t.value}
            className="-mb-px border-b-2 border-transparent px-2.5 py-2 text-[13px] font-medium text-ink-2 hover:text-ink data-[state=active]:border-accent data-[state=active]:text-ink">
            {t.label}
          </RTabs.Trigger>
        ))}
      </RTabs.List>
      {children}
    </RTabs.Root>
  );
}
export const TabPanel = ({ value, children }: { value: string; children: ReactNode }) => (
  <RTabs.Content value={value} className="px-4 pb-5 pt-4 outline-none">{children}</RTabs.Content>
);

// ---------- sheet (side panel) ----------
export function Sheet({ open, onOpenChange, title, children, actions }: { open: boolean; onOpenChange: (o: boolean) => void; title: ReactNode; children: ReactNode; actions?: ReactNode }) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="sheet-overlay" />
        <Dialog.Content className="sheet-content" aria-describedby={undefined}>
          <div className="sticky top-0 z-10 flex items-start gap-3 border-b border-rule bg-panel px-4 py-3">
            <Dialog.Title className="m-0 flex-1 break-all text-base font-semibold">{title}</Dialog.Title>
            {actions}
            <Dialog.Close asChild><Button variant="ghost" size="sm" aria-label="닫기">✕</Button></Dialog.Close>
          </div>
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

// ---------- containers ----------
export function Panel({ title, description, actions, children, className, pad = true }: { title?: ReactNode; description?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; pad?: boolean }) {
  return (
    <section className={cx("rounded-lg border border-rule bg-panel", className)}>
      {(title || actions) && (
        <header className="flex flex-wrap items-start gap-3 px-4 pt-3.5">
          <div className="min-w-0 flex-1">
            {title && <h2 className="m-0 text-[15px] font-semibold">{title}</h2>}
            {description && <p className="m-0 mt-0.5 max-w-[90ch] text-[13px] text-ink-2">{description}</p>}
          </div>
          {actions}
        </header>
      )}
      <div className={pad ? "px-4 pb-4 pt-3" : ""}>{children}</div>
    </section>
  );
}

export function Stat({ label, value, sub, tone }: { label: ReactNode; value: ReactNode; sub?: ReactNode; tone?: "pass" | "fail" }) {
  return (
    <div className="min-w-0 border-rule px-3.5 py-3">
      <div className="text-xs text-ink-2">{label}</div>
      <div className={cx("text-xl font-semibold tracking-tight", tone === "pass" && "text-pass", tone === "fail" && "text-fail")}>{value}</div>
      {sub && <div className="text-xs text-ink-3">{sub}</div>}
    </div>
  );
}
export function StatGrid({ children, cols = 4 }: { children: ReactNode; cols?: number }) {
  return <div className="grid divide-x divide-y divide-rule overflow-hidden rounded-lg border border-rule [&>*]:border-rule" style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>{children}</div>;
}

export function Empty({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="px-4 py-10 text-center text-ink-2">
      <b className="mb-1 block font-semibold text-ink">{title}</b>
      {children}
      {action && <div className="mt-3">{action}</div>}
    </div>
  );
}

export function Callout({ tone = "warn", children }: { tone?: "warn" | "info" | "fail"; children: ReactNode }) {
  const cls = { warn: "border-warn bg-warn-soft", info: "border-ink-3 bg-sunken text-ink-2", fail: "border-fail bg-fail-soft" }[tone];
  return <div className={cx("mb-3 rounded-r border-l-[3px] px-3 py-2 text-[13px]", cls)}>{children}</div>;
}

export function CopyField({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <div className="my-1.5 flex items-stretch overflow-hidden rounded border border-rule bg-sunken">
      <code className="flex-1 overflow-x-auto whitespace-nowrap px-2.5 py-1.5 font-mono text-xs">{text}</code>
      <button className="border-l border-rule px-2.5 text-xs text-ink-2 hover:text-ink"
        onClick={() => navigator.clipboard.writeText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1500); })}>
        {done ? "복사됨" : "복사"}
      </button>
    </div>
  );
}

export function KV({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <dl className="m-0 grid grid-cols-[120px_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-[13px]">
      {items.map(([k, v], i) => (<div key={i} className="contents"><dt className="text-ink-2">{k}</dt><dd className="m-0 break-words">{v}</dd></div>))}
    </dl>
  );
}

export function Loading({ label = "불러오는 중" }: { label?: string }) {
  return <div className="px-4 py-16 text-center text-ink-3" role="status">{label}…</div>;
}
