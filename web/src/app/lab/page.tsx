import { Suspense } from "react";
import { Loading } from "@/components/ui";
import { LabView } from "@/views/lab-view";

export const metadata = { title: "조합 실험실 · Model Arena" };
export default function Page() {
  return <Suspense fallback={<Loading />}><LabView /></Suspense>;
}
