import { Suspense } from "react";
import { Loading } from "@/components/ui";
import { RunView } from "@/views/run-view";

export const metadata = { title: "run 상세 · Model Arena" };
export default function Page() {
  return <Suspense fallback={<Loading />}><RunView /></Suspense>;
}
