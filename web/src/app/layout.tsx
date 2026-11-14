import type { Metadata } from "next";
import { IBM_Plex_Sans_KR } from "next/font/google";
import { AppShell } from "@/components/shell/app-shell";
import "./globals.css";

const plex = IBM_Plex_Sans_KR({ weight: ["400", "500", "600", "700"], subsets: ["latin"], variable: "--font-plex", display: "swap", preload: false });

export const metadata: Metadata = { title: "Model Arena", description: "s_trader 모델·조건 평가 대시보드" };

// set the theme before first paint (saved choice, else the OS preference)
const themeScript = `(function(){try{var t=localStorage.getItem("arena-theme")||(matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");document.documentElement.dataset.theme=t}catch(e){}})()`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ko" className={plex.variable} suppressHydrationWarning>
      <head><script dangerouslySetInnerHTML={{ __html: themeScript }} /></head>
      <body><AppShell>{children}</AppShell></body>
    </html>
  );
}
