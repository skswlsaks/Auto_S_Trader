import { Suspense } from "react";
import { Loading } from "@/components/ui";
import { CompareView } from "@/views/compare-view";

export const metadata = { title: "모델 비교 · Model Arena" };
export default function Page() {
  return <Suspense fallback={<Loading />}><CompareView /></Suspense>;
}
