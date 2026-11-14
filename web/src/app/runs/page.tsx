import { Suspense } from "react";
import { Loading } from "@/components/ui";
import { RunsView } from "@/views/runs-view";

export const metadata = { title: "모델 순위 · Model Arena" };
export default function Page() {
  return <Suspense fallback={<Loading />}><RunsView /></Suspense>;
}
